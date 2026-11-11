"""Tests for prerender: export-time markers + KaTeX for offline pages."""

import pytest

from hwgenie import prerender
from hwgenie.prerender import place_markers, split_math

pytestmark = pytest.mark.skipif(not prerender.available(),
                                reason="quickjs not installed")


def mark(i):
    return prerender.marker_html(i)


def test_split_math_matches_auto_render():
    assert split_math("a $x$ b") == [
        ("text", "a "), ("math", "x", False, "$x$"), ("text", " b")]
    # $$ wins over $; braces hide a closing delimiter
    assert split_math("$$\\text{$y$}$$")[0][:3] == (
        "math", "\\text{$y$}", True)
    # an unclosed opener leaves the rest as text
    assert split_math("cost $5") == [("text", "cost "), ("text", "$5")]


def test_marker_after_anchor_text():
    html = "<p>We have $x &lt; y$ so done.</p>"
    out = place_markers(html, [{"anchor": "so   done"}])
    assert out == f"<p>We have $x &lt; y$ so done{mark(0)}.</p>"


def test_marker_inside_math_moves_past_it_and_entities_decode():
    # the anchor ends inside the formula, and is matched against the
    # decoded text (&lt; is <)
    out = place_markers("<p>Then $x &lt; y+1$ holds.</p>",
                        [{"anchor": "x < y"}])
    assert out == f"<p>Then $x &lt; y+1${mark(0)} holds.</p>"


def test_marker_in_display_goes_after_it_and_order_is_kept():
    html = ('<div class="math-display">\\[ a+b \\]</div><p>end</p>')
    out = place_markers(html, [{"anchor": "a+b"}, {"anchor": "b"},
                               {"anchor": None}, {"anchor": "missing"}])
    assert out == ('<div class="math-display">\\[ a+b \\]</div>'
                   f"{mark(0)}{mark(1)}<p>end</p>")


def test_block_boundary_counts_as_space():
    out = place_markers("<p>first</p><p>second</p>",
                        [{"anchor": "first second"}])
    assert out == f"<p>first</p><p>second{mark(0)}</p>"


def test_typeset_uses_region_macros_and_skips_code():
    r = prerender.Renderer()
    reg = r.region("a", {"\\ZZ": "\\mathbb{Z}"})
    out = r.typeset("<p>$n\\in\\ZZ$ and <code>$raw$</code></p>", reg)
    assert 'class="katex"' in out
    assert "mathbb" not in out.split("<annotation")[0]   # macro expanded
    assert "<code>$raw$</code>" in out
    assert not r.failed
    # the stylesheet embeds the fonts this math needs (AMS for \mathbb)
    css = prerender.katex_style(r.rendered)
    assert "KaTeX_AMS-Regular.woff2" not in css      # inlined, not linked
    assert css.count("data:font/woff2") >= 2
    assert "fonts/KaTeX_Fraktur" not in css


def test_foldeq_prerendered_fully_folded():
    r = prerender.Renderer()
    reg = r.region("a", {})
    out = r.typeset('<div class="foldeq" data-tex="a \\fold{=} b '
                    '\\fold{=} c"></div>', reg)
    assert out.startswith('<div class="foldeq" data-tex=')
    assert 'class="katex-display"' in out
    assert "aligned" in out          # folded rows
