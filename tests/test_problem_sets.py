"""Problem set manager: source editing, sync classification, payloads."""

import json

import pytest

from hwgenie.metadata import parse_metadata
from hwgenie.problem_sets import (_clean_edits, apply_edits,
                                  candidate_courses, classify_sync,
                                  describe_edit, load_pinned,
                                  problem_set_sources, save_pinned,
                                  set_hw_field)

COMMANDS = """\\documentclass[11pt]{article}
\\usepackage{hwgenie}

\\hwnumber{3}
\\hwtitle{Digits and Sage}
\\hwrelease{no}   % flip to yes to publish
\\hwsolutions{no} % flip to yes to publish solutions

\\begin{document}
\\end{document}
"""

BLOCK = """\\documentclass{article}
%===hwgenie===
% type      = problemset
% number    = 4
% solutions = no
%=============
\\begin{document}
\\end{document}
"""


# ------------------------------------------------------ set_hw_field --

def test_sets_an_existing_command_and_keeps_its_comment():
    out = set_hw_field(COMMANDS, "release", "yes")
    assert "\\hwrelease{yes}   % flip to yes to publish" in out
    assert parse_metadata(out).release == "yes"


def test_inserts_a_missing_command_in_canonical_order():
    out = set_hw_field(COMMANDS, "due", "Friday at 11:59pm")
    assert parse_metadata(out).due == "Friday at 11:59pm"
    # due sorts after title and before release
    assert out.index("\\hwtitle") < out.index("\\hwdue") < out.index(
        "\\hwrelease")


def test_inserts_below_the_preamble_when_there_is_no_metadata_at_all():
    bare = "\\documentclass{article}\n\\usepackage{hwgenie}\n\\begin{document}\n"
    out = set_hw_field(bare, "release", "yes")
    assert "\\hwrelease{yes}" in out
    assert out.index("\\usepackage{hwgenie}") < out.index("\\hwrelease")


def test_an_empty_value_deletes_the_whole_command_line():
    with_due = set_hw_field(COMMANDS, "due", "Friday")
    out = set_hw_field(with_due, "due", "")
    assert "\\hwdue" not in out
    assert out == COMMANDS


def test_edits_the_v2_comment_block_when_a_file_uses_one():
    out = set_hw_field(BLOCK, "solutions", "yes")
    assert parse_metadata(out).solutions_release == "yes"
    assert "\\hwsolutions" not in out


def test_adds_a_missing_key_to_a_v2_block():
    out = set_hw_field(BLOCK, "due", "Monday")
    assert parse_metadata(out).due == "Monday"
    assert out.index("% due") < out.index("%=============")


def test_setting_the_same_value_twice_is_stable():
    once = set_hw_field(COMMANDS, "release", "yes")
    assert set_hw_field(once, "release", "yes") == once


# -------------------------------------------------------- apply_edits --

def test_apply_edits_writes_all_three_fields():
    out = apply_edits(COMMANDS, {"released": True, "solutions": False,
                                 "due": "Sep 4"})
    m = parse_metadata(out)
    assert (m.release, m.solutions_release, m.due) == ("yes", "no", "Sep 4")


def test_apply_edits_only_touches_the_keys_it_is_given():
    out = apply_edits(COMMANDS, {"released": True})
    assert parse_metadata(out).solutions_release == "no"
    assert "\\hwdue" not in out


# ------------------------------------------------------- sync + edits --

@pytest.mark.parametrize("ours,theirs,dirty,expected", [
    (set(), set(), set(), "insync"),
    ({"a.tex"}, set(), set(), "ahead"),
    (set(), set(), {"a.tex"}, "ahead"),
    (set(), {"a.tex"}, set(), "behind"),
    ({"a.tex"}, {"a.tex"}, set(), "diverged"),
    (set(), {"a.tex"}, {"a.tex"}, "diverged"),
])
def test_classify_sync(ours, theirs, dirty, expected):
    assert classify_sync("a.tex", ours, theirs, dirty) == expected


