import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import newsletter as nl  # noqa: E402

PRIMARY, PRESS = nl.load_sources()


def item(label="FACT", sources=None, **kw):
    base = {"headline": "H", "summary": "S", "label": label, "why_label": "",
            "sources": sources if sources is not None else [
                {"title": "R", "url": "https://www.reuters.com/a"},
                {"title": "V", "url": "https://www.theverge.com/b"}]}
    return {**base, **kw}


def run(data, retrieved=frozenset()):
    return nl.validate(data, PRIMARY, PRESS, set(retrieved))


def test_fact_with_two_independent_sources_kept():
    out = run({"news": [item()]})
    assert out["news"][0]["label"] == "FACT"


def test_fact_with_single_press_source_downgraded_to_rumor():
    out = run({"news": [item(sources=[{"title": "R", "url": "https://reuters.com/a"}])]})
    assert out["news"][0]["label"] == "RUMOR"


def test_fact_with_single_primary_source_kept():
    out = run({"news": [item(sources=[{"title": "A", "url": "https://www.anthropic.com/news/x"}])]})
    assert out["news"][0]["label"] == "FACT"


def test_non_allowlisted_and_http_sources_dropped_and_item_removed():
    bad = [{"title": "x", "url": "https://random-blog.example/a"}, {"title": "y", "url": "http://reuters.com/a"}]
    with pytest.raises(RuntimeError):
        run({"news": [item(sources=bad)]})


def test_lookalike_domain_rejected():
    with pytest.raises(RuntimeError):
        run({"news": [item(sources=[{"title": "x", "url": "https://notreuters.com/a"}])]})


def test_source_not_returned_by_search_is_dropped():
    with pytest.raises(RuntimeError):
        run({"news": [item()]}, retrieved={"https://www.reuters.com/other"})


def test_judgment_spelling_and_invalid_label():
    assert run({"news": [item(label="judgment")]})["news"][0]["label"] == "JUDGEMENT"
    assert run({"news": [item(label="MAYBE")]})["news"][0]["label"] == "RUMOR"


def test_buzzword_requires_valid_source():
    data = {"buzzwords": [
        {"term": "RAG", "meaning": "m", "source_title": "t", "source_url": "https://arxiv.org/abs/1"},
        {"term": "Nope", "meaning": "m", "source_title": "t", "source_url": "https://spam.example/x"}],
        "news": []}
    assert [b["term"] for b in run(data)["buzzwords"]] == ["RAG"]


def test_parse_json_strips_prose_and_fences():
    assert nl.parse_json('Here:\n```json\n{"a": 1}\n```') == {"a": 1}


def test_render_escapes_and_shows_sources_and_labels():
    content = run({"news": [item(headline="<b>x</b>")], "buzzwords": []})
    out = nl.render_html(content, date(2026, 9, 29))
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "reuters.com" in out and "FACT" in out
    assert "https://www.reuters.com/a" in nl.render_text(content, date(2026, 9, 29))
