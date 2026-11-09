"""Unbiased Evaluator: judges a claim, story or situation on evidence and plausibility.

Pipeline
  1. Research  - Claude searches the web for evidence FOR and AGAINST each part (skipped with --offline).
  2. Adjudicate - Claude returns a structured verdict (JSON schema): claims, coincidences, hypotheses, whole.
  3. Enforce   - Python rules below override the model where the evidence does not support it.

Usage
  python evaluator.py "claim or situation text"
  python evaluator.py --file case.txt --offline --json out/verdict.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlparse

MODEL = os.environ.get("EVALUATOR_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("EVALUATOR_EFFORT", "high")
MAX_SEARCHES = int(os.environ.get("EVALUATOR_MAX_SEARCHES", "15"))
MAX_CONTINUATIONS = 4
BETAS = ["server-side-fallback-2026-07-01"]  # re-runs a policy-declined request on a fallback model

CONF = ["LOW", "MEDIUM", "HIGH"]
CLAIM_VERDICTS = ["SUPPORTED", "CONTRADICTED", "MIXED", "UNVERIFIED", "NOT_CHECKABLE"]
WHOLE_VERDICTS = ["SUPPORTED", "PARTLY_SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]

PRINCIPLES = """You are an impartial evaluator. You have no side. Your only loyalty is to evidence.

Rules of reasoning:
1. Neutralise framing. Restate the subject in neutral words. Ignore who said it, tone, loaded words,
   and what outcome anyone wants. The same evidence must get the same verdict whoever it favours.
2. Parts, then whole. Split the subject into atomic claims. Judge each on its own evidence. Then judge
   the whole: do the parts fit together in time, place, motive and mechanism? A whole is never
   stronger than its weakest load-bearing part.
3. Label every claim: FACT (checkable statement about the world), INFERENCE (a conclusion drawn from
   facts), JUDGEMENT (a value or opinion; verdict NOT_CHECKABLE unless it rests on checkable facts).
4. Symmetric search. Look for disconfirming evidence as hard as confirming evidence.
5. Source independence. Sources that copy one origin count as one source. Prefer primary sources.
6. Base rates. Ask how often this kind of thing happens before judging this case.
7. Competing hypotheses. Always test at least two explanations, including a mundane one.
   Rank them by how much evidence is INCONSISTENT with each, not by how much fits.
8. Coincidence skepticism. A coincidence is any link that the story explains only by chance (timing,
   location, people, numbers lining up). One coincidence is normal. Two or more is a cluster: treat it
   as a warning sign. Estimate each one's chance and whether they are independent; independent chances
   multiply, so a cluster is far less likely than any single one. For a cluster, test BOTH: (a) a hidden
   common cause, design, manipulation or fabrication, and (b) selection effects - were the matches
   picked after the fact out of many possible ones (Texas sharpshooter)? Do not accept "just chance"
   or "it must be a plot" without evidence for it.
9. Say "unknown" when evidence is missing. Never fill gaps with assumptions. Never invent sources."""

RESEARCH_PROMPT = PRINCIPLES + """

TASK: Research only. Do not give a final verdict yet.
- Restate the subject neutrally and list its atomic claims.
- For each checkable claim, search for evidence for AND against it. Note the source of each finding.
- List every coincidence the story relies on.
- Note base rates and plausible alternative explanations.
Write concise research notes. Cite only pages you actually retrieved."""

ADJUDICATE_PROMPT = PRINCIPLES + """

