from datetime import date
from pathlib import Path

import pytest

from hwgenie.build import make_variants
from hwgenie.metadata import MetadataError, parse_metadata
from hwgenie.site import build_site

DOC_TEMPLATE = """\\documentclass[11pt]{article}
\\usepackage{hwgenie}

\\hwnumber{4}
\\hwtitle{Congruences}
\\hwsolutions{2025-10-20}
@RELEASE@
\\begin{document}
\\hwmaketitle
%HEADER
\\begin{problem}
\\blue{Prove it.}
\\begin{solution}
Secret.
\\end{solution}
\\end{problem}
\\end{document}
"""


def doc(release: str = "") -> str:
    return DOC_TEMPLATE.replace("@RELEASE@", release)

STY = """\\NeedsTeXFormat{LaTeX2e}
\\ProvidesPackage{hwgenie}[2026/08/05 test]
\\newcommand{\\hwcourse}{Course}
\\InputIfFileExists{coursedata}{}{}
\\newcommand{\\blue}[1]{#1}
\\newenvironment{solution}{}{}
\\endinput
"""


def test_parse_hw_commands():
    m = parse_metadata(doc())
    assert m.fmt == "commands"
    assert m.number == "4"
    assert m.title == "Congruences"
    assert m.solutions_release == "2025-10-20"
    assert m.release is None
    assert m.span == (0, 0)


def test_commands_override_comment_block():
    text = (
        "%===hwgenie===\n% number = 9\n% title = Old\n%====\n"
        "\\hwnumber{4}\n\\hwtitle{New}\n"
    )
    m = parse_metadata(text)
    assert m.number == "4" and m.title == "New"


def test_release_gate_hides_assignment(tmp_path):
    (tmp_path / "course.yml").write_text(
        "course: Math 261\nsemester: Fall 2025\n"
    )
    (tmp_path / "hwgenie.sty").write_text(STY)
    d = tmp_path / "source" / "ps04"
    d.mkdir(parents=True)
    (d / "ps04.tex").write_text(doc("\\hwrelease{2025-12-01}"))
    result = build_site(tmp_path, compile_pdfs=False, today=date(2025, 10, 1))
    assert result.assignments == []
    assert any("not released yet" in w for w in result.warnings)
    assert not (tmp_path / "site" / "ps" / "4").exists()

    result2 = build_site(tmp_path, compile_pdfs=False, today=date(2025, 12, 2))
    assert len(result2.assignments) == 1


def test_submission_inlines_sty_and_coursedata(tmp_path):
    (tmp_path / "hwgenie.sty").write_text(STY)
    (tmp_path / "coursedata.tex").write_text(
        "\\renewcommand{\\hwcourse}{Math 261}\n"
    )
    v = make_variants(doc(), search_dirs=[tmp_path])
    sub = v["submission"]
    assert "\\usepackage{hwgenie}" not in sub
    assert "\\ProvidesPackage" not in sub
    assert "\\renewcommand{\\hwcourse}{Math 261}" in sub
    assert "\\InputIfFileExists{coursedata}" not in sub
    assert "Secret." not in sub


def _handout_doc(number: str, title: str) -> str:
    num = f"\\hwnumber{{{number}}}\n" if number else ""
    return (
        "\\documentclass[11pt]{article}\n\\usepackage{hwgenie}\n"
        f"\\hwtype{{handout}}\n{num}\\hwtitle{{{title}}}\n"
        "\\begin{document}\n\\hwmaketitle\nReview content.\n\\end{document}\n"
    )


def test_handout_doc_type(tmp_path):
    (tmp_path / "course.yml").write_text(
        "course: Math 301\nsemester: Fall 2026\n"
    )
    (tmp_path / "hwgenie.sty").write_text(STY)
    d = tmp_path / "source" / "handouts"
    d.mkdir(parents=True)
    (d / "review.tex").write_text(_handout_doc("2", "Midterm Review"))
    (d / "notation.tex").write_text(_handout_doc("", "Notation Guide"))
    result = build_site(tmp_path, compile_pdfs=False, today=date(2026, 9, 1))
    assert not result.errors
    # numbered handout: numeric slug, "Handout N: Title" listing
    assert (tmp_path / "site" / "handouts" / "2" / "index.html").exists()
    # unnumbered handout: title slug, title-only listing
    assert (tmp_path / "site" / "handouts" / "notation-guide" /
            "index.html").exists()
    index = (tmp_path / "site" / "index.html").read_text()
    assert "Handout 2: Midterm Review" in index
    assert "Notation Guide" in index
    assert "Handout : " not in index
    assert 'id="handouts"' in index          # Handouts section present
    assert 'id="lessons"' not in index       # no phantom Lessons section
    page = (tmp_path / "site" / "handouts" / "2" / "index.html").read_text()
    assert "Handout 2: Midterm Review" in page
    assert "Handout2-Math301-Fall2026.pdf" in page


