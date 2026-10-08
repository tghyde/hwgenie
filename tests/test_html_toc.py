"""A standalone page (built without a home link) keeps the sticky bar, since
its "Contents" button is the only way to open the ToC at narrow widths."""

from hwgenie.build import BuildResult, build_html
from hwgenie.metadata import Metadata

WITH_HEADINGS = r"""
\begin{document}
\hwmaketitle
\section*{First part}
Text.
\subsection*{A step}
More text.
\end{document}
"""

NO_HEADINGS = r"""
\begin{document}
\hwmaketitle
Just a paragraph.
\end{document}
"""


def _build(tmp_path, text, **kwargs):
    meta = Metadata(number="1", course="Math 1", semester="Fall 2026",
                    doc_type="handout", title="Notes")
    out = tmp_path / "index.html"
    result = BuildResult(meta=meta, out_dir=tmp_path)
    build_html(text, meta, include_solutions=False, out_path=out,
               source_dir=tmp_path, result=result, **kwargs)
    assert not result.errors, result.errors
    return out.read_text(encoding="utf-8")


def test_standalone_page_keeps_contents_toggle(tmp_path):
    html = _build(tmp_path, WITH_HEADINGS)
    assert 'id="toc"' in html
    assert 'id="scrollnav"' in html
    assert 'class="sb-toc" aria-controls="toc"' in html
    assert "←" not in html          # no back link without a home


def test_home_link_still_rendered_when_given(tmp_path):
    html = _build(tmp_path, WITH_HEADINGS, sb_home=("../", "Course"))
    assert '<a href="../">← Course</a>' in html
    assert 'class="sb-toc" aria-controls="toc"' in html


def test_page_without_headings_has_no_bar(tmp_path):
    html = _build(tmp_path, NO_HEADINGS)
    assert 'id="toc"' not in html
    assert 'id="scrollnav"' not in html
