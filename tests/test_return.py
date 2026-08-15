"""Tests for hwgenie return (feedback packaging)."""

import csv
import shutil
import time
import zipfile

import pytest

from test_grade import _start_server, make_grading_folder

from hwgenie.cli import main
from hwgenie.feedback import build_feedback, display_name
from hwgenie.grade import GradeError, GradeStore, load_rubric


@pytest.fixture
def grading_folder(tmp_path):
    return make_grading_folder(tmp_path)


def _seed_grades(grading_folder):
    rubric = load_rubric(grading_folder, 3)
    store = GradeStore(grading_folder, rubric)
    store.update("Doe-Jane", 1, {
        "score": 4,
        "comments": [{"anchor": "inline body", "text": "uses $x^2$ well"},
                     {"anchor": None, "text": "general note"}]})
    store.update("Doe-Jane", 2, {"score": 2})
    store.update("Roe-Rick", 3, {"score": 0,
                                 "comments": [{"anchor": None, "text": "x"}]})
    return store


@pytest.fixture
def returned(grading_folder):
    _seed_grades(grading_folder)
    result = build_feedback(grading_folder, pdf=False)
    return grading_folder, result


def test_display_name():
    assert display_name("Doe-Jane") == "Jane Doe"
    assert display_name("Van Der Berg-Alex") == "Alex Van Der Berg"
    assert display_name("Smith-Jones-Ana") == "Ana Smith-Jones"
    assert display_name("Mononym") == "Mononym"


def test_return_outputs(returned):
    folder, result = returned
    assert sorted(result.exported) == ["Doe-Jane", "Roe-Rick"]
    assert result.skipped == ["Poe-Pat"]        # nothing graded
    html = (result.out_dir / "feedback" / "Doe-Jane" /
            "feedback.html").read_text()
    assert "Feedback — Jane Doe" in html         # Last-First unpacked
    assert "uses $x^2$ well" in html             # comment in the JSON blob
    assert "general note" in html                # and in the visible list
    assert "Total: 6 / 11.5" in html             # 4 + 2 of 4+2.5+5
    assert "Second paragraph." in html           # rendered student work
    rick = (result.out_dir / "feedback" / "Roe-Rick" /
            "feedback.html").read_text()
    assert "transcribed from the" in rick        # reconstructed banner


def test_return_overview_and_nav(returned):
    folder, result = returned
    html = (result.out_dir / "feedback" / "Doe-Jane" /
            "feedback.html").read_text()
    # score overview: one column per template problem, cards jump to parts
    assert 'class="scoregrid"' in html
    assert ">P1</div>" in html and ">P2</div>" in html
    assert 'href="#part-1"' in html and 'id="part-1"' in html
    assert html.count('class="pie"') >= 3
    assert "pie-ok" in html          # 4/4 -> green
    # sticky nav: jump chips carry pies, no student name, top button last
    assert 'id="fnav"' in html and 'class="jump"' in html
    assert 'id="totop"' in html
    fnav = html.split('id="fnav"')[1].split("</div>")[0]
    assert 'class="pie"' in fnav
    assert 'class="nm"' not in fnav
    # part headers say "Problem <label>", and the template statement is
    # folded into each card behind the header toggle
    assert "Problem 1.1</span>" in html
    assert 'class="stoggle"' in html and 'class="pstmt"' in html
    assert "Do part one." in html          # segment for box 1
    assert "Do part two." in html          # segment for box 2


def test_return_zip_uses_moodle_folders(returned):
    folder, result = returned
    with zipfile.ZipFile(result.out_dir / "moodle-feedback.zip") as z:
        names = sorted(z.namelist())
    assert names == ["Doe-Jane_111_x/feedback.html",
                     "Roe-Rick_222_x/feedback.html"]


def test_return_gradebook_fans_out_groups(returned):
    folder, result = returned
    rows = list(csv.reader(
        (result.out_dir / "gradebook.csv").open()))
    assert rows[0] == ["submission", "moodle_id", "student",
                       "1.1", "1.2", "2.1a", "total", "out_of"]
    # Doe-Jane is a group of two: one row per member, same scores
    jane_rows = [r for r in rows if r[0] == "Doe-Jane"]
    assert [r[2] for r in jane_rows] == ["Jane Doe", "Extra Member"]
    assert jane_rows[0][3:] == ["4", "2", "", "6", "11.5"]
    rick = next(r for r in rows if r[0] == "Roe-Rick")
    assert rick[2] == "Rick Roe"                 # no group: unpacked slug
    assert rick[3:] == ["", "", "0", "0", "11.5"]


