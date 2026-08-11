"""Tests for the sync-template helpers (pure parts only)."""

from hwgenie.sync_template import classify, file_diff, parse_manifest


def test_parse_manifest_skips_comments_and_blanks():
    text = ("# Files kept in sync\n"
            "hwgenie.sty\n"
            "\n"
            "  submission-preamble.tex  \n"
            "# a comment\n"
            "/.github/workflows/build.yml\n")
    assert parse_manifest(text) == [
        "hwgenie.sty",
        "submission-preamble.tex",
        ".github/workflows/build.yml",
    ]


def test_classify():
    assert classify(None, "x") == "new"
    assert classify("x", "x") == "unchanged"
    assert classify("x", "y") == "update"


def test_file_diff_mentions_both_sides():
    d = file_diff("a.sty", "old\n", "new\n")
    assert "a.sty (this repo)" in d
    assert "a.sty (template)" in d
    assert "-old" in d and "+new" in d