def test_handout_number_optional_in_metadata():
    m = parse_metadata("\\hwtype{handout}\n\\hwtitle{Review}\n")
    assert m.doc_type == "handout"
    assert m.number == ""


def test_coursedata_macros_reach_katex(tmp_path):
    """Course-wide macros live in coursedata.tex (sync-safe, course-owned);
    they must reach the page's KaTeX macro table, not just the PDFs."""
    (tmp_path / "course.yml").write_text(
        "course: Math 301\nsemester: Fall 2026\n"
    )
    (tmp_path / "hwgenie.sty").write_text(STY)
    (tmp_path / "coursedata.tex").write_text("\\def\\inv{^{-1}}\n")
    d = tmp_path / "source" / "ps04"
    d.mkdir(parents=True)
    (d / "ps04.tex").write_text(doc())
    build_site(tmp_path, compile_pdfs=False, today=date(2025, 10, 1))
    page = (tmp_path / "site" / "ps" / "4" / "index.html").read_text()
    assert '"\\\\inv": "^{-1}"' in page


def test_submission_prefers_student_preamble(tmp_path):
    (tmp_path / "hwgenie.sty").write_text(STY)
    (tmp_path / "submission-preamble.tex").write_text(
        "% student preamble\n\\usepackage{amsmath}\n"
        "\\InputIfFileExists{coursedata}{}{}\n"
    )
    (tmp_path / "coursedata.tex").write_text(
        "\\renewcommand{\\hwcourse}{Math 261}\n"
    )
    v = make_variants(doc(), search_dirs=[tmp_path])
    sub = v["submission"]
    assert "% student preamble" in sub
    assert "\\ProvidesPackage" not in sub          # sty NOT inlined
    assert "\\renewcommand{\\hwcourse}{Math 261}" in sub


def test_hwvariant_injected_when_no_header_marker():
    text = doc().replace("%HEADER\n", "")
    v = make_variants(text)
    assert "\\hwvariant{Solutions}" in v["solutions"]
    assert "\\hwvariant{Submission}" in v["submission"]
    assert "\\hwvariant{Handout}" in v["handout"]  # disables \solnewpage there
    assert "\\blue{SOLUTIONS}" not in v["solutions"]


def test_header_marker_still_uses_banner():
    v = make_variants(doc())
    assert "\\blue{SOLUTIONS}" in v["solutions"]
    assert "\\hwvariant" not in v["solutions"]


DUE = "Friday, September 4th at 11:59pm"


def _due_doc() -> str:
    return doc().replace(
        "\\hwtitle{Congruences}\n",
        f"\\hwtitle{{Congruences}}\n\\hwdue{{{DUE}}}\n",
    )


def test_hwdue_parsed_into_metadata():
    m = parse_metadata(_due_doc())
    assert m.due == DUE
    # comment-block form works too
    m2 = parse_metadata(f"%===hwgenie===\n% number = 4\n% due = {DUE}\n%====\n")
    assert m2.due == DUE


def test_hwdue_stripped_from_submission_only():
    v = make_variants(_due_doc())
    assert "\\hwdue" not in v["submission"]  # student preamble has no \hwdue
    # PDF variants keep it: the sty shows/hides it by variant
    assert f"\\hwdue{{{DUE}}}" in v["handout"]
    assert f"\\hwdue{{{DUE}}}" in v["solutions"]
    assert f"\\hwdue{{{DUE}}}" in v["solutions_web"]


def test_hwdue_on_site_pages(tmp_path):
    (tmp_path / "course.yml").write_text(
        "course: Math 261\nsemester: Fall 2025\n"
    )
    (tmp_path / "hwgenie.sty").write_text(STY)
    d = tmp_path / "source" / "ps04"
    d.mkdir(parents=True)
    (d / "ps04.tex").write_text(
        _due_doc().replace("\\hwsolutions{2025-10-20}", "\\hwsolutions{released}")
    )
    result = build_site(tmp_path, compile_pdfs=False, today=date(2025, 10, 1))
    assert result.ok, result.errors
    site = tmp_path / "site"
    handout = (site / "ps" / "4" / "index.html").read_text()
    assert f'<p class="due"><span class="due-label">Due</span>{DUE}</p>' in handout
    # index card: due date rides on the title line
    index = (site / "index.html").read_text()
    assert f'<span class="card-due">Due {DUE}</span>' in index
    # solutions page carries no due line; the macro never leaks into the body
    solutions = (site / "ps" / "4" / "solutions.html").read_text()
    assert 'class="due"' not in solutions
    assert "hwdue" not in handout and "hwdue" not in solutions


def test_no_metadata_still_errors():
    with pytest.raises(MetadataError, match="No metadata"):
        parse_metadata("\\documentclass{article}\\begin{document}\\end{document}")
