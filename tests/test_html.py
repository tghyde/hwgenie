from pathlib import Path

import pytest

from hwgenie.build import build
from hwgenie.htmlgen import HtmlConverter
from hwgenie.katexmacros import extract_macros

SAMPLE = Path(__file__).resolve().parents[2] / "sample" / "Problem Set 3 [source] (Math 261 Fall 2025).tex"


def convert(body: str, include_solutions=True, section="3"):
    conv = HtmlConverter(
        "\\begin{document}\n" + body + "\n\\end{document}",
        include_solutions=include_solutions,
        section=section,
    )
    return conv, conv.convert()


def test_variant_newpages_ignored():
    _c, html = convert(
        "\\begin{problem}\nA.\n\\end{problem}\n\\solnewpage\n"
        "\\handoutnewpage\n\\begin{problem}\nB.\n\\end{problem}"
    )
    assert "newpage" not in html
    assert "A." in html and "B." in html


def test_math_passthrough_escaped():
    _c, html = convert("Let $a < b$ and \\[ x \\geq 1. \\]")
    assert "$a &lt; b$" in html
    assert "\\[ x \\geq 1. \\]" in html


def test_problem_numbering_and_task_span():
    _c, html = convert(
        "\\begin{problem}\n\\blue{Prove $1+1=2$.}\n\\end{problem}\n"
        "\\begin{problem}\nSecond.\n\\end{problem}"
    )
    assert "Problem 3.1" in html and "Problem 3.2" in html
    assert '<span class="task">Prove <span class="nw">$1+1=2$.</span></span>' in html


def test_solutions_toggle():
    body = "\\begin{problem}\nP\n\\begin{solution}\nSecret.\n\\end{solution}\n\\end{problem}"
    _c, with_sol = convert(body, include_solutions=True)
    _c, without = convert(body, include_solutions=False)
    assert "Secret." in with_sol and "<details" in with_sol
    assert "Secret." not in without


def test_enumerate_custom_labels():
    _c, html = convert(
        "\\begin{enumerate}\n\\item First\n\\end{enumerate}\n"
        "\\begin{enumerate}\n\\item[2.] Second\n\\item[3.] Third\n\\end{enumerate}\n"
        "\\begin{enumerate}\n\\item[(a)] Alpha\n\\item[(b)] Beta\n\\end{enumerate}"
    )
    assert "<ol>\n<li>First</li>\n</ol>" in html
    assert '<li value="2">Second</li>' in html
    assert '<ol type="a">' in html and '<li value="1">Alpha</li>' in html


def test_lstlisting_verbatim_content():
    _c, html = convert(
        "\\begin{lstlisting}[language=Python]\n"
        "    def V(r):\n"
        "        # 100% \\end-proof <html> & stuff\n"
        "        return r != 0\n"
        "\\end{lstlisting}"
    )
    assert '<code class="language-python">' in html
    assert "def V(r):" in html
    assert "# 100% \\end-proof &lt;html&gt; &amp; stuff" in html


def test_tabular_to_table():
    _c, html = convert(
        "\\begin{center}\n\\begin{tabular}{|c|l|}\n\\hline\n"
        "$p$ & 3 \\\\\n\\hline\nsquares & $x^2$ \\\\\n\\hline\n"
        "\\end{tabular}\n\\end{center}"
    )
    assert '<th class="al-center">$p$</th>' in html
    assert '<th class="al-left">3</th>' in html
    assert '<td class="al-center">squares</td>' in html
    assert "$x^2$" in html


def test_center_image_and_quote():
    conv, html = convert(
        "\\begin{center}\nWise words here.\n\\end{center}\n"
        "\\begin{center}\n\\includegraphics[scale=.25]{orchard.png}\n\\end{center}"
    )
    assert '<div class="center">\n<p>Wise words here.</p>\n</div>' in html
    assert '<img src="orchard.png"' in html
    assert conv.images == ["orchard.png"]


