"""rubric.yml as a whole: defaults from the template, read/write, the
editor API, and collect seeding the parts."""

import json
import zipfile

import pytest

from hwgenie.grade import GradeError, load_rubric
from hwgenie import rubric as rubric_mod
from test_collect import STUDENT_TEX, make_moodle_dir
from test_grade import _start_server
from test_late import _write_ws

PROBLEM_TEMPLATE = "\n".join([
    r"\documentclass[11pt]{article}",
    r"\begin{document}",
    r"\begin{problem}",
    r"Part one. \begin{solution}\end{solution}",
    r"% \begin{solution} a commented-out box \end{solution}",
    r"\begin{solution}",
    r"\end{solution}",
    r"\end{problem}",
    r"\begin{problem}",
    r"Two. \begin{solution}\end{solution}",
    r"\end{problem}",
    r"\end{document}",
])


def _manifest(folder, n_parts, template=None):
    folder.mkdir(parents=True, exist_ok=True)
    m = {"created": "2026-09-01T00:00:00+00:00", "units": [
        {"slug": "Doe-Jane", "moodle_id": "111", "parts_found": n_parts}]}
    if template is not None:
        m["template"] = {"path": str(template), "parts": n_parts}
    (folder / "manifest.json").write_text(json.dumps(m))
    return folder


# ---------------------------------------------------------------- defaults --

def test_template_box_labels(tmp_path):
    t = tmp_path / "ps01.tex"
    t.write_text(PROBLEM_TEMPLATE)
    assert rubric_mod.template_box_labels(t) == ["1.1", "1.2", "2.1"]
    assert rubric_mod.template_box_labels(None) == []
    assert rubric_mod.template_box_labels(tmp_path / "nope.tex") == []
    # boxes outside any problem: plain ordinals
    t2 = tmp_path / "flat.tex"
    t2.write_text(r"\begin{solution}\end{solution}" "\n"
                  r"\begin{solution}\end{solution}")
    assert rubric_mod.template_box_labels(t2) == ["1", "2"]
    # a count mismatch falls back to ordinals
    parts = rubric_mod.default_parts(t, 5)
    assert [p.label for p in parts] == ["1", "2", "3", "4", "5"]
    assert all(p.max == 5 and not p.ec for p in parts)


# -------------------------------------------------------------- read/write --

def test_write_and_seed(tmp_path):
    g = tmp_path / "math221" / "ps03" / "grading"
    g.mkdir(parents=True)
    t = tmp_path / "ps03.tex"
    t.write_text(PROBLEM_TEMPLATE)
    # settings written first (collect --due) survive the seeding
    from hwgenie.late import write_setting
    write_setting(g, "due", "2026-09-11 23:59")
    assert rubric_mod.seed_parts(g, t, 3)
    text = (g / "rubric.yml").read_text()
    assert text.startswith("# Rubric for ps03")
    assert "due: 2026-09-11 23:59\n" in text
    assert text.endswith("parts:\n- 1.1: 5\n- 1.2: 5\n- 2.1: 5\n")
    assert not rubric_mod.seed_parts(g, t, 3)          # already has parts
    rubric = load_rubric(g, 3)
    assert [r.label for r in rubric] == ["1.1", "1.2", "2.1"]
    # a rewrite keeps the header comment, drops a blank deadline, and
    # prints whole-number maxima without ".0"
    from hwgenie.grade import RubricPart
    rubric_mod.write_rubric(g, [RubricPart("1.1", 4.0), RubricPart("1.2", 2.5),
                                RubricPart("2.1", 3, ec=True)],
                            timezone="America/New_York")
    text = (g / "rubric.yml").read_text()
    assert text.startswith("# Rubric for ps03")
    assert "due:" not in text and "timezone: America/New_York\n" in text
    assert text.endswith("parts:\n- 1.1: 4\n- 1.2: 2.5\n- 2.1: 3 ec\n")


