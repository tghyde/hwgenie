from pathlib import Path

import pytest

from hwgenie import tikzsvg
from hwgenie.htmlgen import HtmlConverter

DOC = """\\documentclass[11pt]{article}
\\usepackage{tikz}
\\begin{document}
Before.
\\begin{tikzpicture}
\\draw[->] (0,0) -- (1,0);
\\end{tikzpicture}
After.
\\end{document}
"""


def test_tikz_positions_document_order():
    text = (
        "\\begin{document}"
        "\\begin{tikzpicture}a\\end{tikzpicture}"
        "mid"
        "\\begin{tikzcd}b\\end{tikzcd}"
        "\\end{document}"
    )
    pos = tikzsvg.tikz_positions(text)
    assert len(pos) == 2
    assert text[pos[0]:].startswith("\\begin{tikzpicture}")
    assert text[pos[1]:].startswith("\\begin{tikzcd}")


def test_tikz_positions_skips_verbatim():
    text = (
        "\\begin{document}"
        "\\begin{verbatim}\\begin{tikzpicture}\\end{verbatim}"
        "\\end{document}"
    )
    assert tikzsvg.tikz_positions(text) == []


def test_inject_preview_places_driver_and_preview():
    injected = tikzsvg._inject_preview(DOC)
    assert injected is not None
    # Driver must precede the tikz load; preview must precede \begin{document}.
    assert injected.index("pgfsys-dvisvgm.def") < injected.index(
        "\\usepackage{tikz}"
    )
    assert injected.index("{preview}") < injected.index("\\begin{document}")
    assert injected.index("\\PreviewEnvironment{tikzpicture}") < injected.index(
        "\\begin{document}"
    )


def test_inject_preview_requires_document_markers():
    assert tikzsvg._inject_preview("no preamble here") is None


def test_postprocess_recolors_black_to_currentcolor():
    svg = "<svg width='10pt' height='20pt'><g fill='#000' stroke='#000'>" \
          "<path fill='none' stroke='#f00'/></g></svg>"
    out = tikzsvg.postprocess(svg, "tz1")
    assert "fill='currentColor'" in out
    assert "stroke='currentColor'" in out
    # Non-black colors are kept as authored.
    assert "stroke='#f00'" in out
    assert "fill='none'" in out


def test_postprocess_namespaces_ids():
    svg = ("<svg width='10pt' height='10pt'>"
           "<clipPath id='cp1'/><g clip-path='url(#cp1)'/>"
           "<use xlink:href='#g1'/><path id='g1'/></svg>")
    out = tikzsvg.postprocess(svg, "tzA")
    assert "id='tzA-cp1'" in out
    assert "url(#tzA-cp1)" in out
    assert "id='tzA-g1'" in out
    assert "href='#tzA-g1'" in out
    assert "id='cp1'" not in out


def test_postprocess_makes_size_fluid():
    svg = "<svg version='1.1' width='150pt' height='75pt' viewBox='0 0 150 75'></svg>"
    out = tikzsvg.postprocess(svg, "tz1")
    assert "width='150pt'" not in out
    assert "style='width:200.0px;max-width:100%;height:auto'" in out
    assert "viewBox='0 0 150 75'" in out


def test_postprocess_strips_xml_prolog():
    svg = ("<?xml version='1.0'?>\n<!-- comment -->\n"
           "<svg width='10pt' height='10pt'></svg>")
    out = tikzsvg.postprocess(svg, "tz1")
    assert not out.startswith("<?xml")
    assert "<!--" not in out


def test_render_document_empty_when_no_tikz(tmp_path):
    svgs, warning = tikzsvg.render_document(
        "\\documentclass{article}\\begin{document}x\\end{document}",
        workdir=tmp_path,
    )
    assert svgs == {} and warning is None


def test_htmlconverter_inlines_svg_when_provided():
    text = ("\\begin{document}\\begin{tikzpicture}x\\end{tikzpicture}"
            "\\end{document}")
    pos = tikzsvg.tikz_positions(text)[0]
    conv = HtmlConverter(text, tikz_svgs={pos: "<svg>D</svg>"})
    html = conv.convert()
    assert '<div class="tikz-figure"><svg>D</svg></div>' in html
    assert "see the PDF" not in html


def test_htmlconverter_placeholder_without_svg():
    text = ("\\begin{document}\\begin{tikzpicture}x\\end{tikzpicture}"
            "\\end{document}")
    conv = HtmlConverter(text)
    html = conv.convert()
    assert "see the PDF" in html
    assert any("placeholder" in w for w in conv.warnings)


@pytest.mark.skipif(not tikzsvg.available(), reason="latex/dvisvgm not found")
def test_render_document_end_to_end(tmp_path):
    svgs, warning = tikzsvg.render_document(DOC, workdir=tmp_path)
    assert warning is None
    assert len(svgs) == 1
    (svg,) = svgs.values()
    assert svg.startswith("<svg")
    assert "currentColor" in svg
    pos = tikzsvg.tikz_positions(DOC)[0]
    assert pos in svgs


def test_figure_embedded_tikz_uses_svg():
    text = ("\\begin{document}\\begin{figure}\\centering"
            "\\begin{tikzpicture}x\\end{tikzpicture}"
            "\\caption{A wheel}\\end{figure}\\end{document}")
    pos = tikzsvg.tikz_positions(text)[0]
    conv = HtmlConverter(text, tikz_svgs={pos: "<svg>W</svg>"})
    html = conv.convert()
    assert '<div class="tikz-figure"><svg>W</svg></div>' in html
    assert "<figcaption>A wheel</figcaption>" in html
    assert "see the PDF" not in html
