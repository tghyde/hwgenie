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


def test_blue_around_block_content_becomes_div():
    # math301 ps03 3.1.5: \blue{...} wrapping text + an enumerate. An inline
    # <span> here would nest <p> inside <p> and the browser drops the class.
    _c, html = convert(
        "\\begin{problem}\n\\begin{enumerate}\n"
        "\\item \\blue{Prove that the following are equivalent\n"
        "\\begin{enumerate}\n\\item $x$,\n\\item $y$.\n\\end{enumerate}\n}\n"
        "\\end{enumerate}\n\\end{problem}"
    )
    assert '<div class="task">\n<p>Prove that the following are equivalent</p>' in html
    assert "</ol>\n</div>" in html
    assert '<span class="task"><p>' not in html
    assert "<p><div" not in html
    # \textcolor takes the same path; plain inline use is unchanged.
    _c, html = convert(
        "\\begin{problem}\n\\textcolor{red}{Note\n\n\\begin{itemize}\\item a\\end{itemize}}"
        " and \\textcolor{red}{inline}.\n\\end{problem}"
    )
    assert '<div class="alert">\n<p>Note</p>\n<ul>' in html
    assert '<span class="alert">inline</span>' in html


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


def test_fold_script_serves_grader_and_feedback_pages():
    """foldeq displays are empty <div data-tex> until the fold script
    renders them; the grading pages must carry it (v0.51.1: instructor
    solutions with foldeq showed a blank line in hwGrader)."""
    from hwgenie.feedback import FEEDBACK_PAGE
    from hwgenie.grade_gui import render_grader
    from hwgenie.htmltemplate import FOLD_JS

    grader = render_grader("/nowhere")
    assert "function fitFolds(root, macros)" in FOLD_JS
    assert "function fitFolds(root, macros)" in grader
    assert "fitFolds(el, macros || {})" in grader     # typeset() hook
    assert "__FOLDJS__" not in grader
    assert "__FOLDJS__" in FEEDBACK_PAGE               # filled at render
    assert "fitFolds(el, macros || {})" in FEEDBACK_PAGE
    # hidden displays render unfolded instead of staying blank
    assert "el.clientWidth === 0" in FOLD_JS


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


def test_katex_arraystretch_predefined():
    # KaTeX does not predefine \arraystretch, so a solution's
    # \renewcommand{\arraystretch}{1.3} inside \[...\] was a parse error.
    assert extract_macros("")["\\arraystretch"] == "1"
    # A preamble-level redefinition still wins over the default.
    macros = extract_macros("\\renewcommand{\\arraystretch}{1.2}\n\\begin{document}")
    assert macros["\\arraystretch"] == "1.2"


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


def test_toc_html_normalizes_levels_and_numbers():
    from hwgenie.htmltemplate import toc_html
    assert toc_html([]) == ""
    toc = toc_html([
        (2, "1.1", "The Basics", "sec-1.1"),
        (3, "", "Cyclic $G$", "sec-cyclic-g"),
    ])
    assert '<nav class="toc" id="toc"' in toc
    # A handout made only of subsections is not indented.
    assert ('<li class="toc-l1"><a href="#sec-1.1">'
            '<span class="toc-num">1.1</span>The Basics</a></li>') in toc
    assert '<li class="toc-l2"><a href="#sec-cyclic-g">Cyclic $G$</a></li>' in toc
    # bare call: generic title, no back link, always a back-to-top link
    assert '<p class="toc-title">Contents</p>' in toc
    assert "toc-home" not in toc
    assert toc.rstrip().endswith(
        '<p class="toc-top"><a href="#top">↑ Top</a></p>\n</nav>')


def test_toc_html_sidebar_carries_home_and_page_label():
    from hwgenie.htmltemplate import toc_html
    toc = toc_html([(1, "", "Problem 3.1", "problem-3.1")],
                   home=("../../#problem-sets", "Math <221>"), label="PS 3 · Solutions")
    assert toc.startswith(
        '<nav class="toc" id="toc" aria-label="Table of contents">\n'
        '<p class="toc-home"><a href="../../#problem-sets">← Math &lt;221&gt;</a></p>\n'
        '<p class="toc-title">PS 3 · Solutions</p>\n<ul>\n')
    assert '<li class="toc-l1"><a href="#problem-3.1">Problem 3.1</a></li>' in toc


def test_scrollbar_contents_toggle():
    from hwgenie.htmltemplate import scrollbar_html
    plain = scrollbar_html("../", "Home", "Handout 1")
    assert "sb-toc" not in plain
    with_toc = scrollbar_html("../", "Home", "Handout 1", toc_toggle=True)
    assert ('<button type="button" class="sb-toc" aria-controls="toc" '
            'aria-expanded="false">Contents</button>') in with_toc
    assert with_toc.index("sb-label") < with_toc.index("sb-toc") < with_toc.index("sb-top")
    # jump links live in the ToC now, never in the bar
    assert "sb-jumps" not in with_toc


