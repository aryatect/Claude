"""Apply the Unbiased Evaluator's hard rules to a verdict JSON and print the Markdown report.

Usage
  python enforce.py verdict.json --retrieved urls.txt [--json-out final.json]
  python enforce.py verdict.json --offline
  python enforce.py verdict.json --retrieved urls.txt --html report.html --title "Outage review"
  cat verdict.json | python enforce.py - --retrieved urls.txt

urls.txt: one URL per line - every page actually retrieved during research.
The rules are kept identical to evaluator/evaluator.py in the standalone agent (tests check both).
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

CONF = ["LOW", "MEDIUM", "HIGH"]
CLAIM_VERDICTS = ["SUPPORTED", "CONTRADICTED", "MIXED", "UNVERIFIED", "NOT_CHECKABLE"]
WHOLE_VERDICTS = ["SUPPORTED", "PARTLY_SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]


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


# ---------------------------------------------------------------- validation
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}


def validate(value, schema=SCHEMA, path="$") -> list[str]:
    """Return a list of problems where value does not match the verdict schema."""
    t = schema["type"]
    if not isinstance(value, _TYPES[t]) or (t == "integer" and isinstance(value, bool)):
        return [f"{path}: expected {t}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: '{value}' not in {schema['enum']}")
    if t == "object":
        errors += [f"{path}.{k}: missing" for k in schema["required"] if k not in value]
        for k, sub in schema["properties"].items():
            if k in value:
                errors += validate(value[k], sub, f"{path}.{k}")
    if t == "array":
        for i, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


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


# ---------------------------------------------------------------- HTML report
_TONE = {
    "SUPPORTED": "good", "PARTLY_SUPPORTED": "warn", "MIXED": "warn",
    "UNSUPPORTED": "bad", "CONTRADICTED": "bad",
    "UNVERIFIED": "muted", "INSUFFICIENT_EVIDENCE": "muted", "NOT_CHECKABLE": "muted",
    "FACT": "info", "INFERENCE": "accent", "JUDGEMENT": "muted",
}

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--sub:#5b6474;--line:#e3e6eb;
--good:#1f7a4d;--good-bg:#e3f4ea;--warn:#8a5a00;--warn-bg:#fdf1d8;--bad:#a8323a;--bad-bg:#fbe5e6;
--muted:#4f5868;--muted-bg:#eceef2;--info:#1f5fa8;--info-bg:#e2edfa;--accent:#6b3fa8;--accent-bg:#efe6fa}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#12151b;--card:#1b1f27;--ink:#e7eaf0;
--sub:#9aa3b2;--line:#2c323d;--good:#7fd6a5;--good-bg:#173a28;--warn:#f0c46c;--warn-bg:#3d3015;--bad:#f2959b;
--bad-bg:#432024;--muted:#b4bccb;--muted-bg:#2a303b;--info:#8db9f0;--info-bg:#1b2d45;
--accent:#c3a2f0;--accent-bg:#2e2342}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.05rem;letter-spacing:.06em;text-transform:uppercase;color:var(--sub);margin:0 0 12px}
h2{font-size:1.15rem;margin:0 0 12px}
section,.hero{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-top:16px}
.hero{margin-top:0;display:grid;gap:14px}
.verdict{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.verdict .pill{font-size:1.25rem;padding:8px 16px}
.summary{font-size:1.08rem;margin:0}
.restate{color:var(--sub);margin:0}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:.8rem;white-space:nowrap}
.good{color:var(--good);background:var(--good-bg)}.warn{color:var(--warn);background:var(--warn-bg)}
.bad{color:var(--bad);background:var(--bad-bg)}.muted{color:var(--muted);background:var(--muted-bg)}
.info{color:var(--info);background:var(--info-bg)}.accent{color:var(--accent);background:var(--accent-bg)}
.meter{display:inline-flex;align-items:center;gap:8px;color:var(--sub);font-size:.9rem}
.meter i{display:inline-block;width:22px;height:8px;border-radius:4px;background:var(--line)}
.meter i.on{background:var(--ink)}
.alert{border-radius:12px;padding:12px 16px;background:var(--warn-bg);color:var(--warn);font-weight:600}
.stats{display:flex;flex-wrap:wrap;gap:8px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:6px 12px;font-size:.9rem}
.stat b{font-size:1.05rem}
table{width:100%;border-collapse:collapse;font-size:.94rem}
th{text-align:left;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--sub);
border-bottom:1px solid var(--line);padding:8px}
td{border-bottom:1px solid var(--line);padding:10px 8px;vertical-align:top}
tr:last-child td{border-bottom:0}
td.why{color:var(--sub);font-size:.88rem}
.core{font-size:.75rem;color:var(--sub)}
a.ref{color:var(--info);text-decoration:none;font-weight:600}
ul{margin:0;padding-left:20px}li{margin:4px 0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.cols section{margin-top:0}
ol.sources{padding-left:24px;word-break:break-all}ol.sources a{color:var(--info)}
footer{color:var(--sub);font-size:.82rem;margin-top:20px;text-align:center}
@media (max-width:700px){
.cols{grid-template-columns:1fr}
table.stack thead{display:none}
table.stack tr{display:block;border-bottom:1px solid var(--line);padding:8px 0}
table.stack td{display:block;border:0;padding:4px 0}
table.stack td[data-k]::before{content:attr(data-k);display:block;font-size:.72rem;text-transform:uppercase;
letter-spacing:.05em;color:var(--sub)}}
"""


