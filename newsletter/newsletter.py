"""Daily AI newsletter: buzzwords + fact-checked news, emailed via SMTP.

Usage:
    python newsletter.py --dry-run     # research + render to out/, no email
    python newsletter.py               # research + email every recipient
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
STATE_FILE = HERE / "state" / "seen_terms.json"
OUT_DIR = HERE / "out"

MODEL = os.environ.get("NEWSLETTER_MODEL", "claude-sonnet-5-5")
MAX_CONTINUATIONS = 5
NUM_BUZZWORDS = 5
NUM_NEWS = 6

LABELS = ("FACT", "JUDGEMENT", "INFERENCE", "RUMOR")
LABEL_COLORS = {
    "FACT": "#1a7f37",
    "JUDGEMENT": "#8250df",
    "INFERENCE": "#bf8700",
    "RUMOR": "#cf222e",
}
LABEL_MEANING = {
    "FACT": "Confirmed by a primary source or 2+ independent outlets.",
    "JUDGEMENT": "An opinion or assessment (by a named person or outlet).",
    "INFERENCE": "A conclusion drawn from facts; not stated by any source.",
    "RUMOR": "Unconfirmed, leaked, or single-source claim.",
}

SYSTEM_PROMPT = f"""You write a short daily AI briefing for a busy non-expert reader.
Use the web_search tool. Every claim must come from a page you actually retrieved.
Never invent a URL, a quote, a date, or a source. If you cannot find enough, return fewer items.

Section 1 - BUZZWORDS: {NUM_BUZZWORDS} AI buzzwords or jargon terms that are new or trending
right now and NOT in the "already sent" list. Each gets a plain-English meaning in at most
25 words (explain like the reader is smart but new to AI, no jargon inside the explanation)
and one source URL that defines or uses the term.

Section 2 - NEWS: up to {NUM_NEWS} AI news items from the last 48 hours, each with a summary of at
most 40 words and 1-3 source URLs. Label every item with exactly one label:
- FACT: confirmed by a primary source (the company, lab, regulator or paper itself) or by 2+
  independent reputable outlets.
- JUDGEMENT: an opinion or assessment. Say whose in why_label.
- INFERENCE: your or an outlet's conclusion drawn from facts, or a forecast. Not directly stated by a source.
- RUMOR: unconfirmed, leaked, anonymous-source, or single-outlet claim not confirmed elsewhere.
When unsure between two labels, choose the weaker one (RUMOR is weakest, FACT strongest).
Cross-check each item against at least one more source before calling it FACT.