def test_matrix_colspec_rewritten_for_katex():
    from hwgenie.htmlgen import _matrix_colspec_to_array as fix

    # Dividers or mixed alignments: delimited array.
    assert fix("\\begin{bmatrix}[rr|r] 1 & 2 & 3 \\\\ 4 & 5 & 6 \\end{bmatrix}") == (
        "\\left[\\begin{array}{rr|r} 1 & 2 & 3 \\\\ 4 & 5 & 6 \\end{array}\\right]"
    )
    assert fix("\\begin{pmatrix}[rc] 1 & 2 \\end{pmatrix}") == (
        "\\left(\\begin{array}{rc} 1 & 2 \\end{array}\\right)"
    )
    # Uniform alignment: KaTeX's starred matrix keeps native spacing.
    assert fix("\\begin{pmatrix}[r] 1 & -2 \\\\ 3 & 4 \\end{pmatrix}") == (
        "\\begin{pmatrix*}[r] 1 & -2 \\\\ 3 & 4 \\end{pmatrix*}"
    )
    assert fix("\\begin{bmatrix}[rrr] 1 & 2 & 3 \\end{bmatrix}") == (
        "\\begin{bmatrix*}[r] 1 & 2 & 3 \\end{bmatrix*}"
    )
    # Column vectors, several in one expression.
    out = fix("v_1 = \\begin{bmatrix}[r] 1 \\\\ -1 \\end{bmatrix}, v_2 = \\begin{bmatrix}[r] 2 \\\\ -3 \\end{bmatrix}")
    assert out.count("\\begin{bmatrix*}[r]") == 2 and "[r] 1" in out and "[r] 2" in out
    # Nested same-named environments and braces do not confuse the scan.
    out = fix("\\begin{bmatrix}[c|c] \\begin{bmatrix} 1 \\\\ 2 \\end{bmatrix} & {a & b} \\end{bmatrix}")
    assert out.startswith("\\left[\\begin{array}{c|c}") and out.endswith("\\end{array}\\right]")
    assert "\\begin{bmatrix} 1 \\\\ 2 \\end{bmatrix}" in out
    # Plain matrices pass through untouched.
    plain = "\\begin{bmatrix} 1 & 2 \\end{bmatrix} \\begin{matrix}[l] x \\end{matrix}"
    assert fix(plain) == "\\begin{bmatrix} 1 & 2 \\end{bmatrix} \\begin{matrix*}[l] x \\end{matrix*}"


def test_matrix_colspec_applied_in_inline_and_display_math():
    _c, html = convert(
        "Let $v = \\begin{bmatrix}[r] 1 \\\\ -2 \\end{bmatrix}$ and\n"
        "\\begin{align*} A &= \\begin{bmatrix}[rr|r] 1 & 0 & 2 \\\\ 0 & 1 & 3 \\end{bmatrix} \\end{align*}"
    )
    assert "[rr|r]" not in html and "\\begin{bmatrix}[r]" not in html
    assert "\\begin{bmatrix*}[r] 1" in html
    assert "\\left[\\begin{array}{rr|r} 1" in html


def test_body_renewcommand_and_font_size_dropped():
    # Row Reducer export prefix: nothing from it may leak into the prose.
    conv, html = convert(
        "Row reducing gives\n"
        "{\\small\\setlength{\\arraycolsep}{9pt}\\renewcommand{\\arraystretch}{1.15}\n"
        "\\[ A \\]}\nand then \\renewcommand*{\\foo}[1]{bar #1} done."
    )
    assert "1.15" not in html and "9pt" not in html
    assert "arraystretch" not in html and "bar" not in html
    assert "Row reducing gives" in html and "done." in html
    assert not any("Unknown macro" in w for w in conv.warnings)


def test_tabular_inside_display_math_becomes_equation_table():
    # \[ \begin{tabular}...\end{tabular} \] is legal LaTeX but KaTeX has no
    # tabular: render it as a centred, headerless table instead.
    _c, html = convert(
        "we get\n\\[\n\\begin{tabular}{rrcl}\n"
        "    $A:$& $x_3 + 90$ &$=$&$ x_1 + 100$\\\\\n"
        "    $B:$& $x_1 + 40$ &$=$&$ x_2 + x_4$\n"
        "\\end{tabular}\n\\]\ndone."
    )
    assert "\\[" not in html and "tabular" not in html
    assert '<div class="center">' in html
    assert '<div class="table-wrap eqtab">' in html
    assert "<th" not in html
    assert '<td class="al-right">$A:$</td>' in html
    assert '<td class="al-left">$ x_1 + 100$</td>' in html
    assert "<p>we get</p>" in html and "<p>done.</p>" in html
    # a plain display equation is untouched
    _c, html = convert("\\[ x = 1 \\]")
    assert "\\[ x = 1 \\]" in html


def test_tabular_row_spacing_is_not_a_row():
    # \\[1.6em] at the end of a row leaves "[1.6em]" at the start of the next
    # chunk after splitting on \; it is spacing, not a one-cell row.
    _, html = convert(
        "\\begin{tabular}{|l|c|}\n\\hline\n"
        "\\textbf{Points} & 12\\\\\n\\hline\n"
        "\\textbf{Score} & \\\\[1.6em]\n\\hline\n"
        "\\end{tabular}"
    )
    assert "[1.6em]" not in html
    assert html.count("<tr>") == 2


def test_solbox_boxed_in_solutions_plain_in_handout():
    body = "\\blue{No solutions \\qquad \\solbox{One solution} \\qquad Many}"
    _, sol = convert(body, include_solutions=True)
    assert '<span class="fbox">One solution</span>' in sol
    _, hand = convert(body, include_solutions=False)
    assert "fbox" not in hand
    assert "One solution" in hand
    # \fbox always boxes; \handoutspace is PDF-only layout.
    _, html = convert("Pick \\fbox{$x_2$} here. \\handoutspace{2in} Next.")
    assert '<span class="fbox">' in html and "2in" not in html
    assert "Next." in html


def test_hrulefill_renders_as_blank_line():
    _, html = convert("\\noindent\\textbf{Name:}\\ \\hrulefill")
    assert '<span class="hrulefill"></span>' in html
    assert "Unknown macro \\hrulefill" not in " ".join(_.warnings)
