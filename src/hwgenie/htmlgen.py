"""LaTeX → HTML conversion for hwgenie's document subset.

Structural elements (problems, solutions, lists, code listings, tables,
figures) are converted to semantic HTML.  Math is passed through verbatim
(HTML-escaped, delimiters intact) and rendered client-side by KaTeX.
"""

from __future__ import annotations

import html as html_mod
import re
import textwrap
import unicodedata
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexSpecialsNode,
)

from . import texscan


def esc(s: str) -> str:
    return html_mod.escape(s, quote=False)


# Macros producing plain characters.
CHAR_MACROS = {
    "#": "#", "%": "%", "&": "&amp;", "_": "_", "$": "$",
    "{": "{", "}": "}", " ": " ", ",": "&thinsp;",
    "ldots": "…", "dots": "…", "textbackslash": "\\",
}

# Inline wrappers: macroname -> (open, close).
WRAP_MACROS = {
    "textbf": ("<strong>", "</strong>"),
    "textit": ("<em>", "</em>"),
    "emph": ("<em>", "</em>"),
    "texttt": ("<code>", "</code>"),
    "textsc": ('<span class="smallcaps">', "</span>"),
    "underline": ("<u>", "</u>"),
    "blue": ('<span class="task">', "</span>"),
    "red": ('<span class="alert">', "</span>"),
    "hl": ("<mark>", "</mark>"),
    "ul": ("<u>", "</u>"),
    "st": ("<s>", "</s>"),
}

# Macros to silently drop, with the number of {}-arguments to consume when
# pylatexenc has not already attached them.
SKIP_MACROS = {
    "vspace": 1, "hspace": 1, "vspace*": 1,
    "bigskip": 0, "medskip": 0, "smallskip": 0,
    "noindent": 0, "indent": 0, "centering": 0,
    "newpage": 0, "clearpage": 0, "hrule": 0,
    "pagestyle": 1, "thispagestyle": 1,
    "setcounter": 2, "addtocounter": 2, "numberwithin": 2,
    "setlength": 2, "addtolength": 2,
    "qed": 0, "pushQED": 1, "popQED": 0,
    "theoremstyle": 1,
    "hwnumber": 1, "hwtitle": 1, "hwsolutions": 1, "hwrelease": 1, "hwtype": 1,
    "hwmaketitle": 0, "hwcourse": 0, "hwsemester": 0, "hwvariant": 1,
    "!": 0, ";": 0, ":": 0,
}

SPECIALS_MAP = {
    "--": "–", "---": "—", "~": "&nbsp;",
    "``": "“", "''": "”", "`": "‘", "'": "’",
}

# Accent macros: combining character applied to the next letter.
ACCENT_MACROS = {
    "'": "\u0301", "\"": "\u0308", "`": "\u0300", "^": "\u0302",
    "~": "\u0303", "=": "\u0304", ".": "\u0307",
    "u": "\u0306", "v": "\u030C", "c": "\u0327", "H": "\u030B",
}

# \newtheorem{name}[shared]{Label}  |  \newtheorem{name}{Label}[parent]
# \newtheorem*{name}{Label}
NEWTHEOREM_RE = re.compile(
    r"\\newtheorem(\*?)\s*\{([^{}]+)\}\s*"
    r"(?:\[([^\[\]]+)\]\s*)?\{([^{}]+)\}\s*(?:\[([^\[\]]+)\])?"
)
NUMBERWITHIN_RE = re.compile(r"\\numberwithin\s*\{equation\}\s*\{([^{}]+)\}")

# Display-math environments: KaTeX supports the *inner* (aligned/gathered)
# forms inside \[...\]; numbering is not preserved.
MATH_ENVS = {
    "align": "aligned", "align*": "aligned",
    "gather": "gathered", "gather*": "gathered",
    "equation": None, "equation*": None,
    "eqnarray": "aligned", "eqnarray*": "aligned",
    "multline": "gathered", "multline*": "gathered",
}

CODE_ENVS = ("lstlisting", "verbatim", "verbatim*", "Verbatim")


class Flow:
    """Accumulates inline content into paragraphs between block elements."""

    def __init__(self):
        self.blocks: List[str] = []
        self._buf: List[str] = []

    def inline(self, s: str) -> None:
        if s:
            self._buf.append(s)

    def parbreak(self) -> None:
        txt = "".join(self._buf).strip()
        self._buf = []
        if txt:
            self.blocks.append(f"<p>{txt}</p>")

    def block(self, s: str) -> None:
        self.parbreak()
        if s:
            self.blocks.append(s)

    def result(self) -> str:
        self.parbreak()
        return "\n".join(self.blocks)


