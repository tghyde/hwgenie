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


def _fake_course(tmp_path: Path) -> Path:
    (tmp_path / "source" / "lessons").mkdir(parents=True)
    (tmp_path / "source" / "problem-sets" / "ps01").mkdir(parents=True)
    (tmp_path / "readings.tex").write_text("% \\reading example\n")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "intro.html").write_text(
        '<p>Hi</p>\n<nav class="site" style="justify-content:center">\n'
        '  <a class="filebox viewbox" href="#handouts">Handouts</a>\n'
        '  <a class="filebox viewbox" href="#readings">Readings</a>\n'
        '  <a class="filebox viewbox" href="#lessons">Lessons</a>\n'
        '  <a class="filebox viewbox" href="#problem-sets">Problem Sets</a>\n'
        "</nav>\n")
    return tmp_path


def test_disable_sections_default_keeps_everything(tmp_path: Path):
    # Readings are opt-in: the default removes the file and its nav button
    # but always keeps lessons and problem sets.
    from hwgenie.new_course import disable_sections
    root = _fake_course(tmp_path)
    assert disable_sections(root) == ["reading assignments"]
    assert (root / "source" / "lessons").is_dir()
    assert (root / "source" / "problem-sets").is_dir()
    assert not (root / "readings.tex").exists()
    intro = (root / "static" / "intro.html").read_text()
    assert "#readings" not in intro
    assert "#lessons" in intro and "#problem-sets" in intro


def test_enable_readings_keeps_file_and_button(tmp_path: Path):
    from hwgenie.new_course import disable_sections
    root = _fake_course(tmp_path)
    assert disable_sections(root, readings=True) == []
    assert (root / "readings.tex").is_file()
    assert "#readings" in (root / "static" / "intro.html").read_text()


def test_disable_lessons(tmp_path: Path):
    from hwgenie.new_course import disable_sections
    root = _fake_course(tmp_path)
    removed = disable_sections(root, lessons=False, readings=True)
    assert removed == ["lessons"]
    assert not (root / "source" / "lessons").exists()
    assert (root / "source" / "problem-sets").is_dir()
    intro = (root / "static" / "intro.html").read_text()
    assert "#lessons" not in intro
    assert "#handouts" in intro and "#problem-sets" in intro


def test_disable_both_sections(tmp_path: Path):
    from hwgenie.new_course import disable_sections
    root = _fake_course(tmp_path)
    removed = disable_sections(root, lessons=False, problem_sets=False,
                               readings=True)
    assert sorted(removed) == ["lessons", "problem sets"]
    intro = (root / "static" / "intro.html").read_text()
    assert "#lessons" not in intro and "#problem-sets" not in intro
    assert "#handouts" in intro


def test_worker_surfaces_unexpected_exception(monkeypatch):
    # A worker crash must end in phase "error", never a forever-"running"
    # page (regression: gh missing from a GUI launch's PATH raised
    # FileNotFoundError straight through the thread).
    from hwgenie import new_course_gui as gui

    def boom(req, log):
        raise RuntimeError("gh vanished")

    monkeypatch.setattr(gui, "create_course", boom)
    state = gui._State()
    monkeypatch.setattr(gui, "STATE", state)
    with state.lock:
        state.phase = "running"
    gui._worker(gui.CreateRequest(course="Math 1", title="T", semester="Fall 2026"))
    snap = state.snapshot()
    assert snap["phase"] == "error"
    assert "gh vanished" in snap["result"]["error"]


def test_run_missing_command_is_steperror():
    import pytest

    from hwgenie.new_course import StepError, _run
    with pytest.raises(StepError, match="command not found"):
        _run(["hwgenie-no-such-binary-xyz"], lambda s: None)


def test_extend_path_adds_homebrew(monkeypatch):
    from hwgenie.new_course import _extend_path
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _extend_path()
    import os
    parts = os.environ["PATH"].split(os.pathsep)
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if Path(d).is_dir():
            assert d in parts
    assert parts.index("/usr/bin") == len(parts) - 2  # extras prepended