def test_clean_edits_drops_junk_and_escaping_paths():
    out = _clean_edits({"ok.tex": {"released": 1, "nope": "x"},
                        "../evil.tex": {"released": True},
                        "/abs.tex": {"released": True},
                        7: {"released": True},
                        "bad.tex": "not a dict"})
    assert out == {"ok.tex": {"released": True}}


def test_clean_edits_keeps_an_empty_patch_as_commit_this_file_as_is():
    assert _clean_edits({"ps01.tex": {}}) == {"ps01.tex": {}}


def test_clean_edits_caps_a_runaway_due_string():
    out = _clean_edits({"a.tex": {"due": "x" * 500}})
    assert len(out["a.tex"]["due"]) == 200


def test_describe_edit_reports_only_real_changes():
    before = {"released": False, "solutions": False, "due": ""}
    assert describe_edit(before, {"released": True, "solutions": False}) == \
        "assignment published"
    assert describe_edit(before, {"due": "Friday"}) == "due Friday"
    assert describe_edit(before, {"released": False}) == ""


# ------------------------------------------------------------- scans --

def test_problem_set_sources_skips_generated_and_local_build_output(tmp_path):
    """The page reads the working tree, where a local ``build/`` sits
    beside the sources — CI only ever sees a clean checkout."""
    d = tmp_path / "source" / "problem-sets" / "ps01"
    (d / "build" / "html").mkdir(parents=True)
    (d / "ps01.tex").write_text("x")
    (d / "ps01 [submission].tex").write_text("x")
    (d / "build" / "PS 1 [source].tex").write_text("x")
    (d / "build" / "PS 1 [solutions].tex").write_text("x")
    scratch = tmp_path / "source" / "problem-sets" / "_scratch"
    scratch.mkdir()
    (scratch / "ps.tex").write_text("x")
    assert [p.name for p in problem_set_sources(tmp_path)] == ["ps01.tex"]


def test_problem_set_sources_drops_gitignored_files(tmp_path):
    from hwgenie.course_admin import _run
    d = tmp_path / "source" / "problem-sets" / "ps01"
    d.mkdir(parents=True)
    (d / "ps01.tex").write_text("x")
    (d / "draft.tex").write_text("x")
    _run(["git", "init", "-q"], cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("draft.tex\n")
    assert [p.name for p in problem_set_sources(tmp_path)] == ["ps01.tex"]


def test_problem_set_sources_is_empty_without_the_folder(tmp_path):
    assert problem_set_sources(tmp_path) == []


def test_pinned_courses_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "problem-sets.json"
    monkeypatch.setattr("hwgenie.problem_sets.PINNED_PATH", path)
    assert load_pinned() == []
    save_pinned(["me/math261-fall2025", "me/math301-fall2026"])
    assert load_pinned() == ["me/math261-fall2025", "me/math301-fall2026"]
    path.write_text("{ not json")
    assert load_pinned() == []


def test_pinned_ignores_non_string_entries(tmp_path, monkeypatch):
    path = tmp_path / "problem-sets.json"
    monkeypatch.setattr("hwgenie.problem_sets.PINNED_PATH", path)
    path.write_text(json.dumps({"pinned": ["ok/repo", 3, None]}))
    assert load_pinned() == ["ok/repo"]


def test_candidates_skip_the_template_and_repos_without_problem_sets(tmp_path):
    from hwgenie.sync_template import DEFAULT_TEMPLATE
    clones = {}
    for repo, has_sets in ((DEFAULT_TEMPLATE, True), ("me/math101", True),
                           ("me/notes", False)):
        d = tmp_path / repo.replace("/", "_")
        (d / "source" / "problem-sets").mkdir(parents=True) if has_sets \
            else d.mkdir()
        (d / "course.yml").write_text("course: X\nsemester: Fall 2026\n")
        clones[repo] = d
    assert [c["repo"] for c in candidate_courses(clones)] == ["me/math101"]
