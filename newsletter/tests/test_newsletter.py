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


# ---- no repeats
SEEN = {"buzzwords": ["Agent containment"],
        "news": [{"headline": "Nvidia launches Open Agent Safety Platform", "label": "FACT", "date": "2026-09-29"}]}


def content(news, buzz=()):
    return {"buzzwords": [{"term": t, "meaning": "m", "source": {"title": "x", "url": "https://arxiv.org/a", "host": "arxiv.org"}} for t in buzz],
            "news": news}


def n(headline, changed=""):
    return {"headline": headline, "summary": "s", "label": "FACT", "why_label": "", "what_changed": changed,
            "sources": [{"title": "R", "url": "https://reuters.com/a", "host": "reuters.com"}]}


def test_repeated_buzzword_dropped_case_and_punctuation_insensitive():
    out = nl.drop_repeats(content([n("Brand new story")], buzz=["agent-containment", "Distillation"]), SEEN)
    assert [b["term"] for b in out["buzzwords"]] == ["Distillation"]


def test_repeated_story_dropped_unless_something_changed():
    with pytest.raises(RuntimeError):
        nl.drop_repeats(content([n("Nvidia launches Open Agent Safety Platform for agents")]), SEEN)
    out = nl.drop_repeats(content([n("Nvidia Open Agent Safety Platform launch", "Anthropic and Microsoft join")]), SEEN)
    assert out["news"][0]["update"] == "Anthropic and Microsoft join"


def test_what_changed_ignored_for_new_story():
    out = nl.drop_repeats(content([n("Totally different story", "bogus")]), SEEN)
    assert out["news"][0]["update"] == ""


def test_record_sent_replaces_updated_story():
    c = nl.drop_repeats(content([n("Nvidia Open Agent Safety Platform launch", "more partners")], buzz=["RAG"]),
                        {"buzzwords": [], "news": SEEN["news"]})
    seen = nl.record_sent(c, {"buzzwords": [], "news": list(SEEN["news"])}, date(2026, 9, 30))
    assert len(seen["news"]) == 1 and seen["news"][0]["date"] == "2026-09-30" and seen["buzzwords"] == ["RAG"]


# ---- mobile / theme design
def test_html_is_mobile_and_dark_mode_ready():
    c = content([n("Story", "changed")], buzz=["RAG"])
    c["news"][0].update(update="changed")
    out = nl.render_html(c, date(2026, 9, 30))
    assert 'name="viewport"' in out and "prefers-color-scheme:dark" in out
    assert 'name="color-scheme"' in out and "max-width:560px" in out
    assert "UPDATE" in out and "What changed" in out and "reuters.com" in out


def test_no_crawl_outlets_not_searched():
    assert {"reuters.com", "wired.com"} <= nl.load_no_crawl()


def test_render_json_cli(tmp_path, monkeypatch):
    import subprocess
    raw = {"buzzwords": [{"term": "RAG", "meaning": "m", "source_title": "t", "source_url": "https://arxiv.org/abs/1"}],
           "news": [item(headline="Fresh story")]}
    (tmp_path / "in.json").write_text(json.dumps(raw))
    (tmp_path / "seen.json").write_text(json.dumps({"buzzwords": [], "news": []}))
    r = subprocess.run([sys.executable, str(Path(nl.__file__)), "--render-json", str(tmp_path / "in.json"),
                        "--seen-json", str(tmp_path / "seen.json")], capture_output=True, text=True)
    assert r.returncode == 0 and "Rendered 1 buzzwords, 1 news" in r.stdout
    out = Path(nl.__file__).parent / "out"
    assert "Fresh story" in (out / "latest.html").read_text()
    nxt = json.loads((out / "next_seen.json").read_text())
    assert nxt["buzzwords"] == ["RAG"] and nxt["news"][0]["headline"] == "Fresh story"


def test_one_mail_per_day_guard():
    nl.check_not_sent_today({"last_sent": "2026-09-29"}, date(2026, 9, 30))  # new day: fine
    with pytest.raises(RuntimeError, match="Already sent today"):
        nl.check_not_sent_today({"last_sent": "2026-09-30"}, date(2026, 9, 30))


def test_record_sent_stamps_last_sent():
    seen = nl.record_sent({"buzzwords": [], "news": []}, {"buzzwords": [], "news": []}, date(2026, 9, 30))
    assert seen["last_sent"] == "2026-09-30"
