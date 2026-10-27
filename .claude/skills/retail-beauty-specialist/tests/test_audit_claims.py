import copy
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import audit_claims as ac  # noqa: E402

TIERS = ac.load_tiers()
SAMPLE = json.loads((ROOT / "examples" / "sample-claims.json").read_text())
TODAY = date(2026, 10, 27)


def run(ledger, offline=True, fetcher=None, **kw):
    return ac.audit(ledger, TIERS, TODAY, offline=offline, fetcher=fetcher or ac.fetch, **kw)


def fails(rows, cid=None, check=None):
    return [r for r in rows if r[2] == ac.FAIL
            and (cid is None or r[0] == cid) and (check is None or r[1] == check)]


def fact(**kw):
    base = {"id": "C-001", "label": "FACT", "statement": "S", "entity": "Ulta Beauty",
            "status": "FOUND", "confidence": "MEDIUM",
            "sources": [{"url": "https://www.retaildive.com/news/x", "quote": "q",
                         "retrieved": "2026-10-20"}]}
    return {**base, **kw}


def ledger(*claims):
    return {"question": "Q", "as_of": "2026-10-27", "claims": list(claims)}


# ---------------------------------------------------------------- tiers
@pytest.mark.parametrize("url,entity,tier", [
    ("https://www.gartner.com/en/doc", "Ulta Beauty", "HIGH"),
    ("https://www.sec.gov/Archives/x", "Ulta Beauty", "HIGH"),
    ("https://www.retaildive.com/news/x", "Ulta Beauty", "MEDIUM"),
    ("https://www.sephora.com/beauty/x", "Ulta Beauty", "MEDIUM"),   # competitor confirms
    ("https://www.ulta.com/x", "Ulta Beauty", "LOW"),                 # self-published
    ("https://newsroom.ulta.com/x", "Ulta Beauty", "LOW"),            # subdomain of own site
    ("https://someblog.example/x", "Ulta Beauty", "LOW"),             # unlisted
    ("https://www.prnewswire.com/x", "Ulta Beauty", "LOW"),
])
def test_tier_of(url, entity, tier):
    assert ac.tier_of(url, entity, TIERS) == tier


# ---------------------------------------------------------------- sample ledger
def test_sample_ledger_passes_offline():
    rows = run(SAMPLE)
    assert not ac.failed(rows), fails(rows)


def test_sample_main_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(SAMPLE))
    assert ac.main([str(good), "--offline", "--today", "2026-10-27"]) == 0
    assert (tmp_path / "audit.md").read_text().startswith("# Claims audit: ✅ PASSED")

    broken = copy.deepcopy(SAMPLE)
    broken["claims"][1]["confidence"] = "HIGH"   # trade press can't be HIGH
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(broken))
    assert ac.main([str(bad), "--offline", "--today", "2026-10-27"]) == 1


# ---------------------------------------------------------------- confidence
def test_fact_overclaim_fails():
    rows = run(ledger(fact(confidence="HIGH")))
    assert fails(rows, "C-001", "confidence")


def test_self_published_source_capped_low():
    c = fact(confidence="MEDIUM", sources=[{"url": "https://www.ulta.com/rewards",
                                            "quote": "q", "retrieved": "2026-10-20"}])
    assert fails(run(ledger(c)), "C-001", "confidence")


def test_underclaim_allowed():
    assert not fails(run(ledger(fact(confidence="LOW"))))


def test_best_source_sets_cap():
    c = fact(confidence="HIGH", sources=[
        {"url": "https://myblog.example/x", "quote": "q", "retrieved": "2026-10-20"},
        {"url": "https://www.sec.gov/x", "quote": "q", "retrieved": "2026-10-20"}])
    assert not fails(run(ledger(c)))


def test_inference_capped_by_weakest_basis():
    hi = fact(id="C-001", confidence="HIGH",
              sources=[{"url": "https://www.nrf.com/x", "quote": "q", "retrieved": "2026-10-20"}])
    lo = fact(id="C-002", confidence="LOW")
    inf = {"id": "C-003", "label": "INFERENCE", "statement": "S", "status": "FOUND",
           "confidence": "HIGH", "basis": ["C-001", "C-002"]}
    assert fails(run(ledger(hi, lo, inf)), "C-003", "confidence")
    inf["confidence"] = "LOW"
    assert not fails(run(ledger(hi, lo, inf)))


