from datetime import date
from pathlib import Path

import pytest

from hwgenie.build import BuildError
from hwgenie.courseconfig import parse_course_config
from hwgenie.site import build_site, is_released

DOC = """\\documentclass{{article}}
\\newenvironment{{solution}}{{}}{{}}
\\begin{{document}}
%===hwgenie===
% number = {number}
% title = {title}
% solutions = {solutions}
%=============
%HEADER
\\begin{{problem}}
\\blue{{Prove something about $x^2$.}}
\\begin{{solution}}
Top secret answer {number}.
\\end{{solution}}
\\end{{problem}}
\\end{{document}}
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "course.yml").write_text(
        "course: Math 261\ntitle: Number Theory\nsemester: Fall 2025\n"
        "instructor: Prof. Hyde\n"
    )
    for num, title, sol in (
        ("1", "Induction", "released"),
        ("2", "Primes", "2025-11-01"),
        ("3", "Digits", "manual"),
    ):
        d = tmp_path / "source" / f"ps{num}"
        d.mkdir(parents=True)
        (d / f"ps{num}.tex").write_text(
            DOC.format(number=num, title=title, solutions=sol)
        )
    return tmp_path


def test_course_config_parse():
    cfg = parse_course_config(
        "# comment\ncourse: Math 261\ntitle: 'Number Theory'  # trailing\n\nbad line\n"
    )
    assert cfg == {"course": "Math 261", "title": "Number Theory"}


def test_is_released():
    today = date(2025, 10, 15)
    assert is_released("yes", today) is True
    assert is_released("No", today) is False
    assert is_released("released", today) is True
    assert is_released("manual", today) is False
    assert is_released(None, today) is False
    assert is_released("2025-10-15", today) is True
    assert is_released("2025-10-16", today) is False
    assert is_released("someday", today) is None


def test_build_site_structure_and_gating(repo):
    result = build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    assert result.ok, result.errors
    site = result.out_dir

    index = (site / "index.html").read_text()
    assert "Math 261: Number Theory" in index
    assert "Fall 2025 · Prof. Hyde" in index
    assert "Problem Set 1: Induction" in index

    # ps1 released, ps2 date in future, ps3 manual
    assert (site / "ps/1/solutions.html").exists()
    assert not (site / "ps/2/solutions.html").exists()
    assert not (site / "ps/3/solutions.html").exists()
    assert (site / "ps/2/index.html").exists()
    # descriptive download file names
    assert (site / "ps/2/PS2-submission-Math261-Fall2025.tex").exists()
    assert (site / ".nojekyll").exists()

    # index links reflect release state; unreleased solutions simply absent
    assert 'ps/1/solutions.html' in index
    assert 'ps/2/solutions.html' not in index
    assert "not yet released" not in index
    assert "PS2-solutions" not in index

    # single-anchor download boxes; no View link (title is the link)
    assert 'class="filebox"' in index
    assert ">View</a>" not in index
    assert "Problem Set PDF" in index and "LaTeX source" in index
    assert "Handout PDF" not in index
    assert 'ps/1/PS1-Math261-Fall2025.pdf' in index
    assert "katex" in index and "renderMathInElement" in index
    assert 'href="https://github.com/tghyde/hwgenie">hwGenie</a>' in index

    # solutions content only where released
    assert "Top secret answer 1" in (site / "ps/1/solutions.html").read_text()
    handout2 = (site / "ps/2/index.html").read_text()
    assert "Top secret answer 2" not in handout2
    # course/semester came from course.yml
    assert "MATH 261" in handout2.upper()


def test_build_site_date_flip(repo):
    result = build_site(repo, compile_pdfs=False, today=date(2025, 11, 2))
    assert (result.out_dir / "ps/2/solutions.html").exists()


def test_build_site_refuses_foreign_out_dir(repo):
    out = repo / "site"
    out.mkdir()
    (out / "precious.txt").write_text("do not delete")
    with pytest.raises(BuildError, match="Refusing to clean"):
        build_site(repo, compile_pdfs=False)


def test_build_site_no_art_by_default(repo):
    result = build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    index = (result.out_dir / "index.html").read_text()
    assert 'class="hero"' not in index
    assert 'rel="icon"' not in index
    # without banner art the header sits in the text column as before
    assert '<header class="doc">' in index


PAGE_DOC = """\\documentclass{{article}}
\\usepackage{{hwgenie}}
\\hwtype{{{doc_type}}}
{number_line}\\hwtitle{{{title}}}
\\begin{{document}}
\\hwmaketitle
Hello.
\\end{{document}}
"""


def test_build_site_banner_and_favicon(repo):
    static = repo / "static"
    static.mkdir()
    (static / "banner.png").write_bytes(b"\x89PNG fake")
    (static / "favicon.png").write_bytes(b"\x89PNG fake")
    lessons = repo / "source" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "lesson1.tex").write_text(PAGE_DOC.format(
        doc_type="lesson", number_line="\\hwnumber{1}\n", title="Intro"))
    handouts = repo / "source" / "handouts"
    handouts.mkdir(parents=True)
    (handouts / "syllabus.tex").write_text(PAGE_DOC.format(
        doc_type="syllabus", number_line="", title="Syllabus"))
    result = build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    assert result.ok, result.errors
    site = result.out_dir

    index = (site / "index.html").read_text()
    # hero replaces the in-column header; title card floats in the banner
    assert '<div class="hero">\n<img src="banner.png" alt="">' in index
    assert index.count('<header class="doc">') == 1
    assert index.index('class="hero"') < index.index('<header class="doc">')
    assert '<link rel="icon" href="favicon.png">' in index
    # problem-set pages get the favicon and hero at their depth
    ps1 = (site / "ps/1/index.html").read_text()
    assert '<link rel="icon" href="../../favicon.png">' in ps1
    assert '<div class="hero">\n<img src="../../banner.png" alt="">' in ps1
    assert ps1.count('<header class="doc">') == 1
    sol1 = (site / "ps/1/solutions.html").read_text()
    assert '<link rel="icon" href="../../favicon.png">' in sol1
    assert '<div class="hero">\n<img src="../../banner.png" alt="">' in sol1
    # the solutions badge rides inside the floating card
    assert sol1.index('class="hero"') < sol1.index('class="badge"')
    # lessons (depth 2) and syllabus (depth 1) get the hero too
    lesson = (site / "lessons/1/index.html").read_text()
    assert '<div class="hero">\n<img src="../../banner.png" alt="">' in lesson
    assert '<link rel="icon" href="../../favicon.png">' in lesson
    syllabus = (site / "syllabus/index.html").read_text()
    assert '<div class="hero">\n<img src="../banner.png" alt="">' in syllabus
    assert '<link rel="icon" href="../favicon.png">' in syllabus
    # published at the site root via static/
    assert (site / "banner.png").exists()
    assert (site / "favicon.png").exists()


def test_render_page_links_custom_css():
    from hwgenie.htmltemplate import render_page

    page = render_page("T", "C", "H", "<p>b</p>", {},
                       custom_css="../../custom.css",
                       favicon="../../favicon.svg")
    assert '<link rel="stylesheet" href="../../custom.css">' in page
    assert '<link rel="icon" href="../../favicon.svg">' in page
    bare = render_page("T", "C", "H", "<p>b</p>", {})
    assert "custom.css" not in bare and 'rel="icon"' not in bare


def test_build_site_rebuild_cleans_previous(repo):
    build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    # flip ps1 to manual and rebuild: stale solutions must disappear
    ps1 = repo / "source/ps1/ps1.tex"
    ps1.write_text(ps1.read_text().replace("solutions = released", "solutions = manual"))
    result = build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    assert not (result.out_dir / "ps/1/solutions.html").exists()