class HtmlConverter:
    def __init__(self, text: str, include_solutions: bool = True,
                 section: Optional[str] = None, extra_preamble: str = ""):
        self.text = text
        self.extra_preamble = extra_preamble  # e.g. a shared hwgenie.sty
        self.include_solutions = include_solutions
        self.section = section
        self.warnings: List[str] = []
        self.images: List[str] = []
        self.title_lines: List[str] = []
        self.footnotes: List[str] = []
        self.labels: Dict[str, Tuple[str, str]] = {}  # key -> (kind, display)
        self.problem_counter = 0
        self.counters: Dict[str, int] = defaultdict(int)
        self.eq_counter = 0
        self._label_ctx: List[Tuple[str, str]] = []
        self._collecting = False
        self._saw_qedhere = False
        self._in_solution = False
        self.problem_anchors: List[Tuple[str, str]] = []  # (number, anchor id)
        self.theorems = self._parse_newtheorems()
        self.eq_prefix = self._parse_eq_prefix()

    def _parse_newtheorems(self) -> Dict[str, Dict[str, Optional[str]]]:
        thms: Dict[str, Dict[str, Optional[str]]] = {}
        for m in NEWTHEOREM_RE.finditer(self.extra_preamble + "\n" + self.text):
            star, name, shared, label, parent = m.groups()
            if name in ("problem", "solution"):
                continue  # handled specially
            if star:
                thms[name] = {"label": label, "counter": None, "parent": None}
            elif shared:
                owner = thms.get(shared, {})
                counter = owner.get("counter", shared) or shared
                thms[name] = {
                    "label": label,
                    "counter": counter,
                    "parent": owner.get("parent"),
                }
            else:
                thms[name] = {"label": label, "counter": name, "parent": parent}
        return thms

    def _parse_eq_prefix(self) -> str:
        m = NUMBERWITHIN_RE.search(self.extra_preamble + "\n" + self.text)
        sec = self.section or "1"
        if not m:
            return ""
        if m.group(1) == "section":
            return f"{sec}."
        if m.group(1) == "subsection":
            return f"{sec}.0."   # problem sets never increment subsections
        return ""

    # ------------------------------------------------------------- top level

    def _reset_pass_state(self) -> None:
        self.warnings = []
        self.images = []
        self.title_lines = []
        self.footnotes = []
        self.problem_counter = 0
        self.counters = defaultdict(int)
        self.eq_counter = 0
        self._label_ctx = []
        self.problem_anchors = []

    def convert(self) -> str:
        masked = texscan.mask_verbatim(self.text)
        nodes = texscan.parse_nodes(masked)
        doc = next(iter(texscan.iter_envs(nodes, ("document",))), None)
        body = doc.nodelist if doc is not None else nodes

        # Pass 1: collect \label targets (numbers depend on document order).
        self._collecting = True
        self.walk(body, Flow())
        # Pass 2: real conversion with all labels resolvable.
        self._collecting = False
        self._reset_pass_state()
        flow = Flow()
        self.walk(body, flow)
        html = flow.result()

        if self.footnotes:
            items = "\n".join(
                f'<li id="fn-{k + 1}">{fn} '
                f'<a href="#fnref-{k + 1}" class="fn-back" '
                f'aria-label="Back to text">↩</a></li>'
                for k, fn in enumerate(self.footnotes)
            )
            html += (
                '\n<section class="footnotes"><hr class="sep">'
                f"<ol>\n{items}\n</ol></section>"
            )
        return html

    def convert_fragment(self, s: str) -> str:
        """Convert a standalone LaTeX fragment; returns inline-ish HTML."""
        sub = HtmlConverter(s, self.include_solutions, self.section)
        sub.labels = self.labels
        nodes = texscan.parse_nodes(texscan.mask_verbatim(s))
        flow = Flow()
        sub.walk(nodes, flow)
        self.warnings.extend(sub.warnings)
        self.images.extend(sub.images)
        return _unwrap_single_p(flow.result())

    # ---------------------------------------------------------------- walker

    def walk(self, nodes, flow: Flow) -> None:
        nodes = [n for n in nodes if n is not None]
        i = 0
        while i < len(nodes):
            i = self.dispatch(nodes, i, flow)

    def dispatch(self, nodes, i: int, flow: Flow) -> int:
        n = nodes[i]
        if isinstance(n, LatexCommentNode):
            return i + 1
        if isinstance(n, LatexCharsNode):
            parts = re.split(r"\n[ \t]*\n(?:[ \t]*\n)*", n.chars)
            for k, part in enumerate(parts):
                if k:
                    flow.parbreak()
                flow.inline(esc(part))
            return i + 1
        if isinstance(n, LatexSpecialsNode):
            flow.inline(SPECIALS_MAP.get(n.specials_chars, esc(n.specials_chars)))
            return i + 1
        if isinstance(n, LatexMathNode):
            raw = self.text[n.pos : n.pos + n.len]
            raw, mathnotes = _extract_math_footnotes(raw)
            for note in mathnotes:
                self.footnotes.append(self.convert_fragment(note))
            if "\\qedhere" in raw:
                self._saw_qedhere = True
                raw = raw.replace("\\qedhere", "")
                if self._in_solution:
                    # HTML solutions carry no tombstone; drop the qed mark.
                    flow.inline(esc(raw))
                    return i + 1
                if raw.lstrip().startswith("\\["):
                    inner = raw[raw.find("\\[") + 2 : raw.rfind("\\]")]
                    raw = f"\\[{inner.rstrip()} \\tag*{{$\\square$}}\\]"
                    flow.inline(esc(raw))
                    return i + 1
                flow.inline(esc(raw) + '<span class="qedbox"></span>')
                return i + 1
            math_html = esc(raw)
            # Keep trailing punctuation glued to inline math so it can't wrap
            # onto its own line (KaTeX spans are inline-block).
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            if (
                n.displaytype == "inline"
                and isinstance(nxt, LatexCharsNode)
                and nxt.chars
            ):
                m = re.match(r"[.,;:!?)\]'’\"]+", nxt.chars)
                if m:
                    flow.inline(
                        f'<span class="nw">{math_html}{esc(m.group(0))}</span>'
                    )
                    rest = nxt.chars[m.end():]
                    parts = re.split(r"\n[ \t]*\n(?:[ \t]*\n)*", rest)
                    for k, part in enumerate(parts):
                        if k:
                            flow.parbreak()
                        flow.inline(esc(part))
                    return i + 2
            flow.inline(math_html)
            for k in range(len(self.footnotes) - len(mathnotes) + 1,
                           len(self.footnotes) + 1):
                flow.inline(
                    f'<sup class="fn"><a href="#fn-{k}" id="fnref-{k}">{k}</a></sup>'
                )
            return i + 1
        if isinstance(n, LatexGroupNode):
            self.walk(n.nodelist, flow)
            return i + 1
        if isinstance(n, LatexMacroNode):
            return self.macro(nodes, i, flow)
        if isinstance(n, LatexEnvironmentNode):
            return self.env(nodes, i, flow)
        return i + 1

    # ---------------------------------------------------------------- macros

    def _parsed_group_args(self, n: LatexMacroNode) -> List:
        if n.nodeargd and n.nodeargd.argnlist:
            return [a for a in n.nodeargd.argnlist if isinstance(a, LatexGroupNode)]
        return []

    def _take_groups(self, nodes, j: int, count: int) -> Tuple[List, int]:
        """Consume up to `count` group nodes starting at index j (skipping
        whitespace-only chars nodes)."""
        taken: List = []
        while len(taken) < count and j < len(nodes):
            nx = nodes[j]
            if isinstance(nx, LatexCharsNode) and nx.chars.strip() == "":
                j += 1
                continue
            if isinstance(nx, LatexGroupNode):
                taken.append(nx)
                j += 1
                continue
            break
        return taken, j

    def _macro_args(self, nodes, i: int, count: int) -> Tuple[List, int]:
        """Argument groups of macro nodes[i]: parsed ones if available,
        otherwise consumed from the following nodes."""
        n = nodes[i]
        parsed = self._parsed_group_args(n)
        if parsed:
            return parsed[:count], i + 1
        return self._take_groups(nodes, i + 1, count)

    def macro(self, nodes, i: int, flow: Flow) -> int:
        n = nodes[i]
        name = n.macroname

        if name == "\\":
            flow.inline("<br>")
            return i + 1
        if name in CHAR_MACROS:
            flow.inline(CHAR_MACROS[name])
            return i + 1
        if name in SKIP_MACROS:
            _, j = self._macro_args(nodes, i, SKIP_MACROS[name])
            return j
        if name in WRAP_MACROS:
            open_, close = WRAP_MACROS[name]
            args, j = self._macro_args(nodes, i, 1)
            inner = self.convert_inline(args[0].nodelist) if args else ""
            flow.inline(open_ + inner + close)
            return j
        if name == "textcolor":
            args, j = self._macro_args(nodes, i, 2)
            if len(args) == 2:
                color = _group_text(args[0]).strip()
                cls = {"blue": "task", "red": "alert"}.get(color)
                inner = self.convert_inline(args[1].nodelist)
                if cls:
                    flow.inline(f'<span class="{cls}">{inner}</span>')
                else:
                    flow.inline(f'<span style="color:{esc(color)}">{inner}</span>')
            return j
        if name == "includegraphics":
            args, j = self._macro_args(nodes, i, 2)
            fname = None
            for a in reversed(args):
                t = _group_text(a).strip()
                if t and "=" not in t:
                    fname = t
                    break
            if fname is None and args:
                fname = _group_text(args[-1]).strip()
            if fname:
                self.images.append(fname)
                base = fname.rsplit("/", 1)[-1]
                flow.block(
                    f'<figure class="fig"><img src="{html_mod.escape(base)}" '
                    f'alt="{html_mod.escape(_alt_from_filename(fname))}"></figure>'
                )
            return j
        if name == "head":
            args, j = self._macro_args(nodes, i, 1)
            if args:
                self.title_lines = self._split_on_newline_macro(args[0].nodelist)
            return j
        if name == "separate":
            # Problem cards make the old horizontal-rule spacers redundant.
            return i + 1
        if name == "href":
            args, j = self._macro_args(nodes, i, 2)
            if len(args) == 2:
                url = _group_text(args[0]).strip()
                flow.inline(
                    f'<a href="{html_mod.escape(url)}">'
                    f"{self.convert_inline(args[1].nodelist)}</a>"
                )
            return j
        if name == "url":
            args, j = self._macro_args(nodes, i, 1)
            if args:
                url = _group_text(args[0]).strip()
                flow.inline(f'<a href="{html_mod.escape(url)}">{esc(url)}</a>')
            return j
        if name == "item":
            self.warnings.append("\\item found outside a list; ignored.")
            return i + 1
        if name in ACCENT_MACROS:
            return self._accent(nodes, i, flow)
        if name == "label":
            args, j = self._macro_args(nodes, i, 1)
            if args:
                key = _group_text(args[0]).strip()
                if self._label_ctx:
                    self.labels[key] = self._label_ctx[-1]
                elif key not in self.labels:
                    self.warnings.append(
                        f"\\label{{{key}}} outside a numbered environment; "
                        "references to it will not resolve."
                    )
            return j
        if name in ("ref", "eqref"):
            args, j = self._macro_args(nodes, i, 1)
            key = _group_text(args[0]).strip() if args else ""
            target = self.labels.get(key)
            if target is None:
                if not self._collecting:
                    self.warnings.append(f"Unresolved \\{name}{{{key}}} rendered as '??'.")
                flow.inline("(??)" if name == "eqref" else "??")
                return j
            kind, display = target
            anchor = f"{kind}-{_anchor_slug(display)}"
            link = f'<a class="xref" href="#{anchor}">{esc(display)}</a>'
            flow.inline(f"({link})" if name == "eqref" else link)
            return j
        if name == "footnote":
            args, j = self._macro_args(nodes, i, 1)
            if args:
                content = self.convert_inline(args[0].nodelist)
                self.footnotes.append(content)
                k = len(self.footnotes)
                flow.inline(
                    f'<sup class="fn"><a href="#fn-{k}" id="fnref-{k}">{k}</a></sup>'
                )
            return j
        if name in ("quad", "qquad"):
            flow.inline("&emsp;")
            return i + 1
        if name == "qedhere":
            self._saw_qedhere = True
            if not self._in_solution:
                flow.inline('<span class="qedbox"></span>')
            return i + 1
        if name == "epigraph":
            args, j = self._macro_args(nodes, i, 2)
            if len(args) == 2:
                quote = self.convert_inline(args[0].nodelist)
                attribution = self.convert_inline(args[1].nodelist)
                flow.block(
                    '<blockquote class="epigraph">'
                    f"<p>{quote}</p>"
                    f"<footer>{attribution}</footer></blockquote>"
                )
            return j
        if name in self.theorems:
            self.warnings.append(f"\\{name} macro shadowing theorem name; dropped.")
            return i + 1

        self.warnings.append(f"Unknown macro \\{name} dropped (content kept).")
        for a in self._parsed_group_args(n):
            self.walk(a.nodelist, flow)
        return i + 1

    def _accent(self, nodes, i: int, flow: Flow) -> int:
        """Apply an accent macro (\\'e, \\\"o, ...) to the following letter."""
        n = nodes[i]
        combining = ACCENT_MACROS[n.macroname]
        # pylatexenc parses the argument itself: a group ({E}) or a bare
        # chars node (the single letter after \').
        if n.nodeargd and n.nodeargd.argnlist:
            for a in n.nodeargd.argnlist:
                if isinstance(a, LatexGroupNode):
                    inner = self.convert_inline(a.nodelist)
                    flow.inline(_apply_accent(inner, combining))
                    return i + 1
                if isinstance(a, LatexCharsNode) and a.chars:
                    flow.inline(
                        _apply_accent(esc(a.chars[0]), combining)
                        + esc(a.chars[1:])
                    )
                    return i + 1
        j = i + 1
        if j < len(nodes) and isinstance(nodes[j], LatexGroupNode):
            inner = self.convert_inline(nodes[j].nodelist)
            flow.inline(_apply_accent(inner, combining))
            return j + 1
        if j < len(nodes) and isinstance(nodes[j], LatexCharsNode) and nodes[j].chars:
            chars = nodes[j].chars
            flow.inline(_apply_accent(esc(chars[0]), combining))
            rest = chars[1:]
            if rest:
                parts = re.split(r"\n[ \t]*\n(?:[ \t]*\n)*", rest)
                for k, part in enumerate(parts):
                    if k:
                        flow.parbreak()
                    flow.inline(esc(part))
            return j + 1
        return i + 1

    def convert_inline(self, nodes) -> str:
        flow = Flow()
        self.walk(nodes, flow)
        return _unwrap_single_p(flow.result())

    def _split_on_newline_macro(self, nodes) -> List[str]:
        segments: List[List] = [[]]
        for n in nodes:
            if isinstance(n, LatexMacroNode) and n.macroname == "\\":
                segments.append([])
            else:
                segments[-1].append(n)
        return [s for s in (self.convert_inline(seg).strip() for seg in segments) if s]

    # ---------------------------------------------------------- environments

    def env(self, nodes, i: int, flow: Flow) -> int:
        n = nodes[i]
        name = n.environmentname

        if name == "document":
            self.walk(n.nodelist, flow)
        elif name == "problem":
            self.problem_counter += 1
            num = (
                f"{self.section}.{self.problem_counter}"
                if self.section
                else str(self.problem_counter)
            )
            self._label_ctx.append(("problem", num))
            self.problem_anchors.append((num, f"problem-{_anchor_slug(num)}"))
            inner = Flow()
            self.walk(n.nodelist, inner)
            self._label_ctx.pop()
            flow.block(
                f'<details class="problem" open id="problem-{_anchor_slug(num)}">\n'
                f'<summary><h2 class="problem-title">Problem {esc(num)}</h2>'
                "</summary>\n"
                f"{inner.result()}\n</details>"
            )
        elif name in ("solution", "solution*"):
            if self.include_solutions:
                outer = self._in_solution
                self._in_solution = True
                inner = Flow()
                self.walk(n.nodelist, inner)
                self._in_solution = outer
                flow.block(
                    '<details class="solution" open><summary>Solution</summary>\n'
                    f'<div class="solution-body">\n{inner.result()}\n</div></details>'
                )
        elif name in self.theorems:
            flow.block(self._theorem_html(n))
        elif name == "proof":
            outer_qed = self._saw_qedhere
            self._saw_qedhere = False
            inner = Flow()
            self.walk(n.nodelist, inner)
            cls = "proof has-qedhere" if self._saw_qedhere else "proof"
            self._saw_qedhere = outer_qed
            body = _merge_head(
                inner.result(), '<span class="proof-label">Proof.</span>'
            )
            flow.block(f'<div class="{cls}">\n{body}\n</div>')
        elif name in ("enumerate", "itemize"):
            flow.block(self._list_html(n, ordered=(name == "enumerate")))
        elif name == "center":
            inner = Flow()
            self.walk(n.nodelist, inner)
            content = inner.result()
            if content:
                flow.block(f'<div class="center">\n{content}\n</div>')
        elif name in ("figure", "figure*"):
            flow.block(self._figure_html(n))
        elif name in ("tabular", "tabular*"):
            flow.block(self._tabular_html(n))
        elif name in CODE_ENVS:
            flow.block(self._code_html(n))
        elif name in MATH_ENVS:
            flow.block(self._math_env_html(n))
        elif name in ("tikzpicture", "tikzcd"):
            # TikZ can't be rendered client-side; point readers at the PDF.
            flow.block(
                '<div class="thmblock" style="text-align:center">'
                "<em>(diagram — see the PDF version)</em></div>"
            )
            if not self._collecting:
                self.warnings.append(
                    f"{{{name}}} rendered as a see-the-PDF placeholder."
                )
        elif name == "htmlonly":
            inner = Flow()
            self.walk(n.nodelist, inner)
            flow.block(inner.result())
        elif name == "pdfonly":
            pass  # PDF-only content: skipped in HTML
        elif name in ("multicols", "minipage", "quote", "quotation"):
            inner = Flow()
            self.walk(n.nodelist, inner)
            flow.block(inner.result())
        else:
            self.warnings.append(
                f"Unknown environment {{{name}}}: contents converted, wrapper dropped."
            )
            inner = Flow()
            self.walk(n.nodelist, inner)
            flow.block(inner.result())
        return i + 1

    # ------------------------------------------------------------ list logic

    def _list_html(self, n, ordered: bool) -> str:
        items: List[Tuple[Optional[str], List]] = []
        for child in n.nodelist or []:
            if isinstance(child, LatexMacroNode) and child.macroname == "item":
                label = None
                if child.nodeargd and child.nodeargd.argnlist:
                    a0 = child.nodeargd.argnlist[0]
                    if isinstance(a0, LatexGroupNode):
                        label = self.convert_inline(a0.nodelist).strip()
                items.append((label, []))
            elif items:
                items[-1][1].append(child)
            # content before the first \item (whitespace, comments) is dropped

        def li_body(item_nodes) -> str:
            inner = Flow()
            self.walk(item_nodes, inner)
            return _unwrap_single_p(inner.result())

        if not ordered:
            lis = "\n".join(f"<li>{li_body(nl)}</li>" for _label, nl in items)
            return f"<ul>\n{lis}\n</ul>"

        parsed = [_parse_item_label(label) for label, _ in items]
        if all(p is not None for p in parsed) and items:
            list_type = parsed[0][0]
            lis = []
            for (typ, value), (_label, nl) in zip(parsed, items):
                val = f' value="{value}"' if value is not None else ""
                lis.append(f"<li{val}>{li_body(nl)}</li>")
            type_attr = f' type="{list_type}"' if list_type != "1" else ""
            return f"<ol{type_attr}>\n" + "\n".join(lis) + "\n</ol>"

        # Mixed/unparseable labels: definition-style list.
        lis = []
        for (label, nl) in items:
            marker = f'<span class="li-label">{label}</span> ' if label else ""
            lis.append(f"<li>{marker}{li_body(nl)}</li>")
        return '<ul class="no-marker">\n' + "\n".join(lis) + "\n</ul>"

    # ------------------------------------------------------------- fig/table

    def _figure_html(self, n) -> str:
        imgs: List[str] = []
        caption = ""
        i = 0
        nl = [c for c in (n.nodelist or []) if c is not None]
        while i < len(nl):
            c = nl[i]
            if isinstance(c, LatexMacroNode) and c.macroname == "includegraphics":
                args, i = self._macro_args(nl, i, 2)
                for a in reversed(args):
                    t = _group_text(a).strip()
                    if t and "=" not in t:
                        imgs.append(t)
                        break
                continue
            if isinstance(c, LatexMacroNode) and c.macroname == "caption":
                args, i = self._macro_args(nl, i, 1)
                if args:
                    caption = self.convert_inline(args[0].nodelist)
                continue
            i += 1
        self.images.extend(imgs)
        if not imgs and "tikzpicture" in self.text[n.pos : n.pos + n.len]:
            if not self._collecting:
                self.warnings.append(
                    "figure with tikzpicture rendered as a see-the-PDF "
                    "placeholder."
                )
            return (
                '<div class="thmblock" style="text-align:center">'
                "<em>(diagram — see the PDF version)</em></div>"
            )
        parts = [
            f'<img src="{html_mod.escape(f.rsplit("/", 1)[-1])}" '
            f'alt="{html_mod.escape(_alt_from_filename(f))}">'
            for f in imgs
        ]
        cap = f"<figcaption>{caption}</figcaption>" if caption else ""
        return f'<figure class="fig">{"".join(parts)}{cap}</figure>'

    def _tabular_html(self, n) -> str:
        env_text = self.text[n.pos : n.pos + n.len]
        span = texscan.tabular_body_span(env_text)
        if span is None:
            return f'<pre class="code"><code>{esc(env_text)}</code></pre>'
        m = re.match(
            r"\\begin\{tabular\*?\}(?:\[[^\]]*\])?\s*(?:\{[^{}]*\}\s*)?\{([^{}]*)\}",
            env_text,
        ) or re.match(r"\\begin\{tabular\*?\}(?:\[[^\]]*\])?\s*\{([^{}]*)\}", env_text)
        aligns = [c for c in (m.group(1) if m else "")]
        aligns = [{"l": "left", "c": "center", "r": "right"}.get(c) for c in aligns if c in "lcr"]

        body = env_text[span[0] : span[1]]
        rows_raw = texscan.split_top_level(body, "\\\\")
        rows: List[List[str]] = []
        for row in rows_raw:
            cleaned = re.sub(r"\\hline|\\cline\{[^{}]*\}", "", row)
            if not cleaned.strip():
                continue
            cells = texscan.split_top_level(cleaned, "&")
            rows.append([self.convert_fragment(c.strip()) for c in cells])
        if not rows:
            return ""

        def tr(cells: List[str], tag: str) -> str:
            tds = []
            for k, c in enumerate(cells):
                al = aligns[k] if k < len(aligns) and aligns[k] else "center"
                tds.append(f'<{tag} class="al-{al}">{c}</{tag}>')
            return "<tr>" + "".join(tds) + "</tr>"

        head = tr(rows[0], "th")
        rest = "\n".join(tr(r, "td") for r in rows[1:])
        return (
            '<div class="table-wrap"><table>\n'
            f"<thead>{head}</thead>\n<tbody>\n{rest}\n</tbody>\n"
            "</table></div>"
        )

    # ------------------------------------------------------------- code/math

    def _code_html(self, n) -> str:
        name = n.environmentname
        env_text = self.text[n.pos : n.pos + n.len]
        m = re.match(r"\\begin\{" + re.escape(name) + r"\}(\[[^\]]*\])?", env_text)
        end = env_text.rfind("\\end{")
        content = env_text[m.end() : end]
        content = textwrap.dedent(content.lstrip("\n").rstrip())
        lang = ""
        lm = re.search(r"language\s*=\s*([A-Za-z0-9+]+)", m.group(1) or "")
        if lm:
            lang = f' class="language-{lm.group(1).lower()}"'
        return f'<pre class="code"><code{lang}>{esc(content)}</code></pre>'

    def _theorem_html(self, n) -> str:
        spec = self.theorems[n.environmentname]
        display = None
        if spec["counter"]:
            self.counters[spec["counter"]] += 1
            count = self.counters[spec["counter"]]
            display = (
                f"{self.section}.{count}"
                if spec["parent"] == "section" and self.section
                else str(count)
            )
        head = esc(spec["label"]) + (f" {esc(display)}" if display else "")
        title = ""
        # pylatexenc may have parsed [Title] as an environment argument.
        if n.nodeargd and n.nodeargd.argnlist:
            for a in n.nodeargd.argnlist:
                if a is not None and getattr(a, "nodelist", None):
                    title = self.convert_inline(a.nodelist).strip()
                    break
        if title:
            content_nodes = [c for c in (n.nodelist or []) if c is not None]
        else:
            content_nodes, title = self._extract_bracket_title(n.nodelist)
        if title:
            head += f" ({title})"
        anchor = f' id="thm-{_anchor_slug(display)}"' if display else ""
        self._label_ctx.append(("thm", display or spec["label"]))
        inner = Flow()
        self.walk(content_nodes, inner)
        self._label_ctx.pop()
        body = _merge_head(
            inner.result(), f'<span class="thm-head">{head}.</span>'
        )
        return f'<div class="thmblock"{anchor}>\n{body}\n</div>'

    def _extract_bracket_title(self, nodelist):
        """Pull a leading [Optional Title] out of an environment's content."""
        nodes = [c for c in (nodelist or []) if c is not None]
        k = 0
        while (
            k < len(nodes)
            and isinstance(nodes[k], LatexCharsNode)
            and not nodes[k].chars.strip()
        ):
            k += 1
        if k < len(nodes) and isinstance(nodes[k], LatexCharsNode):
            chars = nodes[k].chars.lstrip()
            if chars.startswith("["):
                close = chars.find("]")
                if close >= 0:
                    title = esc(chars[1:close].strip())
                    rest = LatexCharsNode(
                        chars=chars[close + 1 :],
                        pos=nodes[k].pos, len=nodes[k].len,
                    )
                    return nodes[:k] + [rest] + nodes[k + 1 :], title
        return nodes, ""

    def _math_env_html(self, n) -> str:
        name = n.environmentname
        inner_env = MATH_ENVS[name]
        env_text = self.text[n.pos : n.pos + n.len]
        body = env_text[
            len(f"\\begin{{{name}}}") : env_text.rfind(f"\\end{{{name}}}")
        ]
        body, mathnotes = _extract_math_footnotes(body)
        for note in mathnotes:
            self.footnotes.append(self.convert_fragment(note))
        label_keys = re.findall(r"\\label\s*\{([^{}]*)\}", body)
        body = re.sub(r"\\label\s*\{[^{}]*\}", "", body)
        has_qedhere = "\\qedhere" in body
        if has_qedhere:
            self._saw_qedhere = True
            if self._in_solution:
                body = body.replace("\\qedhere", "")
                has_qedhere = False  # no qed marks inside HTML solutions
            elif inner_env:
                # Keep the square on the line where \qedhere sits (amsthm-like).
                body = body.replace("\\qedhere", "\\qquad\\square")
            else:
                body = body.replace("\\qedhere", "")
        anchor = ""
        qed_tag = ""
        if name == "equation":
            self.eq_counter += 1
            display = f"{self.eq_prefix}{self.eq_counter}"
            for key in label_keys:
                self.labels[key.strip()] = ("eq", display)
            if has_qedhere:
                body = body.rstrip() + " \\;\\square"
            body = body.rstrip() + f" \\tag{{{display}}}"
            anchor = f' id="eq-{_anchor_slug(display)}"'
        else:
            if label_keys and not self._collecting:
                self.warnings.append(
                    f"\\label in {{{name}}}: equation numbers in this "
                    "environment are not preserved in HTML; references will "
                    "not resolve."
                )
            if has_qedhere and not inner_env:
                qed_tag = " \\tag*{$\\square$}"
        if inner_env:
            tex = f"\\[\\begin{{{inner_env}}}{body}\\end{{{inner_env}}}{qed_tag}\\]"
        else:
            tex = f"\\[{body}{qed_tag}\\]"
        marks = "".join(
            f'<sup class="fn"><a href="#fn-{k}" id="fnref-{k}">{k}</a></sup>'
            for k in range(len(self.footnotes) - len(mathnotes) + 1,
                           len(self.footnotes) + 1)
        )
        return f'<div class="math-display"{anchor}>{esc(tex)}</div>{marks}'


