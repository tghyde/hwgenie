"""Quote bank: store, epigraph extraction, course import, web API."""

import json

import pytest

from hwgenie.quotebank import (
    QuoteBank,
    QuoteBankError,
    api_get,
    api_post,
    epigraph_tex,
    extract_epigraphs,
    import_course,
    render_quotes,
)


@pytest.fixture
def bank(tmp_path):
    return QuoteBank(tmp_path / "quotes.json")


# ------------------------------------------------------------------ store --

def test_add_save_roundtrip(bank):
    q = bank.add("To be, or not to be.", "\\emph{Hamlet}")
    bank.add_use(q["id"], "Math 261", "Fall 2025", "ps03")
    bank.save()
    again = QuoteBank(bank.path)
    assert len(again.quotes) == 1
    got = again.quotes[0]
    assert got["text"] == "To be, or not to be."
    assert got["uses"] == [{"course": "Math 261", "semester": "Fall 2025",
                            "doc": "ps03"}]
    assert got["created"] and got["modified"]


def test_missing_file_is_empty(tmp_path):
    assert QuoteBank(tmp_path / "nope.json").quotes == []


def test_corrupt_file_raises_not_clobbers(tmp_path):
    p = tmp_path / "quotes.json"
    p.write_text("{broken")
    with pytest.raises(QuoteBankError):
        QuoteBank(p)
    assert p.read_text() == "{broken"


def test_update_and_delete(bank):
    q = bank.add("Original", "Someone")
    bank.update(q["id"], "Fixed", "Someone Else")
    assert bank.get(q["id"])["text"] == "Fixed"
    with pytest.raises(QuoteBankError):
        bank.update(q["id"], "   ", "x")
    bank.delete(q["id"])
    assert bank.quotes == []
    with pytest.raises(QuoteBankError):
        bank.get(q["id"])


def test_add_use_dedupes_and_remove(bank):
    q = bank.add("Quote", "Src")
    bank.add_use(q["id"], "Math 261", "Fall 2025", "ps03")
    bank.add_use(q["id"], "Math 261", "Fall 2025", "ps03")
    assert len(q["uses"]) == 1
    bank.add_use(q["id"], "Math 261", "Fall 2025", "ps04")
    assert len(q["uses"]) == 2
    bank.remove_use(q["id"], 0)
    assert q["uses"] == [{"course": "Math 261", "semester": "Fall 2025",
                          "doc": "ps04"}]
    with pytest.raises(QuoteBankError):
        bank.remove_use(q["id"], 5)
    with pytest.raises(QuoteBankError):
        bank.add_use(q["id"], "", "  ", "")


def test_find_text_ignores_whitespace(bank):
    bank.add("A line\nbroken   here", "S")
    assert bank.find_text("A line broken here") is not None
    assert bank.find_text("something else") is None


def test_epigraph_tex():
    q = {"text": "Hello\\\\world", "source": "\\emph{Book}"}
    assert epigraph_tex(q) == "\\epigraph{Hello\\\\world}{\\emph{Book}}"


# ------------------------------------------------------------- extraction --

def test_extract_simple():
    tex = "\\hwmaketitle\n\\epigraph{The quote.}{The source}\nbody"
    assert extract_epigraphs(tex) == [("The quote.", "The source")]


def test_extract_multiline_and_nested_braces():
    tex = ("\\epigraph{Line one\\\\\nline two}{From \\emph{Content "
           "Nausea} by Parquet Courts}")
    [(text, source)] = extract_epigraphs(tex)
    assert text == "Line one\\\\\nline two"
    assert source == "From \\emph{Content Nausea} by Parquet Courts"


def test_extract_ignores_comments():
    tex = ("% \\epigraph{commented}{away}\n"
           "\\epigraph{real}{one}\n")
    assert extract_epigraphs(tex) == [("real", "one")]


def test_extract_multiple():
    tex = "\\epigraph{a}{b}\ntext\n\\epigraph{c}{d}"
    assert extract_epigraphs(tex) == [("a", "b"), ("c", "d")]


# ----------------------------------------------------------------- import --

