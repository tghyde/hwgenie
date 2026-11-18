"""course.yml `lesson_label` — call lesson files "Chapter" (or anything)."""

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from hwgenie.courseconfig import lesson_labels, parse_course_config
from hwgenie.site import build_site

LESSON_DOC = """\\documentclass{{article}}
\\usepackage{{hwgenie}}
\\hwtype{{lesson}}
\\hwnumber{{{n}}}
\\hwtitle{{{title}}}
\\begin{{document}}
\\hwmaketitle
Intro.
\\subsection{{Basics}}
Text.
\\end{{document}}
"""


def _repo(tmp_path: Path, extra_cfg: str = "") -> Path:
    (tmp_path / "course.yml").write_text(
        "course: Math 339\ntitle: Topology\nsemester: Spring 2027\n" + extra_cfg
    )
    lessons = tmp_path / "source" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "chapter04.tex").write_text(
        LESSON_DOC.format(n="4", title="Covering Maps and the Circle"))
    return tmp_path


def test_lesson_labels_default_and_override():
    assert lesson_labels({}) == ("Lesson", "Lessons")
    assert lesson_labels(parse_course_config("lesson_label: Chapter\n")) == (
        "Chapter", "Chapters")
    cfg = parse_course_config('lesson_label: "Lecture Notes"\n'
                              "lesson_label_plural: Lecture Notes\n")
    assert lesson_labels(cfg) == ("Lecture Notes", "Lecture Notes")


def test_chapter_label_everywhere_on_site(tmp_path):
    repo = _repo(tmp_path, "lesson_label: Chapter\n")
    result = build_site(repo, compile_pdfs=False, today=date(2027, 2, 1))
    assert result.ok, result.errors
    site = result.out_dir

    index = (site / "index.html").read_text()
    assert ">Chapter 4: Covering Maps and the Circle</a>" in index
    assert '<h2 class="index-head" id="lessons">Chapters</h2>' in index
    assert "Chapter PDF" in index
    assert "Chapter4-Math339-Spring2027.pdf" in index
    assert re.search(r'href="#lessons"[^>]*>Chapters<', index)
    assert "Lesson" not in index

    page = (site / "lessons/4/index.html").read_text()
    assert "<title>Chapter 4: Covering Maps and the Circle — Math 339, Spring 2027</title>" in page
    assert '<p class="toc-title">Chapter 4</p>' in page
    assert "Lesson" not in page
    assert result.assignments[0].meta.lesson_label == "Chapter"


def test_default_still_says_lesson(tmp_path):
    repo = _repo(tmp_path)
    result = build_site(repo, compile_pdfs=False, today=date(2027, 2, 1))
    assert result.ok, result.errors
    index = (result.out_dir / "index.html").read_text()
    assert ">Lesson 4: Covering Maps and the Circle</a>" in index
    assert '<h2 class="index-head" id="lessons">Lessons</h2>' in index
    assert "Lesson PDF" in index
    assert "Lesson4-Math339-Spring2027.pdf" in index
    assert "Chapter" not in index
    page = (result.out_dir / "lessons/4/index.html").read_text()
    assert '<p class="toc-title">Lesson 4</p>' in page


def test_cli_lists_chapters(tmp_path, capsys):
    from hwgenie.cli import main

    repo = _repo(tmp_path, "lesson_label: Chapter\n")
    assert main(["build-site", str(repo), "--no-pdf"]) == 0
    assert "Chapter 4 (" in capsys.readouterr().out


# The sty lives in the course template (a sibling checkout), not this repo.
TEMPLATE = Path(__file__).resolve().parents[2] / "course-template"


@pytest.mark.skipif(
    shutil.which("pdflatex") is None or shutil.which("pdftotext") is None
    or not (TEMPLATE / "hwgenie.sty").exists(),
    reason="needs pdflatex, pdftotext and the course-template checkout")
@pytest.mark.parametrize("coursedata, expected", [
    ("", "Lesson 4: Covering Maps"),
    ("\\renewcommand{\\hwlessonlabel}{Chapter}\n", "Chapter 4: Covering Maps"),
])
def test_sty_title_uses_hwlessonlabel(tmp_path, coursedata, expected):
    shutil.copy(TEMPLATE / "hwgenie.sty", tmp_path)
    (tmp_path / "coursedata.tex").write_text(
        "\\renewcommand{\\hwcourse}{Math 339}\n"
        "\\renewcommand{\\hwsemester}{Spring 2027}\n" + coursedata)
    (tmp_path / "ch.tex").write_text(
        LESSON_DOC.format(n="4", title="Covering Maps and the Circle"))
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                    "ch.tex"], cwd=tmp_path, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    text = subprocess.run(["pdftotext", "ch.pdf", "-"], cwd=tmp_path,
                          check=True, capture_output=True, text=True).stdout
    assert expected in " ".join(text.split())
