from pathlib import Path

from hwgenie.readings import load_readings, parse_readings


def test_parse_basic_entries_in_file_order():
    text = (
        "% header comment\n"
        "\\reading{Friday, September 4}{Read Sections 1.1--1.2.}\n"
        "\\reading{Monday, August 31}{Skim the preface.}\n"
    )
    got = parse_readings(text)
    assert [(r.due, r.body) for r in got] == [
        ("Friday, September 4", "Read Sections 1.1--1.2."),
        ("Monday, August 31", "Skim the preface."),
    ]


def test_parse_nested_braces_and_multiline_body():
    text = (
        "\\reading{Wednesday, September 9}{\n"
        "  Read \\href{https://example.com/ch1}{Chapter 1} and think\n"
        "  about $x^2$.\n"
        "}\n"
    )
    (r,) = parse_readings(text)
    assert r.due == "Wednesday, September 9"
    assert "\\href{https://example.com/ch1}{Chapter 1}" in r.body
    assert r.body.startswith("Read")  # stripped


def test_commented_out_entries_are_ignored():
    text = (
        "% \\reading{Friday}{The commented-out example in the skeleton.}\n"
        "\\reading{Monday}{Real entry.}  % trailing note\n"
    )
    got = parse_readings(text)
    assert [(r.due, r.body) for r in got] == [("Monday", "Real entry.")]


def test_escapes_and_comments_inside_groups():
    # \% is a literal percent; a % comment inside a group must not hide
    # the closing brace count, and \{ \} never miscount.
    text = (
        "\\reading{Friday}{Score 90\\% or better % } not a close\n"
        "and braces \\{like this\\}.}\n"
    )
    (r,) = parse_readings(text)
    assert r.due == "Friday"
    assert r.body.endswith("\\{like this\\}.")


def test_similar_macro_names_do_not_match():
    text = "\\readings{Friday}{nope}\n\\reading{Monday}{yes}\n"
    got = parse_readings(text)
    assert [(r.due, r.body) for r in got] == [("Monday", "yes")]


def test_empty_and_unclosed_entries():
    assert parse_readings("") == []
    assert parse_readings("\\reading{}{}") == []
    # An unclosed group can't be parsed; nothing bogus is emitted.
    assert parse_readings("\\reading{Friday}{never closed") == []


def test_load_readings_missing_file(tmp_path: Path):
    assert load_readings(tmp_path) == []
    (tmp_path / "readings.tex").write_text(
        "\\reading{Friday}{Read it.}\n", encoding="utf-8"
    )
    (r,) = load_readings(tmp_path)
    assert r.due == "Friday"