def test_inference_needs_existing_basis():
    inf = {"id": "C-003", "label": "JUDGEMENT", "statement": "S", "status": "FOUND",
           "confidence": "LOW"}
    assert fails(run(ledger(inf)), "C-003", "confidence")
    inf["basis"] = ["C-999"]
    assert fails(run(ledger(inf)), "C-003", "confidence")


def test_basis_cannot_be_info_not_available():
    na = {"id": "C-001", "label": "FACT", "statement": "S", "status": "INFO_NOT_AVAILABLE",
          "confidence": "N/A", "search_log": ["q"]}
    inf = {"id": "C-002", "label": "INFERENCE", "statement": "S", "status": "FOUND",
           "confidence": "LOW", "basis": ["C-001"]}
    assert fails(run(ledger(na, inf)), "C-002")


# ---------------------------------------------------------------- status rules
def test_fact_without_source_fails():
    assert fails(run(ledger(fact(sources=[]))), "C-001", "status")


def test_fact_source_needs_quote():
    c = fact(sources=[{"url": "https://www.retaildive.com/x", "retrieved": "2026-10-20"}])
    assert fails(run(ledger(c)), "C-001", "status")


def test_feature_not_found_needs_search_log_and_checked_source():
    c = fact(status="FEATURE_NOT_FOUND", confidence="LOW")
    assert fails(run(ledger(c)), "C-001", "status")            # no search_log
    c["search_log"] = ["feature list"]
    assert not fails(run(ledger(c)))
    c["sources"] = []
    assert fails(run(ledger(c)), "C-001", "status")            # nothing checked


def test_info_not_available_rules():
    c = {"id": "C-001", "label": "FACT", "statement": "S", "status": "INFO_NOT_AVAILABLE",
         "confidence": "LOW", "search_log": ["q"]}
    assert fails(run(ledger(c)), "C-001", "status")            # must be N/A
    c["confidence"] = "N/A"
    assert not fails(run(ledger(c)))
    del c["search_log"]
    assert fails(run(ledger(c)), "C-001", "status")


def test_schema_errors_and_duplicates():
    rows = run(ledger(fact(label="RUMOR"), fact()))
    assert fails(rows, "C-001", "schema")
    assert any("duplicate" in r[3] for r in fails(rows))
    assert fails(run(ledger()), check="schema")


def test_staleness_warns_not_fails():
    rows = run(ledger(fact(sources=[{"url": "https://www.retaildive.com/x", "quote": "q",
                                     "retrieved": "2025-01-01"}])))
    assert any(r[2] == ac.WARN for r in rows)
    assert not ac.failed(rows)


# ---------------------------------------------------------------- online checks
def fetcher(status, text=""):
    return lambda url: (status, text)


def test_online_quote_found_passes():
    page = "Members drive the  majority of sales — per the report."
    c = fact(sources=[{"url": "https://www.retaildive.com/x",
                       "quote": "members drive the majority of sales - per",
                       "retrieved": "2026-10-20"}])
    rows = run(ledger(c), offline=False, fetcher=fetcher(200, page))
    assert not ac.failed(rows, strict=True)


def test_online_dead_link_fails():
    rows = run(ledger(fact()), offline=False, fetcher=fetcher(404))
    assert fails(rows, "C-001", "link")


def test_online_quote_drift_fails():
    rows = run(ledger(fact()), offline=False, fetcher=fetcher(200, "different text"))
    assert fails(rows, "C-001", "quote")


def test_online_paywall_is_unverifiable_not_fail():
    c = fact(confidence="HIGH", sources=[{"url": "https://www.gartner.com/doc/1", "quote": "q",
                                          "retrieved": "2026-10-20"}])
    rows = run(ledger(c), offline=False, fetcher=fetcher(200, "sign in to read"))
    assert not ac.failed(rows)
    assert ac.failed(rows, strict=True)
    rows = run(ledger(fact()), offline=False, fetcher=fetcher(403))
    assert [r[2] for r in rows if r[1] == "link"] == [ac.UNVERIFIABLE]


def test_page_text_strips_markup():
    assert ac.normalize(ac._page_text("<p>A&amp;B</p><script>x()</script>")) == "a&b"
