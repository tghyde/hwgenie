"""Pre-rendered math for pages that must work offline and without scripts.

The student feedback page used to typeset its math in the browser with KaTeX
from a CDN.  Opened from a download on a phone (the iOS Files / Messages
preview runs no JavaScript) it showed raw LaTeX.  This module does the
browser's work at export time instead:

* comment markers are placed into the student's work exactly as the page's
  ``placeMarkers`` did (anchors are matched against the raw math source, so
  this has to happen before typesetting);
* every formula is typeset by KaTeX, run inside Python by QuickJS, with the
  same delimiters, macros and options KaTeX's auto-render used on the page;
* foldeq displays are pre-rendered fully folded (narrowest form, the right
  one for a phone); a browser that runs scripts refits them to its width;
* the KaTeX stylesheet is inlined with only the fonts the math uses embedded
  as data URIs.

KaTeX 0.16.21 is bundled (``hwgenie/katex``, MIT licence), so nothing is
fetched.  ``available()`` is False when the ``quickjs`` package is missing;
callers then fall back to the CDN page.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

from .htmltemplate import FOLD_JS

ASSETS = Path(__file__).resolve().parent / "katex"

# auto-render's delimiter list as the feedback page configured it, in its
# matching order
DELIMITERS = (("$$", "$$", True), ("\\[", "\\]", True),
              ("$", "$", False), ("\\(", "\\)", False))
_LEFT_RE = re.compile("|".join(re.escape(d[0]) for d in DELIMITERS))
# auto-render does not descend into these
IGNORED_TAGS = {"script", "noscript", "style", "textarea", "pre", "code",
                "option"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
# placeMarkers' block selector (whitespace is inserted between blocks)
BLOCK_TAGS = {"p", "div", "li", "td", "th", "blockquote", "pre", "h1", "h2",
              "h3", "figcaption"}
# JavaScript's \s
JS_WS = set("\t\n\v\f\r       　﻿"
            + "".join(chr(c) for c in range(0x2000, 0x200b)))


def available() -> bool:
    try:
        import quickjs  # noqa: F401
    except ImportError:
        return False
    return (ASSETS / "katex.min.js").is_file()


# ------------------------------------------------------------- tokenizing --

_TOKEN_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>"   # opaque: content is not text
    r"|<!--.*?-->"
    r"|<![^>]*>"
    r"|</?[A-Za-z][^>]*>",
    re.S | re.I)
_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w-]*)")
_CLASS_RE = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")


class _El:
    __slots__ = ("tag", "classes", "parent", "end")

    def __init__(self, tag, classes, parent):
        self.tag, self.classes, self.parent = tag, classes, parent
        self.end = None   # source offset just past the closing tag

    def closest(self, pred):
        el = self
        while el is not None:
            if pred(el):
                return el
            el = el.parent
        return None


def _text_nodes(src: str) -> list[tuple[int, int, _El | None]]:
    """Text nodes of an HTML fragment as (start, end, parent element)."""
    nodes = []
    cur: _El | None = None
    pos = 0
    for m in _TOKEN_RE.finditer(src):
        if m.start() > pos:
            nodes.append((pos, m.start(), cur))
        pos = m.end()
        tok = m.group(0)
        if m.group(1) or tok.startswith("<!"):
            continue   # script/style block, comment, doctype
        t = _TAG_RE.match(tok)
        name = t.group(2).lower()
        if t.group(1):   # end tag: close up to the matching element
            el = cur
            while el is not None and el.tag != name:
                el = el.parent
            if el is not None:
                c = cur
                while c is not el:
                    c.end = m.start()
                    c = c.parent
                el.end = m.end()
                cur = el.parent
            continue
        if name in VOID_TAGS or tok.endswith("/>"):
            continue
        cm = _CLASS_RE.search(tok)
        classes = set((next(g for g in cm.groups() if g is not None)
                       if cm else "").split())
        cur = _El(name, classes, cur)
    if pos < len(src):
        nodes.append((pos, len(src), cur))
    return nodes


_ENT_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);?")


def _decode(src: str) -> tuple[str, list[int]]:
    """The DOM text of a source text run, with offs[i] = the source offset
    of decoded character i (offs[len] = len(src))."""
    out: list[str] = []
    offs: list[int] = []
    pos = 0
    for m in _ENT_RE.finditer(src):
        for k in range(pos, m.start()):
            out.append(src[k])
            offs.append(k)
        d = html_mod.unescape(m.group(0))
        if d == m.group(0):
            for k in range(m.start(), m.end()):
                out.append(src[k])
                offs.append(k)
        else:
            for ch in d:
                out.append(ch)
                offs.append(m.start())
        pos = m.end()
    for k in range(pos, len(src)):
        out.append(src[k])
        offs.append(k)
    offs.append(len(src))
    return "".join(out), offs


# ------------------------------------------------------- comment markers --

def _js_trim_ws(s: str) -> str:
    out, space = [], False
    for ch in s:
        if ch in JS_WS:
            space = True
        else:
            if space and out:
                out.append(" ")
            out.append(ch)
            space = False
    return "".join(out)


def _math_spans(t: str) -> list[tuple[int, int]]:
    spans = []
    open_, closer = -1, None
    for m in re.finditer(r"\\\(|\\\)|\\\[|\\\]|\$", t):
        g = m.group(0)
        if g == "$" and m.start() > 0 and t[m.start() - 1] == "\\":
            continue
        if open_ == -1:
            closer = {"$": "$", "\\(": "\\)", "\\[": "\\]"}.get(g)
            if closer:
                open_ = m.start()
        elif g == closer:
            spans.append((open_, m.end()))
            open_ = -1
    return spans


def marker_html(i: int) -> str:
    return f'<sup class="cmark" data-ci="{i}">{i + 1}</sup>'


def place_markers(frag: str, comments: list) -> str:
    """Insert a numbered marker after each comment's anchor text, matched
    the way the page's placeMarkers did it: against the whitespace-collapsed
    text of the (not yet typeset) work, first occurrence, pushed past the
    end of any math span it lands in, or after a display it lands inside.
    """
    jobs = []
    for i, c in enumerate(comments):
        needle = _js_trim_ws(c.get("anchor") or "")
        if needle:
            jobs.append((i, needle))
    if not jobs:
        return frag
    nodes = _text_nodes(frag)
    box = object()
    norm: list[str] = []
    mp: list[tuple[int, int]] = []
    decoded = []
    last_space, last_blk = True, None
    for ni, (s, e, par) in enumerate(nodes):
        text, offs = _decode(frag[s:e])
        decoded.append((text, offs))
        blk = (par.closest(lambda el: el.tag in BLOCK_TAGS)
               if par is not None else None) or box
        if blk is not last_blk and not last_space and norm:
            norm.append(" ")
            mp.append((ni, 0))
            last_space = True
        last_blk = blk
        for off, ch in enumerate(text):
            if ch in JS_WS:
                if last_space:
                    continue
                norm.append(" ")
                last_space = True
            else:
                norm.append(ch)
                last_space = False
            mp.append((ni, off))
    flat = "".join(norm)
    inserts = []
    for i, needle in jobs:
        at = flat.find(needle)
        if at == -1:
            continue
        ni, off = mp[at + len(needle) - 1]
        off += 1
        text, offs = decoded[ni]
        for a, b in _math_spans(text):
            if a < off < b:
                off = b
                break
        s, _e, par = nodes[ni]
        disp = (par.closest(lambda el: "math-display" in el.classes)
                if par is not None else None)
        if disp is not None:
            pos = disp.end if disp.end is not None else len(frag)
        else:
            pos = s + offs[min(off, len(text))]
        inserts.append((pos, i))
    inserts.sort()
    out, last = [], 0
    for pos, i in inserts:
        out.append(frag[last:pos])
        out.append(marker_html(i))
        last = pos
    out.append(frag[last:])
    return "".join(out)


# ------------------------------------------------------------- typesetting --

def _find_end(delim: str, text: str, start: int) -> int:
    """auto-render's findEndOfMath."""
    i, brace = start, 0
    n = len(delim)
    while i < len(text):
        ch = text[i]
        if brace <= 0 and text[i:i + n] == delim:
            return i
        if ch == "\\":
            i += 1
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        i += 1
    return -1


