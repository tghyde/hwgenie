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
    assert "\\hwvariant" not in v["handout"]
    assert "\\blue{SOLUTIONS}" not in v["solutions"]


def test_header_marker_still_uses_banner():
    v = make_variants(doc())
    assert "\\blue{SOLUTIONS}" in v["solutions"]
    assert "\\hwvariant" not in v["solutions"]


def test_no_metadata_still_errors():
    with pytest.raises(MetadataError, match="No metadata"):
        parse_metadata("\\documentclass{article}\\begin{document}\\end{document}")