WORKSHEET_HEADER = ["Identifier", "Full name", "Email address", "Status",
                    "Grade", "Maximum grade", "Grade can be changed",
                    "Last modified (submission)", "Last modified (grade)"]


def _write_worksheet(folder, max_grade="11.50"):
    rows = [
        ["Participant 111", "Jane Doe", "jd@x.edu", "Submitted", "",
         max_grade, "Yes", "-", "-"],
        ["Participant 222", "Rick Roe", "rr@x.edu", "Submitted", "3.00",
         max_grade, "No", "-", "-"],
        ["Participant 333", "Pat Poe", "pp@x.edu", "Submitted", "",
         max_grade, "Yes", "-", "-"],
        ["Participant 999", "No Submission", "ns@x.edu", "No submission",
         "", max_grade, "Yes", "-", "-"],
    ]
    path = folder / "Grades-TEST-Problem Set 1--42.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(WORKSHEET_HEADER)
        w.writerows(rows)
    return path


def test_worksheet_fill(grading_folder):
    _seed_grades(grading_folder)
    _write_worksheet(grading_folder)          # auto-detected
    result = build_feedback(grading_folder)
    ws = result.worksheet
    assert ws is not None and ws["filled"] == 1     # Jane
    assert ws["locked"] == ["Roe-Rick"]             # "changeable" = No
    assert ws["unmatched"] == []
    out = (result.out_dir / "grading-worksheet-upload.csv")
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")          # BOM survives
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert rows[0] == WORKSHEET_HEADER
    jane = next(r for r in rows if r[0] == "Participant 111")
    assert jane[4] == "6.00"                        # total filled
    assert jane[1] == "Jane Doe"                    # rest untouched
    rick = next(r for r in rows if r[0] == "Participant 222")
    assert rick[4] == "3.00"                        # locked: unchanged
    pat = next(r for r in rows if r[0] == "Participant 333")
    assert pat[4] == ""                             # ungraded: untouched


def test_worksheet_max_mismatch_warns(grading_folder):
    _seed_grades(grading_folder)
    _write_worksheet(grading_folder, max_grade="100.00")
    result = build_feedback(grading_folder)
    assert any("Maximum grade" in w for w in result.warnings)


def test_worksheet_bad_file_warns_not_fails(grading_folder):
    _seed_grades(grading_folder)
    (grading_folder / "Grades-bad.csv").write_text("just,some,junk\n1,2,3\n")
    result = build_feedback(grading_folder)
    assert result.ok
    assert result.worksheet is None
    assert any("worksheet not filled" in w for w in result.warnings)


def test_return_nothing_graded(grading_folder):
    with pytest.raises(GradeError, match="nothing to export"):
        build_feedback(grading_folder, pdf=False)


def test_return_include_ungraded(grading_folder):
    _seed_grades(grading_folder)
    result = build_feedback(grading_folder, pdf=False,
                            include_ungraded=True)
    assert "Poe-Pat" in result.exported


def test_return_cli(grading_folder, capsys):
    _seed_grades(grading_folder)
    assert main(["return", str(grading_folder)]) == 0
    out = capsys.readouterr().out
    assert "Exported 2 submissions" in out
    assert "1 submissions had nothing graded" in out


@pytest.mark.skipif(shutil.which("pdflatex") is None,
                    reason="pdflatex not installed")
def test_return_pdf_sheet(grading_folder):
    _seed_grades(grading_folder)
    result = build_feedback(grading_folder, pdf=True)
    pdf = result.out_dir / "feedback" / "Doe-Jane" / "feedback.pdf"
    assert result.pdf_failures == [] and pdf.is_file()
    assert pdf.read_bytes()[:5] == b"%PDF-"
    with zipfile.ZipFile(result.out_dir / "moodle-feedback.zip") as z:
        assert "Doe-Jane_111_x/feedback.pdf" in z.namelist()


def test_api_export(grading_folder):
    from hwgenie.grade_gui import AppHolder, GradingApp

    _seed_grades(grading_folder)
    holder = AppHolder(root=grading_folder)
    holder.current = GradingApp(grading_folder)
    server, client = _start_server(holder)
    try:
        assert client.post("/api/export", {"pdf": False})["ok"] is True
        for _ in range(100):
            st = client.get("/api/export")
            if not st["running"]:
                break
            time.sleep(0.05)
        assert st["error"] is None
        assert st["summary"]["exported"] == 2
        assert (grading_folder / "return" / "moodle-feedback.zip").is_file()
    finally:
        server.shutdown()