def _make_repo(tmp_path):
    repo = tmp_path / "course"
    (repo / "source" / "problem-sets" / "ps01").mkdir(parents=True)
    (repo / "source" / "problem-sets" / "ps02").mkdir(parents=True)
    (repo / "coursedata.tex").write_text(
        "\\renewcommand{\\hwcourse}{Math 261}\n"
        "\\renewcommand{\\hwsemester}{Fall 2025}\n")
    (repo / "source" / "problem-sets" / "ps01" / "ps01.tex").write_text(
        "\\hwmaketitle\n\\epigraph{First quote.}{Author One}\n")
    (repo / "source" / "problem-sets" / "ps02" / "ps02.tex").write_text(
        "\\hwmaketitle\n\\epigraph{Second quote.}{Author Two}\n")
    return repo


def test_import_course(tmp_path, bank):
    repo = _make_repo(tmp_path)
    res = import_course(repo, bank)
    assert res == {"course": "Math 261", "semester": "Fall 2025",
                   "files": 2, "added": 2, "uses": 2}
    docs = {q["uses"][0]["doc"] for q in bank.quotes}
    assert docs == {"ps01", "ps02"}
    # saved to disk
    assert len(QuoteBank(bank.path).quotes) == 2


def test_import_is_idempotent_and_merges_reuse(tmp_path, bank):
    repo = _make_repo(tmp_path)
    import_course(repo, bank)
    res = import_course(repo, bank)
    assert res["added"] == 0 and res["uses"] == 0
    # the same quote in another doc becomes a new use, not a new quote
    (repo / "source" / "problem-sets" / "ps02" / "ps02.tex").write_text(
        "\\epigraph{First  quote.}{Author One}\n")
    res = import_course(repo, bank)
    assert res["added"] == 0 and res["uses"] == 1
    q = QuoteBank(bank.path).find_text("First quote.")
    assert {u["doc"] for u in q["uses"]} == {"ps01", "ps02"}


def test_import_skips_build_and_underscore_dirs(tmp_path, bank):
    repo = _make_repo(tmp_path)
    bld = repo / "source" / "problem-sets" / "ps01" / "build"
    bld.mkdir()
    (bld / "_hwg_handout.tex").write_text("\\epigraph{First quote.}{A}\n")
    exp = repo / "source" / "_experiments"
    exp.mkdir()
    (exp / "psx.tex").write_text("\\epigraph{Experimental}{B}\n")
    res = import_course(repo, bank)
    assert res["files"] == 2 and res["added"] == 2
    assert bank.find_text("Experimental") is None
    q = bank.find_text("First quote.")
    assert [u["doc"] for u in q["uses"]] == ["ps01"]


def test_import_needs_source_dir(tmp_path, bank):
    with pytest.raises(QuoteBankError):
        import_course(tmp_path, bank)


# -------------------------------------------------------------------- api --

def test_api_full_flow(tmp_path):
    path = tmp_path / "quotes.json"
    payload, code = api_post("/quotes/api/save",
                             {"text": "Q", "source": "S"}, path)
    assert code == 200 and payload["ok"]
    qid = payload["quote"]["id"]

    payload, code = api_post("/quotes/api/use",
                             {"id": qid, "course": "Math 261",
                              "semester": "Fall 2025", "doc": "ps05"}, path)
    assert code == 200 and len(payload["quote"]["uses"]) == 1

    payload, code = api_post("/quotes/api/save",
                             {"id": qid, "text": "Q2", "source": "S2"}, path)
    assert payload["quote"]["text"] == "Q2"
    assert len(payload["quote"]["uses"]) == 1   # edit keeps uses

    payload, code = api_get("/quotes/api/list", path)
    assert code == 200 and len(payload["quotes"]) == 1

    payload, code = api_post("/quotes/api/deluse",
                             {"id": qid, "index": 0}, path)
    assert payload["quote"]["uses"] == []

    payload, code = api_post("/quotes/api/delete", {"id": qid}, path)
    assert code == 200
    assert json.loads(path.read_text())["quotes"] == []


def test_api_errors(tmp_path):
    path = tmp_path / "quotes.json"
    payload, code = api_post("/quotes/api/save", {"text": ""}, path)
    assert code == 400 and not payload["ok"]
    payload, code = api_post("/quotes/api/use", {"id": "nope"}, path)
    assert code == 400
    assert api_post("/quotes/api/other", {}, path) is None
    assert api_get("/quotes/api/other", path) is None


def test_render_quotes_page():
    page = render_quotes()
    assert "Quote bank" in page and "/quotes/api/list" in page