def split_math(text: str) -> list[tuple]:
    """auto-render's splitAtDelimiters: [("text", s) | ("math", tex,
    display, raw)]."""
    data: list[tuple] = []
    while True:
        m = _LEFT_RE.search(text)
        if m is None:
            break
        if m.start() > 0:
            data.append(("text", text[:m.start()]))
            text = text[m.start():]
        left, right, display = next(d for d in DELIMITERS
                                    if text.startswith(d[0]))
        end = _find_end(right, text, len(left))
        if end == -1:
            break
        data.append(("math", text[len(left):end], display,
                     text[:end + len(right)]))
        text = text[end + len(right):]
    if text:
        data.append(("text", text))
    return data


_DEFINES_RE = re.compile(r"\\(?:[gex]?def|(?:re)?newcommand|let|global)\b")
_FOLDEQ_RE = re.compile(r'<div class="foldeq"([^>]*)>\s*</div>')
_ATTR_RE = re.compile(r'([\w-]+)(?:\s*=\s*"([^"]*)")?')


class Renderer:
    """KaTeX in a QuickJS context.  Macro sets are named regions: every
    formula typeset under one region shares one macros object, as the page
    shared one object per typeset() target (so \\newcommand in one formula
    is seen by the later ones)."""

    def __init__(self):
        import quickjs
        ctx = quickjs.Context()
        ctx.eval("var module = {exports: {}}; var exports = module.exports;"
                 "var document = {addEventListener: function() {}};")
        ctx.eval((ASSETS / "katex.min.js").read_text(encoding="utf-8"))
        ctx.eval("var katex = module.exports.default || module.exports;")
        ctx.eval(FOLD_JS)
        ctx.eval("var REGIONS = {};")
        self._define = ctx.eval(
            "(function(k, m) { REGIONS[k] = JSON.parse(m); })")
        self._render = ctx.eval("""(function(tex, display, k) {
            try {
              return katex.renderToString(tex, {displayMode: display,
                macros: REGIONS[k], throwOnError: false, strict: false});
            } catch (e) { return null; }
        })""")
        self._fold = ctx.eval("""(function(src, tag) {
            var p = foldParse(src);
            return foldBuild(p.parts, p.rels, p.rels.length) + tag;
        })""")
        self._ctx = ctx
        self.rendered: list[str] = []
        self.failed: list[str] = []
        self._defs: dict = {}
        # the problem statements repeat on every student's page
        self._cache: dict = {}

    def region(self, key: str, macros: dict | None) -> str:
        m = json.dumps(macros or {}, sort_keys=True)
        self._define(key, m)
        self._defs[key] = m
        return key

    def tex(self, tex: str, display: bool, region: str) -> str | None:
        out = self._render(tex, display, region)
        if out is None:
            self.failed.append(tex)
        else:
            self.rendered.append(out)
        return out

    def typeset(self, frag: str, region: str) -> str:
        """auto-render over an HTML fragment, then its foldeq displays."""
        key = None
        if not _DEFINES_RE.search(frag):   # no macro state carried along
            key = (frag, self._defs.get(region))
            hit = self._cache.get(key)
            if hit is not None:
                self.rendered.extend(hit[1])
                self.failed.extend(hit[2])
                return hit[0]
        r0, f0 = len(self.rendered), len(self.failed)
        out = self._typeset(frag, region)
        if key is not None:
            self._cache[key] = (out, self.rendered[r0:], self.failed[f0:])
        return out

    def _typeset(self, frag: str, region: str) -> str:
        out, last = [], 0
        for s, e, par in _text_nodes(frag):
            if par is not None and par.closest(
                    lambda el: el.tag in IGNORED_TAGS):
                continue
            text, _offs = _decode(frag[s:e])
            pieces = split_math(text)
            if not any(p[0] == "math" for p in pieces):
                continue
            buf = []
            for p in pieces:
                if p[0] == "text":
                    buf.append(html_mod.escape(p[1], quote=False))
                    continue
                r = self.tex(p[1], p[2], region)
                buf.append(html_mod.escape(p[3], quote=False) if r is None
                           else f"<span>{r}</span>")
            out.append(frag[last:s])
            out.append("".join(buf))
            last = e
        out.append(frag[last:])
        return self._folds("".join(out), region)

    def _folds(self, frag: str, region: str) -> str:
        def one(m):
            attrs = {k: html_mod.unescape(v or "")
                     for k, v in _ATTR_RE.findall(m.group(1))}
            src = attrs.get("data-tex")
            if not src:
                return m.group(0)
            r = self.tex(self._fold(src, attrs.get("data-tag", "")), True,
                         region)
            return m.group(0) if r is None else (
                f'<div class="foldeq"{m.group(1)}>{r}</div>')
        return _FOLDEQ_RE.sub(one, frag)


