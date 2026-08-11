from hwgenie.htmlgen import HtmlConverter

PREAMBLE = (
    "\\newtheorem{theorem}{Theorem}[section]\n"
    "\\newtheorem{proposition}[theorem]{Proposition}\n"
    "\\newtheorem{definition}[theorem]{Definition}\n"
    "\\newtheorem*{remark}{Remark}\n"
    "\\numberwithin{equation}{subsection}\n"
)


def convert(body: str, section="1"):
    conv = HtmlConverter(
        PREAMBLE + "\\begin{document}\n" + body + "\n\\end{document}",
        include_solutions=True,
        section=section,
    )
    return conv, conv.convert()


def test_theorem_numbering_shared_counters():
    _c, html = convert(
        "\\begin{theorem}\nA.\n\\end{theorem}\n"
        "\\begin{definition}\nB.\n\\end{definition}\n"
        "\\begin{proposition}\nC.\n\\end{proposition}\n"
        "\\begin{remark}\nD.\n\\end{remark}"
    )
    assert '<p class="thm-head">Theorem 1.1</p>' in html
    assert '<p class="thm-head">Definition 1.2</p>' in html
    assert '<p class="thm-head">Proposition 1.3</p>' in html
    assert '<p class="thm-head">Remark</p>' in html


def test_theorem_optional_title_and_ref():
    _c, html = convert(
        "\\begin{theorem}[Division with Remainder]\n\\label{thm div}\nStatement.\n"
        "\\end{theorem}\n"
        "By Theorem \\ref{thm div} we win."
    )
    assert 'Theorem 1.1 <span class="thm-note">(Division with Remainder)</span>' in html
    assert 'id="thm-1.1"' in html
    assert 'By Theorem <a class="xref" href="#thm-1.1">1.1</a> we win.' in html


def test_forward_reference_resolves():
    _c, html = convert(
        "See Theorem \\ref{later}.\n"
        "\\begin{theorem}\n\\label{later}\nX.\n\\end{theorem}"
    )
    assert 'See Theorem <a class="xref" href="#thm-1.1">1.1</a>.' in html


def test_equation_numbering_and_eqref():
    conv, html = convert(
        "\\begin{equation}\n\\label{eqn logic ex}\n(P \\wedge Q) \\Rightarrow R.\n"
        "\\end{equation}\n"
        "So \\eqref{eqn logic ex} is read aloud."
    )
    assert "\\tag{1.0.1}" in html
    assert 'id="eq-1.0.1"' in html
    assert "\\label" not in html
    assert '(<a class="xref" href="#eq-1.0.1">1.0.1</a>) is read aloud' in html


def test_proof_block():
    _c, html = convert("\\begin{proof}\nObvious.\n\\end{proof}")
    assert '<div class="proof">' in html
    assert '<p class="proof-label">Proof</p>' in html
    assert 'Obvious.' in html


def test_footnotes():
    _c, html = convert(
        "Axioms are nice.\\footnote{Said \\textbf{everyone}.} More text."
    )
    assert '<sup class="fn"><a href="#fn-1" id="fnref-1">1</a></sup>' in html
    assert '<li id="fn-1">Said <strong>everyone</strong>.' in html


def test_accents():
    _c, html = convert("Garc\\'ia M\\'arquez and G\\\"odel and \\'{E}cole.")
    assert "García Márquez" in html
    assert "Gödel" in html
    assert "École" in html


def test_problem_label_ref():
    _c, html = convert(
        "\\begin{problem}\n\\label{prob one}\nDo it.\n\\end{problem}\n"
        "Recall Problem \\ref{prob one}."
    )
    assert 'Recall Problem <a class="xref" href="#problem-1.1">1.1</a>.' in html


def test_unresolved_ref_warns():
    conv, html = convert("See \\ref{nope}.")
    assert "See ??." in html
    assert any("Unresolved" in w for w in conv.warnings)


def test_punctuation_clings_to_inline_math():
    _c, html = convert("We know $x = 1$, so $y = 2$.")
    assert '<span class="nw">$x = 1$,</span>' in html
    assert '<span class="nw">$y = 2$.</span>' in html


def test_solutions_carry_no_qed_marks():
    _c, html = convert(
        "\\begin{problem}\nP.\n\\begin{solution}\n"
        "\\begin{enumerate}\n\\item[(c)] $(q,r) = (19,14)$\\qedhere\n"
        "\\end{enumerate}\n"
        "Also \\[\nx = 1.\\qedhere\n\\]\n\\end{solution}\n\\end{problem}"
    )
    assert "qedbox" not in html
    assert "\\qedhere" not in html
    assert "\\tag*" not in html
    assert "\\square" not in html


def test_qedhere_in_proof_display_math_becomes_tag():
    _c, html = convert(
        "\\begin{proof}\nSo\n\\[\n(-51,-2), (42,84).\\qedhere\n\\]\n\\end{proof}"
    )
    assert "\\qedhere" not in html
    assert "\\tag*{$\\square$}" in html
    assert 'class="proof has-qedhere"' in html


def test_qedhere_in_proof_align_star_stays_on_its_line():
    _c, html = convert(
        "\\begin{proof}\n\\begin{align*}\nx &= 1 \\\\\ny &= 2\\qedhere\n"
        "\\end{align*}\n\\end{proof}"
    )
    assert "y &amp;= 2\\qquad\\square" in html
    assert "\\tag*" not in html


def test_epigraph_macro():
    _c, html = convert(
        "\\epigraph{Number is the ruler of forms and ideas.}"
        "{Attributed to \\textit{Pythagoras}}"
    )
    assert '<blockquote class="epigraph">' in html
    assert "<p>Number is the ruler of forms and ideas.</p>" in html
    assert "<footer>Attributed to <em>Pythagoras</em></footer>" in html


def test_separate_emits_nothing():
    _c, html = convert("Before.\n\\separate\nAfter.")
    assert "<hr" not in html


def test_problem_is_collapsible_details():
    _c, html = convert("\\begin{problem}\nDo.\n\\end{problem}")
    assert '<details class="problem" open id="problem-1.1">' in html
    assert "<summary><h2" in html


def test_subsection_becomes_centered_heading():
    _c, html = convert(
        "\\subsection{Modular Arithmetic}\nText.\n"
        "\\subsection{Orders modulo $m$}\nMore."
    )
    assert ('<h2 class="sec-head" id="sec-1.1">'
            '<span class="sec-num">1.1</span>Modular Arithmetic</h2>') in html
    assert '<span class="sec-num">1.2</span>' in html


def test_problem_optional_title_in_summary():
    _c, html = convert(
        "\\begin{problem}[Chinese Remainder Theorem]\nDo it.\n\\end{problem}"
    )
    assert ('Problem 1.1 <span class="problem-note">'
            "· Chinese Remainder Theorem</span>") in html
    assert "[Chinese Remainder Theorem]" not in html


def test_proof_custom_label():
    _c, html = convert(
        "\\begin{proof}[Proof of Theorem 3]\nEasy.\n\\end{proof}"
    )
    assert '<p class="proof-label">Proof of Theorem 3</p>' in html


def test_problem_brace_protected_title():
    _c, html = convert(
        "\\begin{problem}[{CRT for $\\ZZ[i]$}]\nDo it.\n\\end{problem}"
    )
    assert "problem-note" in html
    assert "CRT for" in html
    assert "[CRT" not in html and "[{CRT" not in html