def test_special_chars_and_head():
    conv, html = convert(
        "\\head{MATH 261, Fall 2025\\\\ Problem Set 3: Digits}\n"
        "Dashes -- and --- plus \\texttt{\\#COND\\_TBD} and ``quotes''."
    )
    assert conv.title_lines == ["MATH 261, Fall 2025", "Problem Set 3: Digits"]
    assert "–" in html and "—" in html
    assert "<code>#COND_TBD</code>" in html
    assert "“quotes”" in html


def test_align_env_wrapped_for_katex():
    _c, html = convert("\\begin{align*}\nx &= 1 \\\\\ny &= 2\n\\end{align*}")
    assert "\\[\\begin{aligned}" in html
    assert "\\end{aligned}\\]" in html


def test_foldeq_star_emits_data_tex():
    _c, html = convert(
        "\\begin{foldeq*}\n"
        "    a + b &= (2j + 1) + (2k + 1) \\fold{=} 2(j + k + 1).\n"
        "\\end{foldeq*}"
    )
    assert '<div class="math-display foldeq"' in html
    # body is preserved verbatim (whitespace-collapsed) with markers intact
    assert ("data-tex=\"a + b &amp;= (2j + 1) + (2k + 1) "
            "\\fold{=} 2(j + k + 1).\"") in html
    assert "data-tag" not in html


def test_foldeq_numbered_tag_and_label():
    conv, html = convert(
        "\\begin{foldeq}\n\\label{eq fold}\nx &= y \\fold{=} z\n\\end{foldeq}\n"
        "See \\eqref{eq fold}."
    )
    assert 'data-tag="\\tag{1}"' in html
    assert 'id="eq-1"' in html
    assert conv.labels["eq fold"] == ("eq", "1")


def test_foldeq_qedhere_stripped_in_solutions():
    _c, html = convert(
        "\\begin{problem}\n\\begin{solution}\n"
        "\\begin{foldeq*}\na &= b \\fold{=} c\\qedhere\n\\end{foldeq*}\n"
        "\\end{solution}\n\\end{problem}"
    )
    assert "qedhere" not in html
    assert "\\square" not in html


def test_katex_macro_extraction():
    macros = extract_macros(
        "\\def\\ZZ{\\mathbb{Z}}\n"
        "\\newcommand{\\lcm}{\\mathrm{lcm}}\n"
        "\\newcommand{\\pow}[2]{#1^{#2}}\n"
        "\\DeclareMathOperator{\\ord}{ord}\n"
        "\\begin{document}\\def\\notme{x}\\end{document}"
    )
    assert macros["\\ZZ"] == "\\mathbb{Z}"
    assert macros["\\lcm"] == "\\mathrm{lcm}"
    assert macros["\\pow"] == "#1^{#2}"
    assert macros["\\ord"] == "\\operatorname{ord}"
    assert "\\notme" not in macros


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample file not present")
def test_sample_html_end_to_end(tmp_path):
    result = build(SAMPLE, out_dir=tmp_path, compile_pdfs=False)
    assert result.ok
    handout = result.files["handout_html"].read_text()
    solutions = result.files["solutions_html"].read_text()

    for page in (handout, solutions):
        assert "katex" in page
        assert "Problem 3.1" in page and "Problem 3.4" in page
        assert '"\\\\ZZ": "\\\\mathbb{Z}"' in page or "\\\\mathbb{Z}" in page
        assert "\\begin{problem}" not in page
        assert "%HEADER" not in page

    assert "Since 8 divides" not in handout
    assert "Since 8 divides" in solutions
    assert 'class="badge"' in solutions and 'class="badge"' not in handout
    # images copied next to the pages
    html_dir = result.files["handout_html"].parent
    for img in ("orchard.png", "r_is_4.jpeg", "vr_plot.png", "vr_plot_pi.png"):
        assert (html_dir / img).exists()
    # handout must not reference solution-only images
    assert "vr_plot.png" not in handout
    assert "vr_plot.png" in solutions