def test_payload_and_save(tmp_path):
    t = tmp_path / "ps03.tex"
    t.write_text(PROBLEM_TEMPLATE)
    g = _manifest(tmp_path / "math221" / "ps03" / "grading", 3, t)
    d = rubric_mod.payload(g)
    assert not d["exists"] and d["due"] is None and d["n_parts"] == 3
    assert [p["label"] for p in d["parts"]] == ["1.1", "1.2", "2.1"]
    assert d["default_labels"] == ["1.1", "1.2", "2.1"]
    assert d["problems"] == [{"num": 1, "boxes": [1, 2]},
                             {"num": 2, "boxes": [3]}]
    assert d["name"] == "ps03"
    d = rubric_mod.save(g, {"due": "2026-09-11 23:59",
                            "timezone": "America/New_York",
                            "parts": [{"label": "1.1", "max": "4"},
                                      {"label": "1.2", "max": 2.5},
                                      {"label": "2.1", "max": "", "ec": True}]})
    assert d["exists"] and d["due"] == "2026-09-11 23:59"
    assert d["timezone"] == "America/New_York"
    assert d["parts"] == [{"label": "1.1", "max": 4.0, "ec": False},
                          {"label": "1.2", "max": 2.5, "ec": False},
                          {"label": "2.1", "max": 5.0, "ec": True}]
    assert "- 2.1: 5 ec" in d["text"]
    # a partial file is padded with defaults for the missing parts
    (g / "rubric.yml").write_text("parts:\n- 1.1: 3\n")
    d = rubric_mod.payload(g)
    assert [(p["label"], p["max"]) for p in d["parts"]] == [
        ("1.1", 3.0), ("1.2", 5.0), ("2.1", 5.0)]


@pytest.mark.parametrize("data, msg", [
    ({"parts": [{"label": "1"}]}, "exactly 3 parts"),
    ({"parts": [{"label": ""}, {"label": "b"}, {"label": "c"}]}, "empty"),
    ({"parts": [{"label": "a:b"}, {"label": "b"}, {"label": "c"}]}, "contain"),
    ({"parts": [{"label": "a"}, {"label": "a"}, {"label": "c"}]}, "twice"),
    ({"parts": [{"label": "a", "max": "x"}, {"label": "b"}, {"label": "c"}]},
     "bad max"),
    ({"parts": [{"label": "a", "max": -1}, {"label": "b"}, {"label": "c"}]},
     "bad max"),
    ({"due": "tomorrow", "parts": [{"label": "a"}, {"label": "b"},
                                   {"label": "c"}]}, "cannot parse due"),
    ({"timezone": "Mars/Olympus", "parts": [{"label": "a"}, {"label": "b"},
                                            {"label": "c"}]},
     "unknown timezone"),
])
def test_save_rejects(tmp_path, data, msg):
    g = _manifest(tmp_path / "g", 3)
    with pytest.raises(GradeError, match=msg):
        rubric_mod.save(g, data)
    assert not (g / "rubric.yml").exists()


# --------------------------------------------------------------------- api --

