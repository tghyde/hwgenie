import shutil
from pathlib import Path

import pytest

from hwgenie import texscan, transforms
from hwgenie.build import build, make_variants
from hwgenie.metadata import MetadataError, parse_metadata

SAMPLE = Path(__file__).resolve().parents[2] / "sample" / "Problem Set 3 [source] (Math 261 Fall 2025).tex"


def variants_of(body: str, meta: str = None) -> dict:
    meta = meta or (
        "%===hwgenie===\n"
        "% number = 1\n"
        "% course = Math 261\n"
        "% semester = Fall 2025\n"
        "%=============\n"
    )
    doc = (
        "\\documentclass{article}\n\\begin{document}\n"
        + meta + "%HEADER\n" + body + "\n\\end{document}\n"
    )
    return make_variants(doc)


# ------------------------------------------------------------------ metadata

def test_metadata_v2():
    text = (
        "%===hwgenie===\n"
        "% type = problemset\n"
        "% number   = 3\n"
        "% title = Digits and Sage\n"
        "% course = Math 261\n"
        "% semester = Fall 2025\n"
        "% solutions = 2025-10-15\n"
        "%=============\n"
    )
    m = parse_metadata(text)
    assert m.number == "3"
    assert m.course == "Math 261"
    assert m.title == "Digits and Sage"
    assert m.solutions_release == "2025-10-15"
    assert text[m.span[0]:m.span[1]] == text


def test_metadata_v2_bare_course_number_gets_prefix():
    m = parse_metadata(
        "%===hwgenie===\n% number = 1\n% course = 261\n% semester = Fall 2025\n%====\n"
    )
    assert m.course == "Math 261"


def test_metadata_legacy():
    text = (
        "\\begin{document}\n"
        "%Problem Set Data\n"
        "%number = 7\n"
        "%course = 261\n"
        "%semester = Spring 2025\n"
        "%path = /tmp/some where\n"
        "\n\\head{...}\n"
    )
    m = parse_metadata(text)
    assert m.fmt == "legacy"
    assert m.course == "Math 261"
    assert m.legacy_path == "/tmp/some where"
    assert text[m.span[0]:m.span[1]].startswith("%Problem Set Data")
    assert text[m.span[0]:m.span[1]].rstrip().endswith("where")


def test_metadata_missing_number_is_error():
    with pytest.raises(MetadataError, match="number"):
        parse_metadata("%===hwgenie===\n% course = 261\n% semester = Fall 2025\n%====\n")


def test_metadata_course_semester_optional():
    m = parse_metadata("%===hwgenie===\n% number = 1\n%====\n")
    assert m.course is None and m.semester is None


def test_metadata_absent():
    with pytest.raises(MetadataError, match="No metadata"):
        parse_metadata("\\documentclass{article}")


# ----------------------------------------------------------------- solutions

def test_solution_blanked_with_indent():
    v = variants_of(
        "\\begin{problem}\n"
        "    Statement.\n"
        "    \\begin{solution}\n"
        "        Secret answer.\n"
        "    \\end{solution}\n"
        "\\end{problem}\n"
    )
    assert "Secret answer" not in v["submission"]
    assert "    \\begin{solution}\n    \t%Write your solution here\n    \\end{solution}" in v["submission"]
    assert "Secret answer" not in v["handout"]
    assert "\\begin{solution}" not in v["handout"]
    assert "Secret answer" in v["solutions"]


def test_commented_end_solution_does_not_truncate():
    # The old regex approach would have ended the environment at the
    # commented-out \end{solution}.
    v = variants_of(
        "\\begin{problem}\nS.\n"
        "\\begin{solution}\n"
        "Part one.\n"
        "%\\end{solution} -- commented out!\n"
        "Part two secret.\n"
        "\\end{solution}\n"
        "\\end{problem}\n"
    )
    assert "Part two secret" not in v["submission"]
    assert "commented out" not in v["submission"]


# ------------------------------------------------------------------- figures

def test_center_includegraphics_removed_quote_center_kept():
    v = variants_of(
        "\\begin{center}\nA lovely quotation.\n\\end{center}\n"
        "\\begin{problem}\nLook:\n"
        "\\begin{center}\n\\includegraphics[scale=.25]{orchard.png}\n\\end{center}\n"
        "\\end{problem}\n"
    )
    assert "includegraphics" not in v["submission"]
    assert "A lovely quotation." in v["submission"]
    # handout keeps images
    assert "includegraphics" in v["handout"]


def test_figure_env_removed():
    v = variants_of(
        "\\begin{problem}\nP.\n"
        "\\begin{figure}[h]\n\\centering\n\\includegraphics{x.png}\n\\caption{c}\n\\end{figure}\n"
        "\\end{problem}\n"
    )
    assert "includegraphics" not in v["submission"]
    assert "caption" not in v["submission"]


def test_figure_inside_removed_solution_no_conflict():
    v = variants_of(
        "\\begin{problem}\nP.\n"
        "\\begin{solution}\nSee:\n"
        "\\begin{center}\\includegraphics{a.png}\\end{center}\n"
        "\\end{solution}\n"
        "\\end{problem}\n"
    )
    assert "includegraphics" not in v["submission"]
    assert "%Write your solution here" in v["submission"]


