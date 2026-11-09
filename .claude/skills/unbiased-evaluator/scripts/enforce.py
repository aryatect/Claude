"""Apply the Unbiased Evaluator's hard rules to a verdict JSON and print the Markdown report.

Usage
  python enforce.py verdict.json --retrieved urls.txt [--json-out final.json]
  python enforce.py verdict.json --offline
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


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Apply evaluator rules and render the verdict grid.")
    p.add_argument("verdict", help="Verdict JSON file, or - for stdin.")
    p.add_argument("--retrieved", help="File with one retrieved URL per line.")
    p.add_argument("--offline", action="store_true", help="No web research was done.")
    p.add_argument("--json-out", help="Also write the enforced verdict JSON here.")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
