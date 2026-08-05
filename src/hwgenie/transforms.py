"""Edit-based transforms on the original source text.

Each transform returns a list of edits (start, end, replacement) computed from
the parse tree of the masked text.  Edits are applied to the ORIGINAL text, so
the author's formatting is preserved everywhere we don't explicitly touch.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import texscan

Edit = Tuple[int, int, str]

INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")


def expand_inputs(
    text: str,
    search_dirs: Sequence[Path],
    warnings: Optional[List[str]] = None,
    depth: int = 0,
) -> str:
    """Inline \\input{...}/\\include{...} files so every generated variant is
    self-contained (students receive a single submission .tex).  Files are
    looked up in `search_dirs`; unresolvable inputs are left in place (the
    PDF compiler may still find them via TEXINPUTS)."""
    if depth > 3 or not search_dirs:
        return text
    masked = texscan.mask_verbatim(text)
    out: List[str] = []
    last = 0
    for m in INPUT_RE.finditer(masked):
        fname = m.group(1).strip()
        if not fname.endswith(".tex"):
            fname += ".tex"
        path = next(
            (d / fname for d in search_dirs if (Path(d) / fname).exists()), None
        )
        if path is None:
            if warnings is not None:
                warnings.append(
                    f"\\input{{{m.group(1)}}} not found next to the source or "
                    "repo root; left unexpanded."
                )
            continue
        content = Path(path).read_text(encoding="utf-8")
        content = expand_inputs(content, search_dirs, warnings, depth + 1)
        out.append(text[last : m.start()])
        out.append(content.strip("\n"))
        last = m.end()
    out.append(text[last:])
    return "".join(out)

SOLUTION_PLACEHOLDER = "%Write your solution here"


def apply_edits(text: str, edits: List[Edit]) -> str:
    """Apply edits; edits fully contained inside an earlier (outer) edit are
    dropped (e.g. a figure inside a solution that is being removed)."""
    kept: List[Edit] = []
    for e in sorted(edits, key=lambda e: (e[0], -e[1])):
        if kept and e[0] < kept[-1][1] and e[1] <= kept[-1][1]:
            continue  # contained in previous edit
        if kept and e[0] < kept[-1][1]:
            raise ValueError(f"Overlapping (non-nested) edits: {kept[-1]} vs {e}")
        kept.append(e)
    out = []
    last = 0
    for start, end, repl in kept:
        out.append(text[last:start])
        out.append(repl)
        last = end
    out.append(text[last:])
    return "".join(out)


def collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n[ \t]*\n[ \t]*(\n[ \t]*)+", "\n\n\n", text)


# ---------------------------------------------------------------- solutions

def solution_edits(text: str, nodes, mode: str) -> List[Edit]:
    """mode='remove' deletes solution environments; mode='blank' replaces each
    with an empty one, preserving indentation."""
    edits: List[Edit] = []
    for env in texscan.iter_envs(nodes, ("solution", "solution*")):
        start, end = env.pos, env.pos + env.len
        if mode == "remove":
            edits.append((start, end, ""))
        elif mode == "blank":
            indent = texscan.line_indent(text, start)
            name = env.environmentname
            repl = (
                f"\\begin{{{name}}}\n"
                f"{indent}\t{SOLUTION_PLACEHOLDER}\n"
                f"{indent}\\end{{{name}}}"
            )
            edits.append((start, end, repl))
        else:
            raise ValueError(mode)
    return edits


# ------------------------------------------------------------------ figures

def figure_edits(text: str, nodes) -> List[Edit]:
    """Remove figure/figure* environments AND center environments that contain
    an \\includegraphics (bare centered images, as used in practice)."""
    edits: List[Edit] = []
    for env in texscan.iter_envs(nodes, ("figure", "figure*")):
        edits.append((env.pos, env.pos + env.len, ""))
    for env in texscan.iter_envs(nodes, ("center",)):
        if texscan.contains_macro(env, "includegraphics"):
            edits.append((env.pos, env.pos + env.len, ""))
    return edits


USEPACKAGE_HWGENIE_RE = re.compile(r"\\usepackage\{hwgenie\}")


def inline_sty(text: str, search_dirs: Sequence[Path]) -> str:
    """Replace \\usepackage{hwgenie} with the .sty contents (and inline
    coursedata.tex) so the student submission compiles standalone."""
    m = USEPACKAGE_HWGENIE_RE.search(text)
    if not m:
        return text
    sty_path = next(
        (Path(d) / "hwgenie.sty" for d in search_dirs
         if (Path(d) / "hwgenie.sty").exists()),
        None,
    )
    if sty_path is None:
        return text
    sty = sty_path.read_text(encoding="utf-8")
    sty = re.sub(r"\\NeedsTeXFormat\{[^{}]*\}[^\n]*\n", "", sty)
    sty = re.sub(r"\\ProvidesPackage\{[^{}]*\}(\[[^\]]*\])?[^\n]*\n", "", sty)
    sty = sty.replace("\\endinput", "")
    cd_path = next(
        (Path(d) / "coursedata.tex" for d in search_dirs
         if (Path(d) / "coursedata.tex").exists()),
        None,
    )
    if cd_path is not None:
        sty = sty.replace(
            "\\InputIfFileExists{coursedata}{}{}",
            cd_path.read_text(encoding="utf-8").strip("\n"),
        )
    return (
        text[: m.start()]
        + "% ---------- hwgenie.sty (inlined by hwgenie) ----------\n"
        + sty.strip("\n")
        + "\n% ---------- end hwgenie.sty ----------"
        + text[m.end():]
    )


def inject_variant(text: str, label: str) -> str:
    """Insert \\hwvariant{label} after \\usepackage{hwgenie} — the modern
    replacement for the %HEADER banner marker."""
    m = USEPACKAGE_HWGENIE_RE.search(text)
    if not m:
        return text
    return text[: m.end()] + f"\n\\hwvariant{{{label}}}" + text[m.end():]


def env_removal_edits(text: str, nodes, names) -> List[Edit]:
    """Remove entire environments by name (e.g. htmlonly from the submission)."""
    return [
        (env.pos, env.pos + env.len, "")
        for env in texscan.iter_envs(nodes, tuple(names))
    ]


# ------------------------------------------------------------------- tables

def clear_table_edits(text: str, nodes) -> List[Edit]:
    """For tabular environments whose body starts with a %CLEAR comment: keep
    the header row and first column, blank every other cell."""
    edits: List[Edit] = []
    for env in texscan.iter_envs(nodes, ("tabular", "tabular*")):
        env_text = text[env.pos : env.pos + env.len]
        span = texscan.tabular_body_span(env_text)
        if span is None:
            continue
        body = env_text[span[0] : span[1]]
        first_line = body.lstrip().splitlines()[0] if body.strip() else ""
        if "%CLEAR" not in first_line:
            continue
        rows = texscan.split_top_level(body, "\\\\")
        header = rows[0].replace("%CLEAR", "", 1)
        new_rows = [header]
        for row in rows[1:]:
            cells = texscan.split_top_level(row, "&")
            new_rows.append(" & ".join([cells[0]] + [" "] * (len(cells) - 1)))
        new_body = "\\\\".join(new_rows)
        abs_start = env.pos + span[0]
        abs_end = env.pos + span[1]
        edits.append((abs_start, abs_end, new_body))
    return edits


# ------------------------------------------------------------------- header

def banner(text_label: str) -> str:
    return "\\begin{center}\n\t\\blue{%s}\n\\end{center}" % text_label


def header_edits(masked_text: str, replacement: str, remove: bool = False) -> List[Edit]:
    """Replace (or remove) every line containing the %HEADER marker.  Scans the
    masked text so markers inside code listings are ignored."""
    edits: List[Edit] = []
    for m in re.finditer(r"^[^\n]*%HEADER[^\n]*\n?", masked_text, re.MULTILINE):
        if remove:
            edits.append((m.start(), m.end(), ""))
        else:
            edits.append((m.start(), m.end(), replacement + "\n"))
    return edits