# -------------------------------------------------------------------- tables

CLEAR_TABLE = (
    "\\begin{problem}\nFill in:\n"
    "\\begin{center}\n"
    "\\begin{tabular}{|c|c|c|} %CLEAR\n"
    "    \\hline\n"
    "    $p$ & 3 & 5 \\\\\n"
    "    \\hline\n"
    "    answer & 2 & $\\begin{pmatrix} 1 & 0 \\\\ 0 & 1 \\end{pmatrix}$ \\\\\n"
    "    \\hline\n"
    "\\end{tabular}\n"
    "\\end{center}\n"
    "\\end{problem}\n"
)


def test_clear_table_keeps_header_and_first_column():
    v = variants_of(CLEAR_TABLE)
    sub = v["submission"]
    assert "%CLEAR" not in sub
    assert "$p$ & 3 & 5" in sub          # header row intact
    assert "answer" in sub               # first column intact
    assert "pmatrix" not in sub          # data cell (with nested & and \\) cleared
    # solutions keep the filled table
    assert "pmatrix" in v["solutions"]
    # handout also gets the cleared table
    assert "pmatrix" not in v["handout"]


def test_table_without_clear_untouched():
    v = variants_of(CLEAR_TABLE.replace(" %CLEAR", ""))
    assert "pmatrix" in v["submission"]


KEEP_ARRAY = (
    "\\begin{problem}\nFill in the group table:\n"
    "\\[\n"
    "\\begin{array}{|c||c|c|c|} %CLEAR\n"
    "    \\hline\n"
    "    G & 1 & a & b \\\\\n"
    "    \\hline\\hline\n"
    "    1 & 1 & a & b \\\\\n"
    "    \\hline\n"
    "    a & a & \\keep{\\blue{1}} & c \\\\\n"
    "    \\hline\n"
    "    b & b & c & \\keep{\\blue{a}} \\\\\n"
    "    \\hline\n"
    "\\end{array}\n"
    "\\]\n"
    "\\end{problem}\n"
)


def test_clear_array_keeps_marked_cells():
    v = variants_of(KEEP_ARRAY)
    for raw in (v["submission"], v["handout"]):
        out = " ".join(raw.split())
        assert "%CLEAR" not in out
        assert "G & 1 & a & b" in out                    # header row intact
        assert "a & & \\keep{\\blue{1}} & \\\\" in out   # marked cell kept, rest cleared
        assert "b & & & \\keep{\\blue{a}} \\\\" in out
        assert "1 & & & \\\\" in out                     # unmarked row cleared
    # solutions keep the filled table
    assert "1 & 1 & a & b" in v["solutions"]


def test_clear_tabular_keeps_marked_cells():
    v = variants_of(
        CLEAR_TABLE.replace("answer & 2 &", "answer & \\keep{2} &")
    )
    sub = v["submission"]
    assert "\\keep{2}" in sub
    assert "pmatrix" not in sub


def test_split_top_level_nested():
    parts = texscan.split_top_level(r"a & $\begin{pmatrix}1 & 2\end{pmatrix}$ & {x & y}", "&")
    assert len(parts) == 3


# -------------------------------------------------------------------- header

def test_header_banners():
    v = variants_of("\\begin{problem}\nP.\n\\end{problem}\n")
    assert "\\blue{SOLUTIONS}" in v["solutions"]
    assert "\\blue{SUBMISSION}" in v["submission"]
    assert "%HEADER" not in v["handout"]
    assert "SOLUTIONS" not in v["handout"]


def test_metadata_removed_from_submission_only():
    v = variants_of("\\begin{problem}\nP.\n\\end{problem}\n")
    assert "hwgenie" not in v["submission"]
    assert "hwgenie" in v["solutions"]


def test_variant_newpages_stripped_from_submission_only():
    v = variants_of(
        "\\begin{problem}\nA.\n\\end{problem}\n"
        "\\solnewpage\n"
        "\\handoutnewpage\n"
        "\\begin{problem}\nB.\n\\end{problem}\n"
    )
    for macro in ("\\solnewpage", "\\handoutnewpage"):
        assert macro in v["solutions"]
        assert macro in v["solutions_web"]
        # Kept in the handout tex; hwgenie.sty decides which one fires.
        assert macro in v["handout"]
    assert "newpage" not in v["submission"]
    assert "A." in v["submission"] and "B." in v["submission"]


def test_handout_variant_injected_modern_docs():
    doc = (
        "\\documentclass{article}\n\\usepackage{hwgenie}\n"
        "\\hwnumber{1}\n\\begin{document}\n"
        "\\begin{problem}\nA.\n\\end{problem}\n\\end{document}\n"
    )
    v = make_variants(doc)
    assert "\\hwvariant{Handout}" in v["handout"]
    assert "\\hwvariant{Solutions}" in v["solutions"]
    assert "\\hwvariant{Submission}" in v["submission"]