def test_api_rubric_roundtrip(tmp_path):
    from hwgenie.grade_gui import AppHolder
    t = tmp_path / "ps03.tex"
    t.write_text(PROBLEM_TEMPLATE)
    g = _manifest(tmp_path / "math221" / "ps03" / "grading", 3, t)
    holder = AppHolder(root=tmp_path / "math221")
    server, client = _start_server(holder)
    try:
        d = client.get("/api/rubric?folder=" + str(g))
        assert d["ok"] and not d["exists"]
        assert [p["label"] for p in d["parts"]] == ["1.1", "1.2", "2.1"]
        st = client.get("/api/state?folder=" + str(g))
        # unsaved defaults are the editor's suggestion only
        assert [r["label"] for r in st["rubric"]] == ["Part 1", "Part 2",
                                                      "Part 3"]
        assert st["late"]["due"] is None
        d = client.post("/api/rubric", {
            "folder": str(g), "due": "2026-09-11 23:59",
            "timezone": "America/New_York",
            "parts": [{"label": "1.1", "max": 4}, {"label": "1.2", "max": 6},
                      {"label": "2.1", "max": 2, "ec": True}]})
        assert d["ok"] and d["exists"]
        # the open app sees the new rubric and deadline at once
        st = client.get("/api/state?folder=" + str(g))
        assert [(r["label"], r["max"], r["ec"]) for r in st["rubric"]] == [
            ("1.1", 4.0, False), ("1.2", 6.0, False), ("2.1", 2.0, True)]
        assert st["late"]["due"] == "2026-09-11T23:59-04:00"
        assert st["units"][0]["parts"]["3"]["ec"] is True
        r = client.post("/api/rubric", {"folder": str(g), "parts": []},
                        expect=400)
        assert "exactly 3 parts" in r["error"]
        client.get("/api/rubric?folder=" + str(tmp_path), expect=400)
        client.get("/api/rubric", expect=400)          # nothing open
        # the hub page carries the editor; hwGrader links to it
        page = client.get("/grading?pick=1&view=collect").decode()
        assert 'id="sec-rubric"' in page and "Rubric &amp; deadline" in page
        page = client.get("/grading?folder=" + str(g)).decode()
        assert 'id="rubricbtn"' in page
    finally:
        server.shutdown()


def test_api_rubric_locked_on_grader_server(tmp_path):
    from hwgenie.grade_gui import AppHolder
    g = _manifest(tmp_path / "math221" / "ps03" / "grading", 3)
    holder = AppHolder(root=tmp_path / "math221", grader_only=True)
    server, client = _start_server(holder)
    try:
        client.get("/api/rubric?folder=" + str(g), expect=403)
        client.post("/api/rubric", {"folder": str(g), "parts": []},
                    expect=403)
        page = client.get("/grading?folder=" + str(g)).decode()
        assert 'id="rubricbtn"' in page      # hidden client-side for graders
    finally:
        server.shutdown()
    assert not (g / "rubric.yml").exists()


# ----------------------------------------------------------------- collect --

def test_collect_seeds_rubric_parts(tmp_path):
    from hwgenie.collect import collect
    src = make_moodle_dir(tmp_path)
    ps = tmp_path / "math221" / "ps01"
    (ps / "moodle-raw").mkdir(parents=True)
    (ps / "build").mkdir()
    zpath = ps / "moodle-raw" / "MATH-221-PS1.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, str(f.relative_to(src)))
    _write_ws(ps / "moodle-raw" / "Grades-MATH-221-PS1--9.csv", [
        ("111", "Jane Doe", "Friday, September 4, 2026, 10:36 PM"),
        ("222", "Rick Roe", "Saturday, September 5, 2026, 1:15 AM")])
    tmpl = ps / "build" / "PS1-submission.tex"
    tmpl.write_text(PROBLEM_TEMPLATE.replace(
        r"Two. \begin{solution}\end{solution}" "\n", ""))   # 2 boxes
    dest = ps / "grading"
    collect(zpath, dest, template=tmpl, due="2026-09-04 23:59")
    text = (dest / "rubric.yml").read_text()
    assert "due: 2026-09-04 23:59" in text
    assert text.endswith("parts:\n- 1.1: 5\n- 1.2: 5\n")
    # a hand-tuned rubric survives a re-collect
    (dest / "rubric.yml").write_text("due: 2026-09-04 23:59\nparts:\n"
                                     "- 1.1: 3\n- 1.2: 7 ec\n")
    collect(zpath, dest, template=tmpl)
    assert (dest / "rubric.yml").read_text().endswith("- 1.1: 3\n- 1.2: 7 ec\n")
    assert STUDENT_TEX  # (import used: keeps the fixture module loaded)