TASK: Produce the final structured evaluation from the subject and research notes.
- evidence.source is either an exact URL from the RETRIEVED URLS list or the word "input"
  (the user's own text). Never use any other URL.
- load_bearing = true if the overall story fails when this claim is false.
- depends_on_coincidences = how many listed coincidences a hypothesis needs to be pure chance.
- what_would_change_my_mind: concrete evidence that would flip the verdict."""


def _enum(values):
    return {"type": "string", "enum": values}


_EVIDENCE = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "source": {"type": "string"}},
    "required": ["summary", "source"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "neutral_restatement": {"type": "string"},
        "claims": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "text": {"type": "string"},
                "label": _enum(["FACT", "INFERENCE", "JUDGEMENT"]),
                "load_bearing": {"type": "boolean"},
                "verdict": _enum(CLAIM_VERDICTS),
                "confidence": _enum(CONF),
                "evidence_for": {"type": "array", "items": _EVIDENCE},
                "evidence_against": {"type": "array", "items": _EVIDENCE},
                "reasoning": {"type": "string"},
            },
            "required": ["id", "text", "label", "load_bearing", "verdict", "confidence",
                         "evidence_for", "evidence_against", "reasoning"],
            "additionalProperties": False,
        }},
        "coincidences": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "claim_ids": {"type": "array", "items": {"type": "string"}},
                "estimated_chance": {"type": "string"},
                "independent_of_others": {"type": "boolean"},
            },
            "required": ["description", "claim_ids", "estimated_chance", "independent_of_others"],
            "additionalProperties": False,
        }},
        "hypotheses": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "explanation": {"type": "string"},
                "inconsistent_evidence": {"type": "array", "items": {"type": "string"}},
                "depends_on_coincidences": {"type": "integer"},
                "rank": {"type": "integer"},
            },
            "required": ["name", "explanation", "inconsistent_evidence", "depends_on_coincidences", "rank"],
            "additionalProperties": False,
        }},
        "bias_checks": {
            "type": "object",
            "properties": {
                "loaded_language": {"type": "array", "items": {"type": "string"}},
                "missing_perspectives": {"type": "array", "items": {"type": "string"}},
                "source_independence": {"type": "string"},
            },
            "required": ["loaded_language", "missing_perspectives", "source_independence"],
            "additionalProperties": False,
        },
        "whole": {
            "type": "object",
            "properties": {
                "verdict": _enum(WHOLE_VERDICTS),
                "confidence": _enum(CONF),
                "summary": {"type": "string"},
                "coherence_issues": {"type": "array", "items": {"type": "string"}},
                "what_would_change_my_mind": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "confidence", "summary", "coherence_issues", "what_would_change_my_mind"],
            "additionalProperties": False,
        },
    },
    "required": ["neutral_restatement", "claims", "coincidences", "hypotheses", "bias_checks", "whole"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- model calls
def _check_stop(resp) -> None:
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Output hit max_tokens; shorten the input or raise the limit.")


def _retrieved_urls(responses) -> set[str]:
    """URLs the web search tool actually returned or that text blocks cite."""
    urls: set[str] = set()
    for resp in responses:
        for block in resp.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                urls.update(getattr(r, "url", "") for r in block.content)
            elif block.type == "text":
                urls.update(getattr(c, "url", "") or "" for c in (getattr(block, "citations", None) or []))
    urls.discard("")
    return urls


def research(client, subject: str) -> tuple[str, set[str]]:
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES}]
    messages = [{"role": "user", "content": f"SUBJECT TO EVALUATE:\n<subject>\n{subject}\n</subject>"}]
    responses = []
    for _ in range(MAX_CONTINUATIONS + 1):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=64000, system=RESEARCH_PROMPT, tools=tools, messages=messages,
            output_config={"effort": EFFORT}, betas=BETAS, fallbacks="default",
        ) as stream:
            resp = stream.get_final_message()
        _check_stop(resp)
        responses.append(resp)
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    else:
        raise RuntimeError("Research did not finish within the continuation limit.")
    notes = "\n".join(b.text for r in responses for b in r.content if b.type == "text")
    return notes, _retrieved_urls(responses)


def adjudicate(client, subject: str, notes: str, urls: set[str]) -> dict:
    user = (f"SUBJECT:\n<subject>\n{subject}\n</subject>\n\n"
            f"RESEARCH NOTES:\n<notes>\n{notes or '(offline: no external research)'}\n</notes>\n\n"
            "RETRIEVED URLS:\n" + ("\n".join(sorted(urls)) or "(none)"))
    with client.beta.messages.stream(
        model=MODEL, max_tokens=64000, system=ADJUDICATE_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=BETAS, fallbacks="default",
    ) as stream:
        resp = stream.get_final_message()
    _check_stop(resp)
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


# ---------------------------------------------------------------- enforcement
def _cap(level: str, ceiling: str) -> str:
    return CONF[min(CONF.index(level), CONF.index(ceiling))]


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def enforce(data: dict, retrieved: set[str], offline: bool) -> dict:
    """Apply hard rules so the verdict cannot outrun the evidence. Records every override."""
    notes: list[str] = []

    def keep(evidence: list[dict], cid: str) -> list[dict]:
        out = []
        for e in evidence:
            src = (e.get("source") or "").strip()
            if src == "input" or (src.startswith("https://") and src in retrieved):
                out.append(e)
            else:
                notes.append(f"{cid}: dropped evidence with unverifiable source '{src}'.")
        return out

    for c in data["claims"]:
        cid = c["id"]
        c["evidence_for"] = keep(c["evidence_for"], cid)
        c["evidence_against"] = keep(c["evidence_against"], cid)
        web_for = {_domain(e["source"]) for e in c["evidence_for"] if e["source"] != "input"}
        if c["label"] == "FACT" and c["verdict"] == "SUPPORTED":
            if not web_for:
                c["verdict"], c["confidence"] = "UNVERIFIED", "LOW"
                why = "offline mode, not checked externally" if offline else "no retrieved source supports it"
                notes.append(f"{cid}: FACT marked SUPPORTED but {why} -> UNVERIFIED.")
            elif len(web_for) < 2 and c["confidence"] == "HIGH":
                c["confidence"] = "MEDIUM"
                notes.append(f"{cid}: HIGH confidence needs 2+ independent domains -> MEDIUM.")
        if c["verdict"] == "SUPPORTED" and c["evidence_against"] and c["confidence"] == "HIGH":
            c["confidence"] = "MEDIUM"
            notes.append(f"{cid}: has counter-evidence -> confidence capped at MEDIUM.")

    whole = data["whole"]
    core = [c for c in data["claims"] if c["load_bearing"]]

    # Parts -> whole consistency
    if any(c["verdict"] == "CONTRADICTED" for c in core) and whole["verdict"] in ("SUPPORTED", "PARTLY_SUPPORTED"):
        whole["verdict"] = "UNSUPPORTED"
        notes.append("Whole: a load-bearing claim is CONTRADICTED -> UNSUPPORTED.")
    if any(c["verdict"] in ("UNVERIFIED", "MIXED") for c in core) and whole["verdict"] == "SUPPORTED":
        whole["verdict"] = "PARTLY_SUPPORTED"
        notes.append("Whole: a load-bearing claim is unverified or mixed -> PARTLY_SUPPORTED.")
    if core:
        weakest = min((c["confidence"] for c in core), key=CONF.index)
        if CONF.index(whole["confidence"]) > CONF.index(weakest):
            whole["confidence"] = weakest
            notes.append(f"Whole: confidence capped at weakest load-bearing claim ({weakest}).")

    # Coincidence skepticism
    n = len(data["coincidences"])
    data["coincidence_cluster"] = n >= 2
    if n >= 2:
        whole["confidence"] = _cap(whole["confidence"], "MEDIUM")
        notes.append(f"Coincidence cluster ({n}): whole confidence capped at MEDIUM; treat with skepticism.")
        top = min(data["hypotheses"], key=lambda h: h["rank"], default=None)
        if top and top["depends_on_coincidences"] >= 2:
            whole["confidence"] = "LOW"
            notes.append(f"Top hypothesis '{top['name']}' needs {top['depends_on_coincidences']} "
                         "coincidences to be chance -> confidence LOW.")

    # Competing hypotheses
    if len(data["hypotheses"]) < 2:
        whole["confidence"] = "LOW"
        notes.append("Fewer than 2 hypotheses tested -> confidence LOW.")

    data["enforcement_notes"] = notes
    return data


# ---------------------------------------------------------------- rendering
def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _sources(evidence: list[dict]) -> str:
    return "<br>".join(_cell(e["source"]) for e in evidence) or "-"


def render(d: dict) -> str:
    w = d["whole"]
    out = [f"# Verdict: {w['verdict']} (confidence {w['confidence']})", "", _cell(w["summary"]), ""]
    if d.get("coincidence_cluster"):
        out += [f"> **Coincidence cluster:** {len(d['coincidences'])} coincidences. Read skeptically.", ""]
    out += ["**Neutral restatement:** " + _cell(d["neutral_restatement"]), "", "## Parts",
            "| ID | Claim | Label | Core | Verdict | Conf. | Sources for | Sources against |",
            "|---|---|---|---|---|---|---|---|"]
    out += [f"| {c['id']} | {_cell(c['text'])} | {c['label']} | {'yes' if c['load_bearing'] else 'no'} | "
            f"{c['verdict']} | {c['confidence']} | {_sources(c['evidence_for'])} | {_sources(c['evidence_against'])} |"
            for c in d["claims"]]
    if d["coincidences"]:
        out += ["", "## Coincidences", "| # | Coincidence | Claims | Est. chance | Independent |", "|---|---|---|---|---|"]
        out += [f"| {i} | {_cell(k['description'])} | {', '.join(k['claim_ids'])} | {_cell(k['estimated_chance'])} | "
                f"{'yes' if k['independent_of_others'] else 'no'} |" for i, k in enumerate(d["coincidences"], 1)]
    out += ["", "## Competing hypotheses", "| Rank | Hypothesis | Evidence against it | Coincidences needed |",
            "|---|---|---|---|"]
    out += [f"| {h['rank']} | **{_cell(h['name'])}**: {_cell(h['explanation'])} | "
            f"{_cell('; '.join(h['inconsistent_evidence']) or '-')} | {h['depends_on_coincidences']} |"
            for h in sorted(d["hypotheses"], key=lambda h: h["rank"])]
    b = d["bias_checks"]
    out += ["", "## Bias checks",
            f"- Loaded language: {_cell(', '.join(b['loaded_language']) or 'none found')}",
            f"- Missing perspectives: {_cell(', '.join(b['missing_perspectives']) or 'none found')}",
            f"- Source independence: {_cell(b['source_independence'])}"]
    if w["coherence_issues"]:
        out += ["", "## Whole-picture issues"] + [f"- {_cell(x)}" for x in w["coherence_issues"]]
    out += ["", "## What would change this verdict"] + [f"- {_cell(x)}" for x in w["what_would_change_my_mind"]]
    if d.get("enforcement_notes"):
        out += ["", "## Rule overrides"] + [f"- {x}" for x in d["enforcement_notes"]]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Evidence-first, coincidence-skeptical evaluator.")
    p.add_argument("subject", nargs="?", help="Claim, story or situation to evaluate.")
    p.add_argument("--file", help="Read the subject from a text file.")
    p.add_argument("--offline", action="store_true", help="No web search; reason only over the input.")
    p.add_argument("--json", dest="json_out", help="Also write the full verdict JSON to this path.")
    a = p.parse_args(argv)
    subject = open(a.file, encoding="utf-8").read() if a.file else a.subject
    if not subject or not subject.strip():
        p.error("give a subject or --file")

    import anthropic
    client = anthropic.Anthropic()
    notes, urls = ("", set()) if a.offline else research(client, subject)
    data = enforce(adjudicate(client, subject, notes, urls), urls, a.offline)
    sys.stdout.write(render(data))
    if a.json_out:
        os.makedirs(os.path.dirname(a.json_out) or ".", exist_ok=True)
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