def test_foldeq_becomes_equation_in_submission_only():
    v = variants_of(
        "\\begin{problem}\nShow that\n"
        "\\begin{foldeq*}\n"
        "    a + b &= 2j + 2k + 2 \\fold{=} 2(j + k + 1).\n"
        "\\end{foldeq*}\n"
        "\\begin{foldeq}\n"
        "    x &= y \\fold{\\in} S.\n"
        "\\end{foldeq}\n"
        "\\end{problem}\n"
    )
    sub = v["submission"]
    assert "foldeq" not in sub and "\\fold{" not in sub and "&" not in sub
    assert "\\begin{equation*}\n    a + b = 2j + 2k + 2 = 2(j + k + 1).\n\\end{equation*}" in sub
    assert "\\begin{equation}\n    x = y \\in S.\n\\end{equation}" in sub
    # Other variants keep the fold markers for the HTML converter.
    assert "\\begin{foldeq*}" in v["handout"] and "\\fold{=}" in v["solutions"]


def test_hw_metadata_commands_stripped_from_submission_only():
    doc = (
        "\\documentclass{article}\n\\usepackage{hwgenie}\n"
        "\\hwnumber{1}\n\\hwtitle{The Beginning}\n"
        "\\hwrelease{no}   % flip to yes\n\\hwsolutions{no}\n\\hwtype{problemset}\n"
        "\\begin{document}\n"
        "\\begin{problem}\nA.\n\\end{problem}\n\\end{document}\n"
    )
    v = make_variants(doc)
    sub = v["submission"]
    for macro in ("\\hwrelease", "\\hwsolutions", "\\hwtype"):
        assert macro not in sub
        assert macro in v["solutions"]
    # Numbering/title commands survive — the student preamble defines them.
    assert "\\hwnumber{1}" in sub and "\\hwtitle{The Beginning}" in sub


def test_pdfonly_unwrapped_in_submission():
    v = variants_of(
        "\\begin{problem}\nP.\n\\end{problem}\n"
        "\\begin{pdfonly}\n"
        "Print-only note.\n"
        "\\begin{solution}\nSecret.\n\\end{solution}\n"
        "\\end{pdfonly}\n"
    )
    sub = v["submission"]
    assert "pdfonly" not in sub
    assert "Print-only note." in sub
    # Edits inside the unwrapped body still apply.
    assert "Secret." not in sub and "%Write your solution here" in sub
    assert "\\begin{pdfonly}" in v["handout"]


def test_hwpreview_stripped_from_all_variants():
    doc = (
        "\\documentclass{article}\n\\usepackage{hwgenie}\n"
        "\\hwpreview{Handout}\n"
        "\\hwnumber{1}\n\\begin{document}\n"
        "\\begin{problem}\nA.\n\\end{problem}\n\\end{document}\n"
    )
    v = make_variants(doc)
    for key in ("handout", "solutions", "submission", "solutions_web"):
        assert "hwpreview" not in v[key], key


# --------------------------------------------------------------- integration

@pytest.mark.skipif(not SAMPLE.exists(), reason="sample file not present")
def test_sample_end_to_end(tmp_path):
    result = build(SAMPLE, out_dir=tmp_path, compile_pdfs=False)
    assert result.ok
    sub = result.files["submission"].read_text()

    # metadata + images + solutions gone
    assert "%Problem Set Data" not in sub
    assert "includegraphics" not in sub
    for secret in ("Since 8 divides", "gcd(m,n) == 1", "6/\\pi^2"):
        assert secret not in sub
    # 11 blank solution environments remain
    assert sub.count("%Write your solution here") == 11
    assert sub.count("\\begin{solution}") == 11
    # the sample's table has no %CLEAR marker, so it must stay intact
    assert "\\# squares mod $p$ & 2 & 3" in sub

    handout = result.files["handout_tex"].read_text()
    assert "includegraphics" in handout
    assert "\\begin{solution}" not in handout.replace("\\newenvironment{solution}", "")
    assert "Since 8 divides" not in handout

    solutions = result.files["solutions_tex"].read_text()
    assert "\\blue{SOLUTIONS}" in solutions
    assert "Since 8 divides" in solutions


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample file not present")
def test_sample_with_clear_marker(tmp_path):
    """Adding %CLEAR to the squares-mod-p table empties the answer row."""
    text = SAMPLE.read_text()
    tagged = text.replace(
        "\\begin{tabular}{|c|c|c|c|c|c|c|c|c|c|c|c|}",
        "\\begin{tabular}{|c|c|c|c|c|c|c|c|c|c|c|c|} %CLEAR",
    )
    assert tagged != text
    src = tmp_path / "ps3.tex"
    src.write_text(tagged)
    result = build(src, out_dir=tmp_path / "out", compile_pdfs=False)
    sub = result.files["submission"].read_text()
    assert "\\# squares mod $p$" in sub        # first column kept
    assert "& 16" not in sub                    # answer-row values cleared
    assert "$p$ & 3 & 5 & 7" in sub             # header row kept
    assert "%CLEAR" not in sub
