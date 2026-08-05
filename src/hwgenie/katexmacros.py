"""Extract user macros from a LaTeX preamble for KaTeX's `macros` option.

Handles \\def\\name{...}, \\newcommand{\\name}[n]{...}, and
\\DeclareMathOperator{\\name}{...}.  KaTeX infers arity from #n tokens in the
expansion, so parameterized newcommands work unchanged.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple


def _match_braced(text: str, i: int) -> Optional[Tuple[str, int]]:
    """text[i] must be '{'; return (contents, index_after_close)."""
    if i >= len(text) or text[i] != "{":
        return None
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
        j += 1
    return None


DEF_RE = re.compile(r"\\def\s*\\([A-Za-z]+)\s*(?=\{)")
NEWCMD_RE = re.compile(
    r"\\(?:re)?newcommand\*?\s*(?:\{\\([A-Za-z]+)\}|\\([A-Za-z]+))\s*"
    r"(?:\[(\d+)\]\s*)?(?:\[[^\]]*\]\s*)?(?=\{)"
)
DECLOP_RE = re.compile(r"\\DeclareMathOperator(\*?)\s*\{\\([A-Za-z]+)\}\s*(?=\{)")


# LaTeX built-ins KaTeX lacks; \ensuremath is a no-op inside math, which is
# the only context KaTeX renders.
DEFAULT_MACROS = {"\\ensuremath": "#1"}


def extract_macros(text: str) -> Dict[str, str]:
    """Scan the preamble (text before \\begin{document}) for macro definitions."""
    end = text.find("\\begin{document}")
    preamble = text[: end if end >= 0 else len(text)]
    macros: Dict[str, str] = dict(DEFAULT_MACROS)

    for m in DEF_RE.finditer(preamble):
        body = _match_braced(preamble, m.end())
        if body:
            macros["\\" + m.group(1)] = body[0]

    for m in NEWCMD_RE.finditer(preamble):
        name = m.group(1) or m.group(2)
        body = _match_braced(preamble, m.end())
        if body:
            macros["\\" + name] = body[0]

    for m in DECLOP_RE.finditer(preamble):
        star = m.group(1)
        body = _match_braced(preamble, m.end())
        if body:
            op = "\\operatorname*" if star else "\\operatorname"
            macros["\\" + m.group(2)] = f"{op}{{{body[0]}}}"

    return macros
