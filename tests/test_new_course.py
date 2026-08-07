"""Tests for the new-course scaffolding helpers (pure parts only)."""

from pathlib import Path

from hwgenie.new_course import (
    CreateRequest,
    derive_repo_name,
    fill_placeholders,
)


def test_derive_repo_name_basic():
    assert derive_repo_name("Math 301", "Spring 2026") == "math301-spring2026"


def test_derive_repo_name_punctuation_and_case():
    assert derive_repo_name("CMPU/MATH 240", "Fall 2027") == "cmpumath240-fall2027"


def test_derive_repo_name_empty():
    assert derive_repo_name("", "") == "new-course"


def test_resolved_repo_prefers_explicit_name():
    req = CreateRequest(course="Math 301", title="X", semester="Spring 2026",
                        repo="my-custom-name")
    assert req.resolved_repo() == "my-custom-name"
    req.repo = ""
    assert req.resolved_repo() == "math301-spring2026"


def test_fill_placeholders(tmp_path: Path):
    (tmp_path / "course.yml").write_text(
        'course: "@@COURSE@@"\nsemester: "@@SEMESTER@@"\ntheme: slate\n')
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "syllabus.tex").write_text(
        "@@INSTRUCTOR@@ at @@OFFICE@@, \\texttt{@@EMAIL@@}")
    (tmp_path / "figs.png").write_bytes(b"\x89PNG not text")

    changed = fill_placeholders(tmp_path, {
        "course": "Math 301", "semester": "Spring 2026",
        "instructor": "Prof. X", "office": "Room 1", "email": "x@y.edu",
        "title": "Unused Here",
    })

    assert sorted(p.name for p in changed) == ["course.yml", "syllabus.tex"]
    assert (tmp_path / "course.yml").read_text() == \
        'course: "Math 301"\nsemester: "Spring 2026"\ntheme: slate\n'
    assert (tmp_path / "sub" / "syllabus.tex").read_text() == \
        "Prof. X at Room 1, \\texttt{x@y.edu}"


def test_fill_placeholders_skips_git_dir(tmp_path: Path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config.yml").write_text("@@COURSE@@")
    changed = fill_placeholders(tmp_path, {"course": "Math 1"})
    assert changed == []
    assert (git / "config.yml").read_text() == "@@COURSE@@"


def test_fill_placeholders_leaves_missing_values(tmp_path: Path):
    f = tmp_path / "a.tex"
    f.write_text("@@COURSE@@ / @@EMAIL@@")
    fill_placeholders(tmp_path, {"course": "Math 2", "email": ""})
    assert f.read_text() == "Math 2 / @@EMAIL@@"
