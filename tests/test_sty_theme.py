import pytest

from hwgenie.build import make_variants
from hwgenie.courseconfig import parse_course_config
from hwgenie.htmlgen import HtmlConverter
from hwgenie.themes import theme_css, theme_from_config

STY = (
    "\\newtheorem{theorem}{Theorem}[section]\n"
    "\\newtheorem{definition}[theorem]{Definition}\n"
    "\\numberwithin{equation}{subsection}\n"
    "\\def\\ZZ{\\mathbb{Z}}\n"
    "\\newenvironment{htmlonly}{\\comment}{\\endcomment}\n"
    "\\newenvironment{pdfonly}{}{}\n"
)


def test_theorems_and_macros_from_extra_preamble():
    conv = HtmlConverter(
        "\\begin{document}\n\\begin{theorem}\nX.\n\\end{theorem}\n"
        "\\begin{equation}\n\\label{e}\n1+1\n\\end{equation}\n\\end{document}",
        section="2",
        extra_preamble=STY,
    )
    html = conv.convert()
    assert '<p class="thm-head">Theorem 2.1</p>' in html
    assert "\\tag{2.0.1}" in html


def test_htmlonly_pdfonly_routing_in_html():
    conv = HtmlConverter(
        "\\begin{document}\n"
        "\\begin{htmlonly}\nWeb bonus text.\n\\end{htmlonly}\n"
        "\\begin{pdfonly}\nPrint note text.\n\\end{pdfonly}\n"
        "\\end{document}",
        extra_preamble=STY,
    )
    html = conv.convert()
    assert "Web bonus text." in html
    assert "Print note text." not in html


def test_submission_strips_htmlonly_keeps_pdfonly():
    doc = (
        "\\documentclass{article}\n\\begin{document}\n"
        "%===hwgenie===\n% number = 1\n% course = Math 261\n"
        "% semester = Fall 2025\n%====\n%HEADER\n"
        "\\begin{htmlonly}\nWeb bonus.\n\\end{htmlonly}\n"
        "\\begin{pdfonly}\nPrint note.\n\\end{pdfonly}\n"
        "\\end{document}\n"
    )
    v = make_variants(doc)
    assert "Web bonus." not in v["submission"]
    assert "Print note." in v["submission"]
    # PDFs rely on the comment environment, so htmlonly stays in those variants
    assert "Web bonus." in v["handout"]


def test_theme_css_overrides():
    css = theme_css("slate", {"light.accent": "#8c2f22", "font-body": "Palatino"})
    assert "--accent: #8c2f22;" in css
    assert "--font-body: Palatino;" in css.split("@media")[0]
    # dark accent untouched
    assert "--accent: #8db1ea;" in css


def test_theme_unknown_raises():
    with pytest.raises(KeyError, match="Unknown theme"):
        theme_css("neon")


def test_theme_from_config_dotted_keys():
    cfg = parse_course_config(
        "course: Math 261\ntheme: slate\ntheme.dark.bg: '#000000'\n"
    )
    css = theme_from_config(cfg)
    assert "--bg: #000000;" in css
    assert "--bg: #faf9f6;" in css  # light untouched