def render_html(d: dict, title: str = "Unbiased Evaluation") -> str:
    """Render the enforced verdict as a standalone, presentable HTML page (no external assets)."""
    from datetime import date
    from html import escape as e

    refs: dict[str, int] = {}

    def ref(src: str) -> str:
        if src == "input":
            return '<span class="pill muted">input</span>'
        if not src.startswith("https://"):
            return e(src)
        n = refs.setdefault(src, len(refs) + 1)
        return f'<a class="ref" href="{e(src)}" target="_blank" rel="noopener" title="{e(src)}">[{n}]</a>'

    def pill(v: str) -> str:
        return f'<span class="pill {_TONE.get(v, "muted")}">{e(v.replace("_", " "))}</span>'

    def meter(level: str) -> str:
        on = CONF.index(level) + 1
        return ('<span class="meter">' + "".join(f'<i class="{"on" if i < on else ""}"></i>' for i in range(3))
                + f" {e(level.title())}</span>")

    def ul(items) -> str:
        return "<ul>" + "".join(f"<li>{e(str(x))}</li>" for x in items) + "</ul>" if items else "<p>None found.</p>"

    def evid(items) -> str:
        return "<br>".join(f"{e(x['summary'])} {ref(x['source'])}" for x in items) or "-"

    w, claims = d["whole"], d["claims"]
    counts = {}
    for c in claims:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    stats = "".join(f'<span class="stat"><b>{n}</b> {e(k.replace("_", " ").lower())}</span>'
                    for k, n in counts.items())

    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>'
           f"<style>{_CSS}</style></head><body><main>",
           f'<div class="hero"><h1>{e(title)}</h1>',
           f'<div class="verdict">{pill(w["verdict"])} {meter(w["confidence"])}</div>',
           f'<p class="summary">{e(w["summary"])}</p>']
    if d.get("coincidence_cluster"):
        out.append(f'<div class="alert">Coincidence cluster: {len(d["coincidences"])} coincidences. '
                   "Read this story skeptically.</div>")
    out += [f'<p class="restate"><b>Neutral restatement:</b> {e(d["neutral_restatement"])}</p>',
            f'<div class="stats"><span class="stat"><b>{len(claims)}</b> claims</span>{stats}'
            f'<span class="stat"><b>{len(d["coincidences"])}</b> coincidences</span>'
            f'<span class="stat"><b>{len(d["hypotheses"])}</b> hypotheses</span></div></div>']

    out.append('<section><h2>Parts</h2><table class="stack"><thead><tr><th>ID</th><th>Claim</th>'
               "<th>Verdict</th><th>Confidence</th><th>Evidence for</th><th>Evidence against</th></tr></thead><tbody>")
    for c in claims:
        out.append(
            f'<tr><td data-k="ID"><b>{e(c["id"])}</b>{"<div class=core>core</div>" if c["load_bearing"] else ""}</td>'
            f'<td data-k="Claim">{e(c["text"])}<div style="margin-top:6px">{pill(c["label"])}</div>'
            f'<div class="why" style="margin-top:6px">{e(c["reasoning"])}</div></td>'
            f'<td data-k="Verdict">{pill(c["verdict"])}</td><td data-k="Confidence">{meter(c["confidence"])}</td>'
            f'<td data-k="Evidence for">{evid(c["evidence_for"])}</td>'
            f'<td data-k="Evidence against">{evid(c["evidence_against"])}</td></tr>')
    out.append("</tbody></table></section>")

    if d["coincidences"]:
        out.append('<section><h2>Coincidences</h2><table class="stack"><thead><tr><th>#</th><th>Coincidence</th>'
                   "<th>Claims</th><th>Estimated chance</th><th>Independent</th></tr></thead><tbody>")
        for i, k in enumerate(d["coincidences"], 1):
            out.append(f'<tr><td data-k="#">{i}</td><td data-k="Coincidence">{e(k["description"])}</td>'
                       f'<td data-k="Claims">{e(", ".join(k["claim_ids"]))}</td>'
                       f'<td data-k="Estimated chance">{e(k["estimated_chance"])}</td>'
                       f'<td data-k="Independent">{"Yes" if k["independent_of_others"] else "No"}</td></tr>')
        out.append("</tbody></table></section>")

    out.append('<section><h2>Competing hypotheses</h2><table class="stack"><thead><tr><th>Rank</th>'
               "<th>Hypothesis</th><th>Evidence against it</th><th>Coincidences needed</th></tr></thead><tbody>")
    for h in sorted(d["hypotheses"], key=lambda h: h["rank"]):
        out.append(f'<tr><td data-k="Rank"><b>{h["rank"]}</b></td>'
                   f'<td data-k="Hypothesis"><b>{e(h["name"])}</b><div class="why">{e(h["explanation"])}</div></td>'
                   f'<td data-k="Evidence against it">{ul(h["inconsistent_evidence"])}</td>'
                   f'<td data-k="Coincidences needed">{h["depends_on_coincidences"]}</td></tr>')
    out.append("</tbody></table></section>")

    b = d["bias_checks"]
    out += ['<div class="cols" style="margin-top:16px">',
            f'<section><h2>Bias checks</h2><p><b>Loaded language</b></p>{ul(b["loaded_language"])}'
            f'<p><b>Missing perspectives</b></p>{ul(b["missing_perspectives"])}'
            f'<p><b>Source independence</b></p><p>{e(b["source_independence"])}</p></section>',
            f'<section><h2>Whole-picture issues</h2>{ul(w["coherence_issues"])}'
            f'<h2 style="margin-top:16px">What would change this verdict</h2>{ul(w["what_would_change_my_mind"])}'
            "</section></div>"]
    if d.get("enforcement_notes"):
        out.append(f'<section><h2>Rule overrides</h2>{ul(d["enforcement_notes"])}</section>')
    if refs:
        out.append('<section><h2>Sources</h2><ol class="sources">' + "".join(
            f'<li><a href="{e(u)}" target="_blank" rel="noopener">{e(u)}</a></li>' for u in refs) + "</ol></section>")
    out.append(f"<footer>Generated {date.today().isoformat()} by the Unbiased Evaluator skill. "
               "Rules limit overconfidence; they do not remove all bias.</footer></main></body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Apply evaluator rules and render the verdict grid.")
    p.add_argument("verdict", help="Verdict JSON file, or - for stdin.")
    p.add_argument("--retrieved", help="File with one retrieved URL per line.")
    p.add_argument("--offline", action="store_true", help="No web research was done.")
    p.add_argument("--json-out", help="Also write the enforced verdict JSON here.")
    p.add_argument("--html", help="Also write a presentable HTML report here.")
    p.add_argument("--title", default="Unbiased Evaluation", help="Title for the HTML report.")
    a = p.parse_args(argv)
    if not a.offline and not a.retrieved:
        p.error("give --retrieved urls.txt, or --offline if no web research was done")

    raw = sys.stdin.read() if a.verdict == "-" else open(a.verdict, encoding="utf-8").read()
    data = json.loads(raw)
    problems = validate(data)
    if problems:
        sys.stderr.write("Verdict JSON does not match references/verdict-format.md:\n- "
                         + "\n- ".join(problems[:20]) + "\n")
        return 2
    urls = set()
    if a.retrieved:
        with open(a.retrieved, encoding="utf-8") as f:
            urls = {line.strip() for line in f if line.strip()}
    data = enforce(data, urls, a.offline)
    sys.stdout.write(render(data))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(render_html(data, a.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
