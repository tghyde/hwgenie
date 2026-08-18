"""Low-level LaTeX scanning utilities.

Strategy: verbatim-like environments (lstlisting, verbatim, ...) are masked out
(content replaced by spaces, newlines preserved) BEFORE handing the text to
pylatexenc.  Node positions on the masked text are therefore valid positions on
the original text, and all edits are performed on the original.
"""

from __future__ import annotations

from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from pylatexenc.latexwalker import (
    LatexEnvironmentNode,
    LatexMacroNode,
    LatexWalker,
)

VERBATIM_ENVS = ("verbatim*", "verbatim", "lstlisting", "Verbatim")


def mask_verbatim(text: str) -> str:
    """Replace the contents of verbatim-like environments with spaces.

    Length and line structure are preserved, so char positions are unchanged.
    """
    chars = list(text)
    for env in VERBATIM_ENVS:
        begin_tok = "\\begin{%s}" % env
        end_tok = "\\end{%s}" % env
        i = 0
        while True:
            b = text.find(begin_tok, i)
            if b < 0:
                break
            content_start = b + len(begin_tok)
            e = text.find(end_tok, content_start)
            if e < 0:
                break
            for j in range(content_start, e):
                if chars[j] != "\n":
                    chars[j] = " "
            i = e + len(end_tok)
    return "".join(chars)


def parse_nodes(masked_text: str):
    walker = LatexWalker(masked_text, tolerant_parsing=True)
    nodes, _, _ = walker.get_latex_nodes()
    return nodes


def _children(node) -> Iterator:
    nl = getattr(node, "nodelist", None)
    if nl:
        for c in nl:
            if c is not None:
                yield c
    argd = getattr(node, "nodeargd", None)
    if argd is not None and getattr(argd, "argnlist", None):
        for a in argd.argnlist:
            if a is not None:
                yield a


def iter_envs(nodes: Iterable, names: Optional[Sequence[str]] = None) -> Iterator[LatexEnvironmentNode]:
    """Recursively yield environment nodes (optionally filtered by name)."""
    for n in nodes:
        if isinstance(n, LatexEnvironmentNode) and (
            names is None or n.environmentname in names
        ):
            yield n
        yield from iter_envs(_children(n), names)


def contains_macro(node, macroname: str) -> bool:
    if isinstance(node, LatexMacroNode) and node.macroname == macroname:
        return True
    return any(contains_macro(c, macroname) for c in _children(node))


def line_indent(text: str, pos: int) -> str:
    """Whitespace prefix of the line containing pos, if pos is preceded only by
    whitespace on that line; otherwise ''."""
    line_start = text.rfind("\n", 0, pos) + 1
    prefix = text[line_start:pos]
    return prefix if prefix.strip() == "" else ""


def split_top_level(s: str, sep: str) -> List[str]:
    """Split s on `sep` ('\\\\' or '&') occurring at zero brace/environment
    depth.  Comments (% to end of line) are skipped.  Separators are removed;
    everything else is preserved verbatim."""
    parts: List[str] = []
    depth = 0
    part_start = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt == "\\":
                if sep == "\\\\" and depth == 0:
                    parts.append(s[part_start:i])
                    part_start = i + 2
                i += 2
                continue
            if nxt and not nxt.isalpha():
                i += 2  # escaped symbol: \{ \} \& \% etc.
                continue
            # macro name
            j = i + 1
            while j < n and s[j].isalpha():
                j += 1
            name = s[i + 1 : j]
            if name in ("begin", "end"):
                k = j
                while k < n and s[k] in " \t":
                    k += 1
                if k < n and s[k] == "{":
                    close = s.find("}", k)
                    if close > 0:
                        depth += 1 if name == "begin" else -1
                        i = close + 1
                        continue
            i = j
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "%":
            eol = s.find("\n", i)
            i = n if eol < 0 else eol
            continue
        elif ch == "&" and sep == "&" and depth == 0:
            parts.append(s[part_start:i])
            part_start = i + 1
        i += 1
    parts.append(s[part_start:])
    return parts


TABLE_ENVS = ("tabular", "tabular*", "array")


def table_body_span(env_text: str) -> Optional[Tuple[int, int]]:
    """Given the full text of a table environment (tabular, tabular*, array),
    return the (start, end) span of its body relative to env_text: after the
    column spec, before \\end{...}."""
    for name in TABLE_ENVS:
        opener = "\\begin{%s}" % name
        if env_text.startswith(opener):
            i = len(opener)
            break
    else:
        return None
    n = len(env_text)
    # optional [pos] argument
    while i < n and env_text[i] in " \t\n":
        i += 1
    if i < n and env_text[i] == "[":
        close = env_text.find("]", i)
        if close < 0:
            return None
        i = close + 1
    while i < n and env_text[i] in " \t\n":
        i += 1
    # required {colspec} (tabular* has an extra {width} argument first)
    nargs = 2 if name == "tabular*" else 1
    for _ in range(nargs):
        if i >= n or env_text[i] != "{":
            return None
        depth = 0
        j = i
        while j < n:
            if env_text[j] == "{":
                depth += 1
            elif env_text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
        while i < n and env_text[i] in " \t":
            i += 1
    end = env_text.rfind("\\end{%s}" % name)
    if end < 0 or end < i:
        return None
    return (i, end)