# ------------------------------------------------------------------ fonts --
# Which of KaTeX's fonts the rendered markup needs: follow the stylesheet's
# font rules down each formula's element tree (from the math-notes share
# build).

_FONT_URL_RE = re.compile(r"url\((fonts/[^)]+\.woff2)\)")
_FONT_FACE_RE = re.compile(r"@font-face\{[^}]*\}")


def _norm_weight(v: str) -> str:
    v = v.strip()
    return {"bold": "700", "normal": "400"}.get(v, v)


def _first_family(v: str) -> str:
    return v.split(",")[0].strip().strip("\"'")


def _decls(block: str) -> dict:
    out = {}
    for decl in block.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _font_rules(css: str) -> list:
    body = _FONT_FACE_RE.sub("", css)
    rules = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        props = {k: v for k, v in _decls(m.group(2)).items()
                 if k in ("font-family", "font-weight", "font-style")}
        if not props:
            continue
        for sel in m.group(1).split(","):
            sel = sel.strip()
            if ":" in sel:
                continue
            compounds = []
            for part in re.split(r"\s*>\s*|\s+", sel):
                tag = re.match(r"^[a-zA-Z]+", part)
                compounds.append((tag.group(0) if tag else None,
                                  set(re.findall(r"\.([\w-]+)", part))))
            spec = sum(len(c) for _t, c in compounds)
            rules.append((spec, len(rules), compounds, props))
    rules.sort(key=lambda r: (r[0], r[1]))
    return rules


