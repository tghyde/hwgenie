"""The assignment overview: statistics, highlights, the page and its
grader-mode lockdown."""

from pathlib import Path

from test_grade import _start_server, make_grading_folder

from hwgenie.grade_gui import AppHolder, GradingApp
from hwgenie.overview import overview_data, render_overview


def _graded(tmp_path: Path) -> Path:
    f = make_grading_folder(tmp_path)
    app = GradingApp(f)
    # rubric: 1.1/4, 1.2/2.5, 2.1a/5  (base out of 11.5)
    app.store.update("Doe-Jane", 1, {"score": 4, "comments": [
        {"anchor": None, "text": "nice\nwork"}]}, by="Ann")
    app.store.update("Doe-Jane", 2, {"score": 2})
    app.store.update("Doe-Jane", 3, {"score": 0})
    app.store.update("Roe-Rick", 1, {"score": 4})
    app.store.update("Roe-Rick", 2, {"score": 2.5})
    app.store.update("Roe-Rick", 3, {"score": 5})
    app.store.update("Poe-Pat", 1, {"score": 1})        # not fully graded
    return f


def test_overview_data(tmp_path):
    d = overview_data(GradingApp(_graded(tmp_path)))
    assert d["n_units"] == 3 and d["graded_parts"] == 7
    assert d["total_parts"] == 9
    by = {u["slug"]: u for u in d["units"]}
    assert by["Roe-Rick"]["raw"] == 11.5 and by["Roe-Rick"]["pct"] == 1
    assert by["Doe-Jane"]["raw"] == 6 and by["Doe-Jane"]["comments"] == 1
    assert not by["Poe-Pat"]["complete"] and by["Poe-Pat"]["pct"] is None
    assert [u["slug"] for u in d["incomplete"]] == ["Poe-Pat"]
    st = d["stats"]
    assert st["n"] == 2 and st["out_of"] == 11.5
    assert st["mean"] == 8.75 and st["median"] == 8.75
    assert st["min"] == 6 and st["max"] == 11.5 and st["perfect"] == 1
    assert sum(st["hist"]) == 2 and st["hist"][-1] == 1 and st["hist"][5] == 1
    # a two-student class: thresholds only, nobody on both lists
    assert [u["slug"] for u in d["well"]] == ["Roe-Rick"]
    assert [u["slug"] for u in d["poorly"]] == ["Doe-Jane"]
    p1, p2, p3 = d["parts"]
    assert (p1["label"], p1["mean"], p1["scored"]) == ("1.1", 3, 3)
    assert (p1["full"], p1["partial"], p1["zero"]) == (2, 1, 0)
    assert p1["comments"] == 1 and p1["graders"] == ["Ann"]
    assert p3["zero"] == 1 and p3["mean_pct"] == 0.5
    assert d["exported"] == 0


def test_overview_highlights_top_up(tmp_path):
    f = make_grading_folder(tmp_path)
    app = GradingApp(f)
    # nine middling students (all 8/11.5 ≈ 70 %) → three named at each end
    for i in range(9):
        slug = f"Stu-{i}"
        app.units.append({"slug": slug, "moodle_id": str(900 + i),
                          "moodle_folder": slug, "pdf": None, "tex": None,
                          "tex_source": None, "extras": [], "anomalies": [],
                          "sha256": {}, "parts_found": None,
                          "collaborators": None})
        for n, s in ((1, 3), (2, 2), (3, 3 + i * 0.1)):
            app.store.update(slug, n, {"score": s})
    d = overview_data(app)
    assert len(d["well"]) == 3 and len(d["poorly"]) == 3
    assert not set(u["slug"] for u in d["well"]) & \
        set(u["slug"] for u in d["poorly"])


def test_overview_page_and_lockdown(tmp_path):
    f = _graded(tmp_path)
    page = render_overview(GradingApp(f))
    assert "Problem Set" in page or f.name in page
    assert "Not exported yet" in page and "class=\"pie" in page
    assert "svg class=\"hist\"" in page and "Rick Roe" in page
    assert "student=Doe-Jane" in page          # deep link into the grader
    assert "2 students are not fully graded" not in page
    assert "1 student is not fully graded" in page

    holder = AppHolder(tmp_path)
    server, client = _start_server(holder)
    try:
        html = client.get("/overview?folder=" + str(f))
        assert b"Overview" in html and b"Rick Roe" in html
    finally:
        server.shutdown()
    holder = AppHolder(tmp_path, grader_only=True)
    server, client = _start_server(holder)
    try:
        client.get("/overview?folder=" + str(f), expect=404)
    finally:
        server.shutdown()


def test_overview_nothing_graded(tmp_path):
    f = make_grading_folder(tmp_path)
    page = render_overview(GradingApp(f))
    assert "No student is fully graded yet" in page
    assert "nobody yet" in page