FOOTNOTE_RE = re.compile(r"\\footnote\{((?:[^{}]|\{[^{}]*\})*)\}")


def _extract_math_footnotes(raw: str):
    """KaTeX cannot render \footnote inside math; pull the notes out."""
    notes = [m.group(1) for m in FOOTNOTE_RE.finditer(raw)]
    return FOOTNOTE_RE.sub("", raw), notes


# ------------------------------------------------------------------- helpers

def _group_text(group) -> str:
    """Raw-ish text of a group node (chars and simple macros only)."""
    out = []
    for c in group.nodelist or []:
        if isinstance(c, LatexCharsNode):
            out.append(c.chars)
        elif isinstance(c, LatexMacroNode) and c.macroname in CHAR_MACROS:
            out.append({"&amp;": "&"}.get(CHAR_MACROS[c.macroname], CHAR_MACROS[c.macroname]))
    return "".join(out)


def _unwrap_single_p(html: str) -> str:
    m = re.fullmatch(r"<p>(.*)</p>", html, re.DOTALL)
    return m.group(1) if m and "<p>" not in m.group(1) else html


def _anchor_slug(s: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", s or "").strip("-")


def _apply_accent(inner: str, combining: str) -> str:
    """Apply a combining accent to the first character of (possibly escaped)
    HTML text, normalizing to a precomposed character."""
    if not inner:
        return inner
    m = re.match(r"&[a-zA-Z]+;|&#\d+;|.", inner, re.DOTALL)
    first = m.group(0)
    rest = inner[m.end():]
    if len(first) == 1:
        first = unicodedata.normalize("NFC", first + combining)
        return first + rest
    return inner


def _merge_head(body_html: str, head_html: str) -> str:
    """Inject a bold run-in heading into the first paragraph of a block."""
    if body_html.startswith("<p>"):
        return body_html.replace("<p>", f"<p>{head_html} ", 1)
    return f"<p>{head_html}</p>\n{body_html}"


def _alt_from_filename(fname: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", fname.rsplit("/", 1)[-1])
    return stem.replace("_", " ").replace("-", " ")


def _parse_item_label(label: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    """Parse '2.', '(a)', 'iii.' → (ol type, value).  None if unparseable."""
    if label is None:
        return ("1", None)
    text = re.sub(r"<[^>]+>", "", label).strip()
    m = re.fullmatch(r"\(?(\d+)[.)]?\)?", text)
    if m:
        return ("1", int(m.group(1)))
    m = re.fullmatch(r"\(?([a-z])[.)]?\)?", text)
    if m:
        return ("a", ord(m.group(1)) - ord("a") + 1)
    m = re.fullmatch(r"\(?([A-Z])[.)]?\)?", text)
    if m:
        return ("A", ord(m.group(1)) - ord("A") + 1)
    return None