def _matches(compounds, chain) -> bool:
    tag, classes = compounds[-1]
    etag, eclasses = chain[-1]
    if (tag and tag != etag) or not classes <= eclasses:
        return False
    k, i = len(compounds) - 2, len(chain) - 2
    while k >= 0 and i >= 0:
        t, c = compounds[k]
        if (not t or t == chain[i][0]) and c <= chain[i][1]:
            k -= 1
        i -= 1
    return k < 0


_CHAIN_CACHE: dict = {}


class _FontWalker(HTMLParser):
    def __init__(self, rules):
        super().__init__(convert_charrefs=True)
        self.rules = rules
        self.used: set = set()
        self._stack: list = []

    def handle_starttag(self, tag, attrs):
        classes = frozenset((dict(attrs).get("class") or "").split())
        chain = [c for c, _p in self._stack] + [(tag, classes)]
        props = dict(self._stack[-1][1]) if self._stack else {
            "font-family": "", "font-weight": "400", "font-style": "normal"}
        ckey = tuple(chain)
        decls = _CHAIN_CACHE.get(ckey)
        if decls is None:
            decls = [d for _s, _o, compounds, d in self.rules
                     if _matches(compounds, chain)]
            _CHAIN_CACHE[ckey] = decls
        for decl in decls:
            props.update(decl)
        if tag not in VOID_TAGS:
            self._stack.append(((tag, classes), props))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if not data.strip() or not self._stack:
            return
        if any("katex-mathml" in c for (_t, c), _p in self._stack):
            return   # screen-reader copy: clipped away, draws no glyphs
        props = self._stack[-1][1]
        fam = _first_family(props["font-family"])
        if fam.startswith("KaTeX_"):
            self.used.add((fam, _norm_weight(props["font-weight"]),
                           props["font-style"].strip()))


def _font_faces(css: str) -> list:
    faces = []
    for block in _FONT_FACE_RE.findall(css):
        props = _decls(block[len("@font-face{"):-1])
        m = _FONT_URL_RE.search(props.get("src", ""))
        if m:
            faces.append((_first_family(props["font-family"]),
                          _norm_weight(props.get("font-weight", "400")),
                          props.get("font-style", "normal").strip(),
                          m.group(1)))
    return faces


@lru_cache(maxsize=1)
def _css() -> str:
    return (ASSETS / "katex.min.css").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _font_data(font: str) -> str:
    return base64.b64encode((ASSETS / font).read_bytes()).decode("ascii")


@lru_cache(maxsize=1)
def _rules() -> list:
    return _font_rules(_css())


@lru_cache(maxsize=4096)
def _fonts_in(rendered: str) -> frozenset:
    walker = _FontWalker(_rules())
    walker.feed(rendered)
    walker.close()
    return frozenset(walker.used)


def katex_style(rendered: list[str]) -> str:
    """The KaTeX stylesheet with the fonts these formulas use embedded
    (the regular roman face always: digits, parentheses, relations)."""
    css = _css()
    faces = _font_faces(css)
    used = {("KaTeX_Main", "400", "normal")}
    for h in rendered:
        used |= _fonts_in(h)
    keep: set = set()
    for fam, weight, style in used:
        exact = [f for f in faces if f[:3] == (fam, weight, style)]
        # no exact face: the browser picks the nearest in the family, so
        # keep the whole family rather than guess its choice
        keep.update(f[3] for f in (exact or [f for f in faces
                                               if f[0] == fam]))

    def face(m):
        block = m.group(0)
        font = _FONT_URL_RE.search(block).group(1)
        if font not in keep:
            return ""
        return re.sub(r"src:[^;}]*",
                      f'src:url(data:font/woff2;base64,{_font_data(font)})'
                      ' format("woff2")', block)
    return _FONT_FACE_RE.sub(face, css)


@lru_cache(maxsize=1)
def katex_script() -> str:
    """katex.min.js, safe to inline in a <script> element."""
    js = (ASSETS / "katex.min.js").read_text(encoding="utf-8")
    return js.replace("</script", "<\\/script").replace("<!--", "<\\!--")