Reply with ONLY one JSON object, no prose, in this shape:
{{"buzzwords":[{{"term":"","meaning":"","source_title":"","source_url":""}}],
 "news":[{{"headline":"","summary":"","label":"FACT|JUDGEMENT|INFERENCE|RUMOR","why_label":"",
           "sources":[{{"title":"","url":""}}]}}]}}"""


# ---------------------------------------------------------------- config / state
def load_sources() -> tuple[set[str], set[str]]:
    data = json.loads((HERE / "sources.json").read_text())
    return set(data["primary"]), set(data["press"])


def load_seen() -> list[str]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(terms: list[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(set(terms), key=str.lower), indent=2) + "\n")


# ---------------------------------------------------------------- research
def _text_of(message) -> str:
    return "".join(b.text for b in message.content if b.type == "text")


def _retrieved_urls(messages) -> set[str]:
    """URLs the search tool actually returned or that citations point to."""
    urls: set[str] = set()
    for message in messages:
        for block in message.content:
            if block.type in ("web_search_tool_result", "web_fetch_tool_result"):
                content = block.content
                if isinstance(content, list):  # error results are a single object
                    urls.update(getattr(r, "url", "") for r in content)
            if block.type == "text":
                for c in getattr(block, "citations", None) or []:
                    urls.add(getattr(c, "url", "") or "")
    urls.discard("")
    return urls


def research(seen: list[str], today: date, allowed_domains: list[str]) -> tuple[dict, set[str]]:
    import anthropic

    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "allowed_domains": allowed_domains, "max_uses": 20}]
    user = (
        f"Today is {today.isoformat()}. Write today's briefing.\n"
        f"Already sent buzzwords (do not repeat): {', '.join(seen[-300:]) or 'none'}"
    )
    messages = [{"role": "user", "content": user}]
    responses = []
    for _ in range(MAX_CONTINUATIONS + 1):
        with client.messages.stream(
            model=MODEL, max_tokens=32000, system=SYSTEM_PROMPT, tools=tools,
            output_config={"effort": "high"}, messages=messages,
        ) as stream:
            resp = stream.get_final_message()
        responses.append(resp)
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Model refused: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    else:
        raise RuntimeError("Research did not finish within continuation limit")

    return parse_json(_text_of(responses[-1])), _retrieved_urls(responses)


def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Model reply contained no JSON object")
    return json.loads(match.group(0))


# ---------------------------------------------------------------- validation
def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _in(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _clean_sources(raw, primary, press, retrieved) -> list[dict]:
    """Keep only https sources on the allowlist that the search tool really returned."""
    out, seen_urls = [], set()
    for s in raw or []:
        url = (s.get("url") or "").strip()
        host = _host(url)
        if not url.startswith("https://") or url in seen_urls:
            continue
        if not (_in(host, primary) or _in(host, press)):
            continue
        if retrieved and url not in retrieved:
            continue
        seen_urls.add(url)
        out.append({"title": (s.get("title") or host).strip(), "url": url, "host": host})
    return out


def validate(data: dict, primary: set[str], press: set[str], retrieved: set[str]) -> dict:
    """Drop unsourced items and downgrade labels the evidence does not support."""
    buzz = []
    for b in data.get("buzzwords", []):
        src = _clean_sources([{"title": b.get("source_title"), "url": b.get("source_url")}],
                             primary, press, retrieved)
        if b.get("term") and b.get("meaning") and src:
            buzz.append({"term": b["term"].strip(), "meaning": b["meaning"].strip(), "source": src[0]})

    news = []
    for n in data.get("news", []):
        sources = _clean_sources(n.get("sources"), primary, press, retrieved)
        if not (n.get("headline") and n.get("summary") and sources):
            continue
        label = str(n.get("label", "")).upper().replace("JUDGMENT", "JUDGEMENT")
        note = (n.get("why_label") or "").strip()
        if label not in LABELS:
            label, note = "RUMOR", "Label missing or invalid; treated as unconfirmed."
        if label == "FACT":
            has_primary = any(_in(s["host"], primary) for s in sources)
            independent = len({s["host"] for s in sources}) >= 2
            if not (has_primary or independent):
                label = "RUMOR"
                note = "Downgraded: only one non-primary source could be verified."
        news.append({"headline": n["headline"].strip(), "summary": n["summary"].strip(),
                     "label": label, "why_label": note, "sources": sources})
    if not buzz and not news:
        raise RuntimeError("No validated content; refusing to send an empty newsletter")
    return {"buzzwords": buzz, "news": news}


# ---------------------------------------------------------------- rendering
def render_html(content: dict, today: date) -> str:
    e = html.escape

    def link(s):
        return f'<a href="{e(s["url"])}" style="color:#0969da">{e(s["title"])}</a> <span style="color:#6e7781">({e(s["host"])})</span>'

    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;color:#1f2328;line-height:1.5">',
        f'<h1 style="font-size:22px;margin-bottom:0">AI Daily Brief</h1><p style="color:#6e7781;margin-top:4px">{today:%A, %d %B %Y}</p>',
        '<h2 style="font-size:18px;border-bottom:1px solid #d0d7de;padding-bottom:4px">Buzzwords</h2>',
    ]
    for b in content["buzzwords"]:
        parts.append(f'<p><strong>{e(b["term"])}</strong> - {e(b["meaning"])}<br><small>Source: {link(b["source"])}</small></p>')
    parts.append('<h2 style="font-size:18px;border-bottom:1px solid #d0d7de;padding-bottom:4px">AI News</h2>')
    for n in content["news"]:
        badge = f'<span style="background:{LABEL_COLORS[n["label"]]};color:#fff;border-radius:4px;padding:1px 6px;font-size:12px;font-weight:600">{n["label"]}</span>'
        srcs = "; ".join(link(s) for s in n["sources"])
        why = f' <small style="color:#6e7781">{e(n["why_label"])}</small>' if n["why_label"] else ""
        parts.append(f'<p>{badge} <strong>{e(n["headline"])}</strong>{why}<br>{e(n["summary"])}<br><small>Sources: {srcs}</small></p>')
    legend = "".join(f'<li><strong>{k}</strong>: {e(v)}</li>' for k, v in LABEL_MEANING.items())
    parts.append(f'<hr><details open><summary style="font-size:13px;color:#6e7781">How labels work</summary><ul style="font-size:13px;color:#6e7781">{legend}</ul></details>')
    parts.append('<p style="font-size:12px;color:#6e7781">Auto-generated with AI web research. Labels are checked against source rules in code, but verify before you rely on anything.</p></div>')
    return "\n".join(parts)


def render_text(content: dict, today: date) -> str:
    lines = [f"AI DAILY BRIEF - {today:%A, %d %B %Y}", "", "BUZZWORDS"]
    for b in content["buzzwords"]:
        lines += [f"- {b['term']}: {b['meaning']}", f"  Source: {b['source']['url']}"]
    lines += ["", "AI NEWS"]
    for n in content["news"]:
        lines += [f"[{n['label']}] {n['headline']}", f"  {n['summary']}"]
        if n["why_label"]:
            lines.append(f"  Why {n['label']}: {n['why_label']}")
        lines += [f"  Source: {s['url']}" for s in n["sources"]]
    lines += ["", "Labels: " + " | ".join(f"{k} = {v}" for k, v in LABEL_MEANING.items())]
    return "\n".join(lines)


# ---------------------------------------------------------------- email
def send(subject: str, html_body: str, text_body: str, recipients: list[str]) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("MAIL_FROM", user)
    ctx = ssl.create_default_context()
    server = smtplib.SMTP_SSL(host, port, context=ctx) if port == 465 else smtplib.SMTP(host, port)
    with server:
        if port != 465:
            server.starttls(context=ctx)
        server.login(user, password)
        for rcpt in recipients:  # one message each so addresses are not shared
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, sender, rcpt
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
            server.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render to newsletter/out/ and do not email")
    args = ap.parse_args()

    today = date.today()
    primary, press = load_sources()
    seen = load_seen()
    raw, retrieved = research(seen, today, sorted(primary | press))
    if not retrieved:
        print("WARNING: no retrieved URLs detected; source-in-results check skipped", file=sys.stderr)
    content = validate(raw, primary, press, retrieved)
    html_body, text_body = render_html(content, today), render_text(content, today)

    if args.dry_run:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / "latest.html").write_text(html_body)
        (OUT_DIR / "latest.txt").write_text(text_body)
        print(text_body)
        return 0

    recipients = [r.strip() for r in os.environ["NEWSLETTER_RECIPIENTS"].split(",") if r.strip()]
    send(f"AI Daily Brief - {today:%d %b %Y}", html_body, text_body, recipients)
    save_seen(seen + [b["term"] for b in content["buzzwords"]])
    print(f"Sent to {len(recipients)} recipient(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
