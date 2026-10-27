"""Validate, test and audit a retail-beauty-specialist claims ledger.

Usage:
    python audit_claims.py research/<run>/claims.json [--offline] [--strict]
                           [--max-age-days 180] [--out audit.md]

Exit code 1 when any check FAILs (or, with --strict, is UNVERIFIABLE).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
TIERS_PATH = HERE.parent / "references" / "source-tiers.json"

LABELS = ("FACT", "INFERENCE", "JUDGEMENT")
STATUSES = ("FOUND", "FEATURE_NOT_FOUND", "INFO_NOT_AVAILABLE")
CONFIDENCES = ("HIGH", "MEDIUM", "LOW", "N/A")
RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
BLOCKED = {401, 403, 429}  # bot walls / paywalls: cannot verify, not proof of a dead link

PASS, FAIL, WARN, UNVERIFIABLE = "PASS", "FAIL", "WARN", "UNVERIFIABLE"


# ---------------------------------------------------------------- tiers
def load_tiers(path: Path = TIERS_PATH) -> dict:
    return json.loads(Path(path).read_text())


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def tier_of(url: str, entity: str, tiers: dict) -> str:
    """Tier a source URL. The claim entity's own site is always LOW (self-published)."""
    host = _host(url)
    if _in(host, tiers.get("entity_domains", {}).get(entity, [])):
        return "LOW"
    for tier in ("HIGH", "MEDIUM", "LOW"):
        if _in(host, tiers.get(tier, [])):
            return tier
    return "LOW"


def is_paywalled(url: str, tiers: dict) -> bool:
    return _in(_host(url), tiers.get("PAYWALLED", []))


# ---------------------------------------------------------------- offline checks
def check_schema(c: dict) -> list[str]:
    errs = []
    for f in ("id", "label", "statement", "status", "confidence"):
        if not str(c.get(f, "")).strip():
            errs.append(f"missing '{f}'")
    if c.get("label") not in LABELS:
        errs.append(f"label must be one of {LABELS}")
    if c.get("status") not in STATUSES:
        errs.append(f"status must be one of {STATUSES}")
    if c.get("confidence") not in CONFIDENCES:
        errs.append(f"confidence must be one of {CONFIDENCES}")
    for i, s in enumerate(c.get("sources") or []):
        if not str(s.get("url", "")).startswith(("http://", "https://")):
            errs.append(f"source[{i}] has no http(s) url")
        if not s.get("retrieved"):
            errs.append(f"source[{i}] missing 'retrieved' date")
    return errs


def check_status(c: dict) -> list[str]:
    errs, status, sources = [], c.get("status"), c.get("sources") or []
    if status == "FOUND":
        if c.get("label") == "FACT" and not sources:
            errs.append("FACT with status FOUND needs at least one source (no link, no claim)")
        for i, s in enumerate(sources):
            if c.get("label") == "FACT" and not str(s.get("quote", "")).strip():
                errs.append(f"source[{i}] needs a supporting 'quote'")
    else:
        if not c.get("search_log"):
            errs.append(f"{status} needs a 'search_log' of queries/URLs checked")
    if status == "FEATURE_NOT_FOUND" and not sources:
        errs.append("FEATURE_NOT_FOUND needs the authoritative source(s) that were checked")
    if status == "INFO_NOT_AVAILABLE" and c.get("confidence") != "N/A":
        errs.append("INFO_NOT_AVAILABLE must have confidence 'N/A'")
    if status != "INFO_NOT_AVAILABLE" and c.get("confidence") == "N/A":
        errs.append(f"{status} needs a HIGH/MEDIUM/LOW confidence")
    return errs


def allowed_confidence(c: dict, by_id: dict, tiers: dict) -> tuple[str | None, list[str]]:
    """Highest confidence the evidence allows, or None when not applicable."""
    if c.get("label") == "FACT":
        if not c.get("sources"):
            return None, []
        best = max((tier_of(s["url"], c.get("entity", ""), tiers) for s in c["sources"]
                    if s.get("url")), key=RANK.get, default="LOW")
        return best, []
    basis = c.get("basis") or []
    if not basis:
        return None, [f"{c.get('label')} needs a 'basis' list of claim IDs it rests on"]
    errs, levels = [], []
    for b in basis:
        ref = by_id.get(b)
        if ref is None:
            errs.append(f"basis '{b}' does not exist")
        elif ref.get("status") == "INFO_NOT_AVAILABLE":
            errs.append(f"basis '{b}' is INFO_NOT_AVAILABLE and cannot support a conclusion")
        elif ref.get("confidence") in RANK:
            levels.append(ref["confidence"])
    return (min(levels, key=RANK.get) if levels else None), errs


def check_confidence(c: dict, by_id: dict, tiers: dict) -> list[str]:
    cap, errs = allowed_confidence(c, by_id, tiers)
    stated = c.get("confidence")
    if cap and stated in RANK and RANK[stated] > RANK[cap]:
        why = "best source tier" if c.get("label") == "FACT" else "weakest basis claim"
        errs.append(f"confidence {stated} exceeds {why} ({cap})")
    return errs


def check_staleness(c: dict, today: date, max_age: int) -> list[str]:
    warns = []
    for i, s in enumerate(c.get("sources") or []):
        try:
            age = (today - date.fromisoformat(str(s.get("retrieved")))).days
        except ValueError:
            continue
        if age > max_age:
            warns.append(f"source[{i}] retrieved {age} days ago (> {max_age})")
    return warns


