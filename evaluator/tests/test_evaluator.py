import copy
import importlib.util
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(__file__)
SKILL_SCRIPT = os.path.join(HERE, "..", "..", ".claude", "skills", "unbiased-evaluator", "scripts", "enforce.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The standalone agent and the skill's script must apply identical rules.
MODULES = [_load("evaluator", os.path.join(HERE, "..", "evaluator.py"))]
if os.path.exists(SKILL_SCRIPT):
    MODULES.append(_load("skill_enforce", SKILL_SCRIPT))


@pytest.fixture(params=MODULES, ids=lambda m: m.__name__)
def ev(request):
    return request.param

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


def test_well_supported_case_is_untouched(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B))]), {A, B}, offline=False)
    assert d["whole"] == {**d["whole"], "verdict": "SUPPORTED", "confidence": "HIGH"}
    assert d["enforcement_notes"] == []


def test_unretrieved_url_is_dropped_and_fact_becomes_unverified(ev):
    d = ev.enforce(case([claim(ev_for=src("https://made-up.com/p"))]), {A}, offline=False)
    c = d["claims"][0]
    assert c["evidence_for"] == [] and c["verdict"] == "UNVERIFIED" and c["confidence"] == "LOW"
    assert d["whole"]["verdict"] == "PARTLY_SUPPORTED" and d["whole"]["confidence"] == "LOW"


def test_offline_fact_cannot_be_supported(ev):
    d = ev.enforce(case([claim(ev_for=src("input"))]), set(), offline=True)
    assert d["claims"][0]["verdict"] == "UNVERIFIED"
    assert "offline" in d["enforcement_notes"][0]


def test_single_domain_caps_high_confidence(ev):
    d = ev.enforce(case([claim(ev_for=src(A, "https://www.a.org/other"))]), {A, "https://www.a.org/other"}, False)
    assert d["claims"][0]["confidence"] == "MEDIUM"


def test_counter_evidence_caps_confidence(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B), ev_against=src(C))]), {A, B, C}, False)
    assert d["claims"][0]["confidence"] == "MEDIUM"


def test_contradicted_core_claim_breaks_the_whole(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B)), claim("C2", verdict="CONTRADICTED", ev_against=src(C))]),
                   {A, B, C}, False)
    assert d["whole"]["verdict"] == "UNSUPPORTED"


def test_non_core_claim_does_not_drag_whole(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B)), claim("C2", verdict="CONTRADICTED", core=False)]),
                   {A, B}, False)
    assert d["whole"]["verdict"] == "SUPPORTED"


def test_one_coincidence_is_normal(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=1), {A, B}, False)
    assert d["coincidence_cluster"] is False and d["whole"]["confidence"] == "HIGH"


def test_two_coincidences_trigger_skepticism(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=2), {A, B}, False)
    assert d["coincidence_cluster"] is True and d["whole"]["confidence"] == "MEDIUM"


def test_top_hypothesis_needing_coincidences_gets_low_confidence(ev):
    hyps = [{"name": "Chance", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 3, "rank": 1},
            {"name": "Common cause", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 0,
             "rank": 2}]
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=3, hyps=hyps), {A, B}, False)
    assert d["whole"]["confidence"] == "LOW"


def test_single_hypothesis_gets_low_confidence(ev):
    hyps = [{"name": "H1", "explanation": "e", "inconsistent_evidence": [], "depends_on_coincidences": 0, "rank": 1}]
    d = ev.enforce(case([claim(ev_for=src(A, B))], hyps=hyps), {A, B}, False)
    assert d["whole"]["confidence"] == "LOW"


def test_render_produces_grid_and_cluster_warning(ev):
    d = ev.enforce(case([claim(ev_for=src(A, B))], coincidences=2), {A, B}, False)
    md = ev.render(copy.deepcopy(d))
    assert "| ID | Claim |" in md and "Coincidence cluster" in md and "## Rule overrides" in md


def test_schema_is_strict(ev):
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(ev.SCHEMA)


@pytest.mark.skipif(not os.path.exists(SKILL_SCRIPT), reason="skill not present")
def test_skill_script_cli(tmp_path):
    verdict = case([claim(ev_for=src(A, "https://made-up.com/p"))], coincidences=2)
    (tmp_path / "v.json").write_text(json.dumps(verdict))
    (tmp_path / "urls.txt").write_text(A + "\n")
    out = subprocess.run([sys.executable, SKILL_SCRIPT, str(tmp_path / "v.json"), "--retrieved",
                          str(tmp_path / "urls.txt")], capture_output=True, text=True, check=True).stdout
    assert "# Verdict: SUPPORTED (confidence MEDIUM)" in out and "made-up.com" in out and "Coincidence cluster" in out


@pytest.mark.skipif(not os.path.exists(SKILL_SCRIPT), reason="skill not present")
def test_skill_script_rejects_bad_json(tmp_path):
    bad = case([claim()])
    bad["claims"][0]["verdict"] = "TRUE"
    del bad["whole"]["summary"]
    (tmp_path / "v.json").write_text(json.dumps(bad))
    r = subprocess.run([sys.executable, SKILL_SCRIPT, str(tmp_path / "v.json"), "--offline"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "'TRUE' not in" in r.stderr and "summary: missing" in r.stderr


@pytest.mark.skipif(not os.path.exists(SKILL_SCRIPT), reason="skill not present")
def test_skill_html_report(tmp_path):
    verdict = case([claim(ev_for=src(A, B), ev_against=src("input"))], coincidences=2)
    verdict["claims"][0]["text"] = "<script>alert(1)</script>"
    (tmp_path / "v.json").write_text(json.dumps(verdict))
    (tmp_path / "urls.txt").write_text(A + "\n" + B + "\n")
    subprocess.run([sys.executable, SKILL_SCRIPT, str(tmp_path / "v.json"), "--retrieved", str(tmp_path / "urls.txt"),
                    "--html", str(tmp_path / "r.html"), "--title", "Test & check"], check=True, capture_output=True)
    page = (tmp_path / "r.html").read_text()
    assert page.startswith("<!doctype html>") and "<title>Test &amp; check</title>" in page
    assert "<script>" not in page and "&lt;script&gt;" in page          # user text is escaped
    assert "Coincidence cluster" in page and "prefers-color-scheme:dark" in page
    assert f'href="{A}"' in page and ">[1]</a>" in page and ">[2]</a>" in page   # numbered sources
    assert "http://" not in page.replace("http://www.w3.org", "")       # no external assets
