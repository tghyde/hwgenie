"""Edit-based transforms on the original source text.

Each transform returns a list of edits (start, end, replacement) computed from
the parse tree of the masked text.  Edits are applied to the ORIGINAL text, so
the author's formatting is preserved everywhere we don't explicitly touch.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from . import texscan

Edit = Tuple[int, int, str]

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