# ---------------------------------------------------------------- online checks
def _page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    return html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))


def normalize(text: str) -> str:
    text = text.lower().translate(str.maketrans({"‘": "'", "’": "'", "“": '"',
                                                 "”": '"', "–": "-", "—": "-",
                                                 " ": " "}))
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, timeout: int = 20) -> tuple[int, str]:
    """Return (http_status, page_text). status 0 means the request itself failed."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (claims-audit)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
            return r.status, _page_text(body)
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def check_source_online(s: dict, tiers: dict, fetcher=fetch) -> list[tuple[str, str, str]]:
    url, rows = s["url"], []
    status, text = fetcher(url)
    paywalled = s.get("paywalled") or is_paywalled(url, tiers)
    if status in BLOCKED or (status == 0 and paywalled):
        return [("link", UNVERIFIABLE, f"{url} blocked/paywalled (HTTP {status})")]
    if status == 0:
        return [("link", UNVERIFIABLE, f"{url} unreachable: {text[:80]}")]
    if status >= 400:
        return [("link", FAIL, f"{url} returned HTTP {status}")]
    rows.append(("link", PASS, f"{url} HTTP {status}"))
    quote = str(s.get("quote", "")).strip()
    if quote:
        if normalize(quote) in normalize(text):
            rows.append(("quote", PASS, f"quote found on {url}"))
        elif paywalled:
            rows.append(("quote", UNVERIFIABLE, f"quote not in public text of paywalled {url}"))
        else:
            rows.append(("quote", FAIL, f"quote no longer on {url}"))
    return rows


# ---------------------------------------------------------------- audit
def audit(ledger: dict, tiers: dict, today: date, max_age: int = 180,
          offline: bool = True, fetcher=fetch) -> list[tuple[str, str, str, str]]:
    """Return rows of (claim_id, check, result, detail)."""
    claims = ledger.get("claims") or []
    rows: list[tuple[str, str, str, str]] = []
    by_id: dict = {}
    for c in claims:
        cid = str(c.get("id", "?"))
        if cid in by_id:
            rows.append((cid, "schema", FAIL, "duplicate claim id"))
        by_id[cid] = c
    if not claims:
        rows.append(("-", "schema", FAIL, "ledger has no claims"))

    for c in claims:
        cid = str(c.get("id", "?"))
        schema = check_schema(c)
        rows += [(cid, "schema", FAIL, e) for e in schema] or [(cid, "schema", PASS, "")]
        if schema:
            continue
        for check, errs in (("status", check_status(c)),
                            ("confidence", check_confidence(c, by_id, tiers))):
            rows += [(cid, check, FAIL, e) for e in errs] or [(cid, check, PASS, "")]
        rows += [(cid, "staleness", WARN, w) for w in check_staleness(c, today, max_age)]
        if not offline:
            for s in c.get("sources") or []:
                rows += [(cid, chk, res, det) for chk, res, det in
                         check_source_online(s, tiers, fetcher)]
    return rows


def failed(rows, strict: bool = False) -> bool:
    bad = {FAIL, UNVERIFIABLE} if strict else {FAIL}
    return any(r[2] in bad for r in rows)


ICON = {PASS: "✅", FAIL: "❌", WARN: "⚠️", UNVERIFIABLE: "🔒"}


def render(rows, ledger: dict, today: date, offline: bool) -> str:
    counts = {k: sum(1 for r in rows if r[2] == k) for k in ICON}
    verdict = "❌ FAILED" if counts[FAIL] else "✅ PASSED"
    out = [f"# Claims audit: {verdict}", "",
           f"- **Question:** {ledger.get('question', '-')}",
           f"- **Audited:** {today.isoformat()} ({'offline rule checks' if offline else 'rules + live links/quotes'})",
           f"- **Claims:** {len(ledger.get('claims') or [])}", "",
           "| Result | Count |", "|---|---|"]
    out += [f"| {ICON[k]} {k} | {v} |" for k, v in counts.items()]
    issues = [r for r in rows if r[2] != PASS]
    out += ["", "## Issues" if issues else "## Issues: none", ""]
    if issues:
        out += ["| Claim | Check | Result | Detail |", "|---|---|---|---|"]
        out += [f"| {cid} | {chk} | {ICON[res]} {res} | {det.replace('|', '/')} |"
                for cid, chk, res, det in issues]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("ledger", type=Path)
    p.add_argument("--offline", action="store_true", help="rule checks only, no web requests")
    p.add_argument("--strict", action="store_true", help="treat UNVERIFIABLE as failure")
    p.add_argument("--max-age-days", type=int, default=180)
    p.add_argument("--tiers", type=Path, default=TIERS_PATH)
    p.add_argument("--out", type=Path, help="write audit markdown here (default: audit.md next to ledger)")
    p.add_argument("--today", type=date.fromisoformat, default=date.today())
    a = p.parse_args(argv)

    ledger = json.loads(a.ledger.read_text())
    rows = audit(ledger, load_tiers(a.tiers), a.today, a.max_age_days, a.offline)
    report = render(rows, ledger, a.today, a.offline)
    out = a.out or a.ledger.with_name("audit.md")
    out.write_text(report)
    print(report)
    print(f"Audit written to {out}")
    return 1 if failed(rows, a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
