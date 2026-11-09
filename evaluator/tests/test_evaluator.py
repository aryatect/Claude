import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import evaluator as ev  # noqa: E402

A, B, C = "https://a.org/x", "https://news.b.com/y", "https://c.net/z"


def claim(cid="C1", label="FACT", verdict="SUPPORTED", conf="HIGH", core=True, ev_for=None, ev_against=None):
    return {"id": cid, "text": "t", "label": label, "load_bearing": core, "verdict": verdict,
            "confidence": conf, "evidence_for": ev_for or [], "evidence_against": ev_against or [],
            "reasoning": "r"}


def case(claims, coincidences=0, hyps=None, verdict="SUPPORTED", conf="HIGH"):
    return {
        "neutral_restatement": "n", "claims": claims,
        "coincidences": [{"description": f"k{i}", "claim_ids": ["C1"], "estimated_chance": "1 in 10",
                          "independent_of_others": True} for i in range(coincidences)],
        "hypotheses": hyps if hyps is not None else [
            {"name": "H1", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 0, "rank": 1},
            {"name": "H2", "explanation": "e", "inconsistent_evidence": ["x"], "depends_on_coincidences": 0, "rank": 2}],
        "bias_checks": {"loaded_language": [], "missing_perspectives": [], "source_independence": "ok"},
        "whole": {"verdict": verdict, "confidence": conf, "summary": "s", "coherence_issues": [],
                  "what_would_change_my_mind": ["m"]},
    }


def src(*urls):
    return [{"summary": "s", "source": u} for u in urls]


def test_well_supported_case_is_untouched():
    d = ev.enforce(case([claim(ev_for=src(A, B))]), {A, B}, offline=False)
    assert d["whole"] == {**d["whole"], "verdict": "SUPPORTED", "confidence": "HIGH"}
    assert d["enforcement_notes"] == []


def test_unretrieved_url_is_dropped_and_fact_becomes_unverified():
    d = ev.enforce(case([claim(ev_for=src("https://made-up.com/p"))]), {A}, offline=False)
    c = d["claims"][0]
    assert c["evidence_for"] == [] and c["verdict"] == "UNVERIFIED" and c["confidence"] == "LOW"
    assert d["whole"]["verdict"] == "PARTLY_SUPPORTED" and d["whole"]["confidence"] == "LOW"


def test_offline_fact_cannot_be_supported():
    d = ev.enforce(case([claim(ev_for=src("input"))]), set(), offline=True)
    assert d["claims"][0]["verdict"] == "UNVERIFIED"
    assert "offline" in d["enforcement_notes"][0]


def test_single_domain_caps_high_confidence():
    d = ev.enforce(case([claim(ev_for=src(A, "https://www.a.org/other"))]), {A, "https://www.a.org/other"}, False)
    assert d["claims"][0]["confidence"] == "MEDIUM"


def test_counter_evidence_caps_confidence():
    d = ev.enforce(case([claim(ev_for=src(A, B), ev_against=src(C))]), {A, B, C}, False)
    assert d["claims"][0]["confidence"] == "MEDIUM"


def test_contradicted_core_claim_breaks_the_whole():
    d = ev.enforce(case([claim(ev_for=src(A, B)), claim("C2", verdict="CONTRADICTED", ev_against=src(C))]),
                   {A, B, C}, False)
    assert d["whole"]["verdict"] == "UNSUPPORTED"


def test_non_core_claim_does_not_drag_whole():
    d = ev.enforce(case([claim(ev_for=src(A, B)), claim("C2", verdict="CONTRADICTED", core=False)]),
                   {A, B}, False)
    assert d["whole"]["verdict"] == "SUPPORTED"


def test_one_coincidence_is_normal():
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=1), {A, B}, False)
    assert d["coincidence_cluster"] is False and d["whole"]["confidence"] == "HIGH"


def test_two_coincidences_trigger_skepticism():
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=2), {A, B}, False)
    assert d["coincidence_cluster"] is True and d["whole"]["confidence"] == "MEDIUM"


def test_top_hypothesis_needing_coincidences_gets_low_confidence():
    hyps = [{"name": "Chance", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 3, "rank": 1},
            {"name": "Common cause", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 0,
             "rank": 2}]
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=3, hyps=hyps), {A, B}, False)
    assert d["whole"]["confidence"] == "LOW"


def test_single_hypothesis_gets_low_confidence():
    hyps = [{"name": "H1", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 0, "rank": 1}]
    d = ev.enforce(case([claim(ev_for=src(A, B))], hyps=hyps), {A, B}, False)
    assert d["whole"]["confidence"] == "LOW"


def test_render_produces_grid_and_cluster_warning():
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=2), {A, B}, False)
    md = ev.render(copy.deepcopy(d))
    assert "| ID | Claim |" in md and "Coincidence cluster" in md and "## Rule overrides" in md


def test_schema_is_strict():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(ev.SCHEMA)
