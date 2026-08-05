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

    # index links reflect release state
    assert 'ps/1/solutions.html' in index
    assert 'ps/2/solutions.html' not in index
    assert index.count("Solutions not yet released") == 2

    # grouped file boxes with download buttons; KaTeX for math in titles
    assert 'class="filebox"' in index and 'class="dl"' in index
    assert "Handout PDF" in index and "LaTeX source" in index
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


def test_build_site_rebuild_cleans_previous(repo):
    build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    # flip ps1 to manual and rebuild: stale solutions must disappear
    ps1 = repo / "source/ps1/ps1.tex"
    ps1.write_text(ps1.read_text().replace("solutions = released", "solutions = manual"))
    result = build_site(repo, compile_pdfs=False, today=date(2025, 10, 15))
    assert not (result.out_dir / "ps/1/solutions.html").exists()
