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
STATE_FILE = HERE / "state" / "seen.json"
SEEN_NEWS_LIMIT = 400
SAME_STORY = 0.5  # word-overlap (Jaccard) at or above this = same story
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

Never repeat anything already sent. You get lists of already-sent buzzwords and news.
Section 1 - BUZZWORDS: {NUM_BUZZWORDS} AI buzzwords or jargon terms that are new or trending
right now and NOT in the already-sent buzzword list. Each gets a plain-English meaning in at most
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
A story in the already-sent news list may be included ONLY if something material changed since
(new facts, official confirmation or denial, a correction, a changed label). Then put what changed
in at most 20 words in what_changed. Otherwise leave it out. For new stories what_changed is "".

Reply with ONLY one JSON object, no prose, in this shape:
{{"buzzwords":[{{"term":"","meaning":"","source_title":"","source_url":""}}],
 "news":[{{"headline":"","summary":"","label":"FACT|JUDGEMENT|INFERENCE|RUMOR","why_label":"","what_changed":"",
           "sources":[{{"title":"","url":""}}]}}]}}"""


# ---------------------------------------------------------------- config / state
def load_sources() -> tuple[set[str], set[str]]:
    data = json.loads((HERE / "sources.json").read_text())
    return set(data["primary"]), set(data["press"])


def load_no_crawl() -> set[str]:
    """Allowlisted outlets whose robots rules make web search error out; cite-only, never searched."""
    return set(json.loads((HERE / "sources.json").read_text()).get("no_crawl", []))


def load_seen() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {"buzzwords": data.get("buzzwords", []), "news": data.get("news", [])}


def save_seen(seen: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {"buzzwords": sorted(set(seen["buzzwords"]), key=str.lower),
           "news": seen["news"][-SEEN_NEWS_LIMIT:]}
    STATE_FILE.write_text(json.dumps(out, indent=2) + "\n")


_STOP = {"the", "and", "for", "with", "that", "this", "from", "its", "into", "over", "new", "says", "after"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def same_story(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= SAME_STORY


def _norm_term(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()


def drop_repeats(content: dict, seen: dict) -> dict:
    """Remove anything already sent. A news story returns only with a stated change."""
    known = {_norm_term(t) for t in seen["buzzwords"]}
    buzz = [b for b in content["buzzwords"] if _norm_term(b["term"]) not in known]
    news = []
    for n in content["news"]:
        match = next((s for s in seen["news"] if same_story(n["headline"], s["headline"])), None)
        if match is None:
            n["update"] = ""
            news.append(n)
        elif n.get("what_changed"):
            n["update"] = n["what_changed"]
            news.append(n)
    if not buzz and not news:
        raise RuntimeError("Nothing new since the last issue; not sending")
    return {"buzzwords": buzz, "news": news}


def record_sent(content: dict, seen: dict, today: date) -> dict:
    seen["buzzwords"] += [b["term"] for b in content["buzzwords"]]
    for n in content["news"]:
        seen["news"] = [s for s in seen["news"] if not same_story(n["headline"], s["headline"])]
        seen["news"].append({"headline": n["headline"], "label": n["label"], "date": today.isoformat()})
    return seen


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


def research(seen: dict, today: date, allowed_domains: list[str]) -> tuple[dict, set[str]]:
    import anthropic

    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search",
              "allowed_domains": allowed_domains, "max_uses": 20}]
    user = (
        f"Today is {today.isoformat()}. Write today's briefing.\n"
        f"Already sent buzzwords: {', '.join(seen['buzzwords'][-300:]) or 'none'}\n"
        "Already sent news (headline | label | date):\n"
        + ("\n".join(f"- {n['headline']} | {n['label']} | {n['date']}" for n in seen["news"][-60:]) or "none")
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
                     "label": label, "why_label": note, "sources": sources,
                     "what_changed": (n.get("what_changed") or "").strip()})
    if not buzz and not news:
        raise RuntimeError("No validated content; refusing to send an empty newsletter")
    return {"buzzwords": buzz, "news": news}


# ---------------------------------------------------------------- rendering
# Soft pastel pills; light defaults inline, dark overrides in <style>. Cards use their own
# fills so the layout holds even in clients that strip <style> or force their own dark mode.
BADGE_LIGHT = {"FACT": ("#e3f4e8", "#1b6b34"), "JUDGEMENT": ("#ece6fa", "#5b3fb0"),
               "INFERENCE": ("#fbf0d6", "#7a5a05"), "RUMOR": ("#fbe4e4", "#a4262c"),
               "UPDATE": ("#e2eefc", "#1f5fae")}
BADGE_DARK = {"FACT": ("#16321f", "#86e0a3"), "JUDGEMENT": ("#2a2244", "#c3b0f5"),
              "INFERENCE": ("#3a2f12", "#f0cd6e"), "RUMOR": ("#3d1c1e", "#f4a3a6"),
              "UPDATE": ("#16283f", "#9cc4f5")}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _css() -> str:
    dark = "".join(f".b-{k.lower()}{{background:{bg}!important;color:{fg}!important}}"
                   for k, (bg, fg) in BADGE_DARK.items())
    return (":root{color-scheme:light dark;supported-color-schemes:light dark}"
            "@media (prefers-color-scheme:dark){"
            "body,.page{background:#0f1216!important;color:#e7eaee!important}"
            ".card{background:#181d23!important;border-color:#262d36!important}"
            ".muted{color:#9aa5b1!important}a{color:#8ab4ff!important}.rule{border-color:#262d36!important}"
            + dark + "}")


def _badge(label: str) -> str:
    bg, fg = BADGE_LIGHT[label]
    return (f'<span class="b-{label.lower()}" style="display:inline-block;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;letter-spacing:.08em;padding:4px 10px;border-radius:999px">{label}</span>')


def render_html(content: dict, today: date) -> str:
    e = html.escape
    muted = "color:#66717d"
    card = ("background:#ffffff;border:1px solid #e6eaef;border-radius:16px;"
            "padding:20px;margin:0 0 16px 0")

    def sources(items, prefix=""):
        rows = "".join(
            f'<div style="margin:6px 0 0 0;font-size:14px;line-height:1.5">'
            f'<a href="{e(s["url"])}" style="color:#2563eb;text-decoration:none">&#8599; {e(s["title"])}</a>'
            f' <span class="muted" style="{muted}">{e(s["host"])}</span></div>' for s in items)
        return f'<div style="margin-top:12px">{prefix}{rows}</div>'

    def heading(text, count):
        return (f'<p class="muted" style="margin:32px 0 12px 4px;font-size:12px;font-weight:700;'
                f'letter-spacing:.14em;text-transform:uppercase;{muted}">{text} &middot; {count}</p>')

    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f'<title>AI Daily Brief</title><style>{_css()}</style></head>'
        f'<body style="margin:0;padding:0;background:#f3f5f8;color:#1c2229">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">'
        f'{len(content["buzzwords"])} new AI terms and {len(content["news"])} fact-checked stories.</div>'
        f'<div class="page" style="background:#f3f5f8;padding:24px 16px;font-family:{FONT};font-size:16px;line-height:1.6">'
        '<div style="max-width:560px;margin:0 auto">',
        f'<h1 style="margin:8px 4px 4px;font-size:28px;line-height:1.2;font-weight:700">AI Daily Brief</h1>'
        f'<p class="muted" style="margin:0 4px;font-size:15px;{muted}">{today:%A, %-d %B %Y}</p>',
    ]
    if content["buzzwords"]:
        parts.append(heading("Buzzwords", len(content["buzzwords"])))
        for b in content["buzzwords"]:
            parts.append(
                f'<div class="card" style="{card}"><div style="font-size:18px;font-weight:700;margin:0 0 6px">{e(b["term"])}</div>'
                f'<div style="font-size:16px;line-height:1.6">{e(b["meaning"])}</div>'
                f'{sources([b["source"]])}</div>')
    if content["news"]:
        parts.append(heading("AI News", len(content["news"])))
        for n in content["news"]:
            upd = (f' {_badge("UPDATE")}' if n.get("update") else "")
            changed = (f'<div style="margin:0 0 8px;font-size:15px"><strong>What changed:</strong> {e(n["update"])}</div>'
                       if n.get("update") else "")
            why = f'<div class="muted" style="margin-top:10px;font-size:14px;line-height:1.5;{muted}">{e(n["why_label"])}</div>' if n["why_label"] else ""
            parts.append(
                f'<div class="card" style="{card}"><div style="margin:0 0 12px">{_badge(n["label"])}{upd}</div>'
                f'<div style="font-size:19px;line-height:1.35;font-weight:700;margin:0 0 10px">{e(n["headline"])}</div>'
                f'{changed}<div style="font-size:16px;line-height:1.6">{e(n["summary"])}</div>{why}{sources(n["sources"])}</div>')
    legend = "".join(
        f'<div style="margin:0 0 10px">{_badge(k)}<div class="muted" style="font-size:14px;line-height:1.5;margin-top:4px;{muted}">{e(v)}</div></div>'
        for k, v in LABEL_MEANING.items())
    parts.append(
        f'<p class="muted" style="margin:32px 0 12px 4px;font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;{muted}">How labels work</p>'
        f'<div style="margin:0 4px">{legend}</div>'
        f'<hr class="rule" style="border:0;border-top:1px solid #e6eaef;margin:24px 0">'
        f'<p class="muted" style="margin:0 4px;font-size:13px;line-height:1.5;{muted}">Auto-generated with AI web research. '
        'Labels follow source rules checked in code. Verify before you rely on anything.</p>'
        '</div></div></body></html>')
    return "".join(parts)


def render_text(content: dict, today: date) -> str:
    lines = [f"AI DAILY BRIEF", f"{today:%A, %-d %B %Y}", ""]
    if content["buzzwords"]:
        lines += ["BUZZWORDS", ""]
        for b in content["buzzwords"]:
            lines += [b["term"], f"  {b['meaning']}", f"  Source: {b['source']['url']}", ""]
    if content["news"]:
        lines += ["AI NEWS", ""]
        for n in content["news"]:
            tag = f"[{n['label']}]" + (" [UPDATE]" if n.get("update") else "")
            lines += [f"{tag} {n['headline']}"]
            if n.get("update"):
                lines.append(f"  What changed: {n['update']}")
            lines.append(f"  {n['summary']}")
            if n["why_label"]:
                lines.append(f"  Why {n['label']}: {n['why_label']}")
            lines += [f"  Source: {s['url']}" for s in n["sources"]]
            lines.append("")
    lines += ["HOW LABELS WORK"] + [f"  {k}: {v}" for k, v in LABEL_MEANING.items()]
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
    ap.add_argument("--render-json", metavar="FILE", help="skip research: validate+render researched JSON to out/")
    ap.add_argument("--seen-json", metavar="FILE", help="already-sent buzzwords/news JSON (with --render-json)")
    args = ap.parse_args()

    today = date.today()
    primary, press = load_sources()
    if args.render_json:
        seen = json.loads(Path(args.seen_json).read_text()) if args.seen_json else load_seen()
        seen = {"buzzwords": seen.get("buzzwords", []), "news": seen.get("news", [])}
        content = drop_repeats(validate(json.loads(Path(args.render_json).read_text()), primary, press, set()), seen)
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / "latest.html").write_text(render_html(content, today))
        (OUT_DIR / "latest.txt").write_text(render_text(content, today))
        next_seen = record_sent(content, {"buzzwords": list(seen["buzzwords"]), "news": list(seen["news"])}, today)
        (OUT_DIR / "next_seen.json").write_text(json.dumps(next_seen, indent=2) + "\n")  # copy to state/seen.json after a successful send
        print(f"Rendered {len(content['buzzwords'])} buzzwords, {len(content['news'])} news to {OUT_DIR}")
        return 0
    seen = load_seen()
    raw, retrieved = research(seen, today, sorted((primary | press) - load_no_crawl()))
    if not retrieved:
        print("WARNING: no retrieved URLs detected; source-in-results check skipped", file=sys.stderr)
    content = drop_repeats(validate(raw, primary, press, retrieved), seen)
    html_body, text_body = render_html(content, today), render_text(content, today)

    if args.dry_run:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / "latest.html").write_text(html_body)
        (OUT_DIR / "latest.txt").write_text(text_body)
        print(text_body)
        return 0

    recipients = [r.strip() for r in os.environ["NEWSLETTER_RECIPIENTS"].split(",") if r.strip()]
    send(f"AI Daily Brief - {today:%d %b %Y}", html_body, text_body, recipients)
    save_seen(record_sent(content, seen, today))
    print(f"Sent to {len(recipients)} recipient(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
