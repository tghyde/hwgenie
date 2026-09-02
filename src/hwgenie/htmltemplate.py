"""Responsive HTML page shell for hwgenie outputs."""

from __future__ import annotations

import html as html_mod
import json
from typing import Dict, List, Optional, Tuple

KATEX_VERSION = "0.16.21"

from .themes import theme_css as _theme_css

DEFAULT_THEME_CSS = _theme_css()

CSS = """
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
[id] { scroll-margin-top: 4.5rem; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-body, Charter, Georgia, serif);
  font-size: 1.0625rem;
  line-height: 1.65;
}
a { color: var(--accent); text-decoration: none; }
main a:not(.filebox) { padding: 0 .12em; margin: 0 -.12em; }
main a:not(.filebox):hover { background: var(--hover-bg); }
main {
  max-width: 44rem;
  margin: 0 auto;
  padding: 1.25rem 1.1rem 4rem;
}
header.doc {
  text-align: center;
  margin: 2rem 0 2.5rem;
}
header.doc .course {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 .4rem;
}
header.doc h1 {
  font-size: 1.6rem;
  line-height: 1.25;
  margin: 0;
  font-weight: 700;
}
/* Optional banner art (static/banner.*): hero above the page content, with
   the usual title header floating in a borderless card. */
.hero {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: 44rem;   /* match the text column so wide screens don't stretch it */
  margin: 0 auto;
  min-height: 11rem;  /* fixed: card-to-edge spacing must not scale with viewport */
  padding: 2.5rem 1.1rem;
}
.hero img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.hero header.doc {
  position: relative;
  margin: 0;
  max-width: min(100%, 38rem);
  padding: 1.2rem 2rem;
  background: var(--card-bg);
}
header.doc p.due {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .95rem;
  font-weight: 600;
  color: var(--accent);
  margin: .55rem 0 0;
}
header.doc p.due .due-label {
  font-size: .8rem;
  letter-spacing: .12em;
  text-transform: uppercase;
  margin-right: .45em;
}
.badge {
  display: inline-block;
  margin-top: .8rem;
  padding: .2rem .85rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .8rem;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--sol-accent);
  background: var(--sol-bg);
}
p { margin: 0 0 .9em; }
hr.sep {
  border: none;
  border-top: 1px solid var(--border);
  width: 55%;
  margin: 2.2rem auto;
}
.center { text-align: center; margin: 1.2rem 0; }
blockquote.epigraph {
  margin: 1.8rem auto;
  max-width: 34rem;
  text-align: center;
  font-style: italic;
}
blockquote.epigraph footer {
  margin-top: .5rem;
  font-style: normal;
  font-size: .9rem;
  color: var(--muted);
}
.task { color: var(--accent); }
.alert { color: var(--alert); }

details.problem {
  scroll-margin-top: 4.5rem;
  background: var(--card-bg);
  padding: 1.15rem 1.35rem;
  margin: 1.9rem 0;
}
/* color blocks get even visual padding: kill trailing paragraph margins */
.thmblock > :last-child,
.proof > :last-child,
.solution-body > :last-child,
details.problem > :last-child,
blockquote.epigraph > :last-child,
pre.code > :last-child {
  margin-bottom: 0;
}
.thmblock > :first-child { margin-top: 0; }
details.problem > summary {
  cursor: pointer;
  list-style: none;
  margin: 0 0 .8rem;
}
details.problem > summary::-webkit-details-marker { display: none; }
details.problem > summary::before {
  content: "▾";
  color: var(--accent);
  margin-right: .5em;
  display: inline-block;
  transition: transform .15s;
}
details.problem:not([open]) > summary::before { transform: rotate(-90deg); }
details.problem:not([open]) > summary { margin-bottom: .3rem; }
.problem-title {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .95rem;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0;
  display: inline;
}
.problem-title .problem-note {
  text-transform: none;
  letter-spacing: normal;
  font-weight: 600;
}

.htmlcard {
  background: var(--card-bg);
  padding: 1.15rem 1.35rem;
  margin: 1.6rem 0;
}
.htmlcard > :last-child { margin-bottom: 0; }
.card-title {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .95rem;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 .7rem;
}

details.solution {
  background: var(--sol-bg);
  padding: .65rem .95rem;
  margin: 1.1rem 0;
}
details.solution summary {
  cursor: pointer;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--sol-accent);
}
.solution-body { margin-top: .6rem; }
/* amsthm-style tombstone (proofs only; solution boxes need no qed in HTML) */
.proof > :last-child::after {
  content: "";
  float: right;
  width: .62em;
  height: .62em;
  margin-top: .35em;
  margin-left: .5em;
  border: 1.2px solid currentColor;
}
.proof.has-qedhere > :last-child::after { content: none; }
.qedbox {
  float: right;
  width: .62em;
  height: .62em;
  margin-top: .35em;
  margin-left: .5em;
  border: 1.2px solid currentColor;
}
.nw { white-space: nowrap; }

ol, ul { padding-left: 1.6rem; margin: 0 0 .9em; }
li { margin-bottom: .45em; }
ul.no-marker { list-style: none; padding-left: .6rem; }
.li-label { font-weight: 600; }

figure.fig {
  margin: 1.4rem auto;
  text-align: center;
}
figure.fig img {
  max-width: min(100%, 620px);
  height: auto;
}
figure.fig figcaption {
  font-size: .9rem;
  color: var(--muted);
  margin-top: .4rem;
}

/* Inline TikZ diagrams (pre-rendered SVG).  Black strokes/fills were
   rewritten to currentColor, so the diagram follows the theme. */
.tikz-figure {
  text-align: center;
  margin: 1.4rem 0;
  color: var(--fg);
}

pre.code {
  background: var(--code-bg);
  padding: .9rem 1rem;
  overflow-x: auto;
  font-size: .82rem;
  line-height: 1.5;
}
pre.code code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: .88em;
}

.table-wrap { overflow-x: auto; margin: 1.2rem 0; }
.table-wrap table {
  border-collapse: collapse;
  margin: 0 auto;
  font-size: .95rem;
}
.table-wrap th, .table-wrap td {
  border: 1px solid var(--border);
  padding: .35rem .75rem;
}
.table-wrap th { background: var(--code-bg); font-weight: 600; }
.al-left { text-align: left; }
.al-center { text-align: center; }
.al-right { text-align: right; }

.thmblock {
  background: var(--card-bg);
  padding: 1.05rem 1.25rem;
  margin: 1.5rem 0;
}
.thm-head {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 .55rem;
}
.thm-head .thm-note {
  text-transform: none;
  letter-spacing: normal;
  font-weight: 400;
  font-size: .95rem;
  color: var(--muted);
}
.proof {
  background: var(--sol-bg);
  padding: 1.05rem 1.25rem;
  margin: 1.5rem 0 1.7rem;
}
.proof-label {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  font-weight: 700;
  font-style: normal;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--sol-accent);
  margin: 0 0 .55rem;
}
/* A proof inside a solution box is already on green — no box-in-box. */
.solution-body .proof { background: transparent; padding: 0; margin: 1rem 0; }

/* Centered section banners (lesson subsections etc.). Titles keep normal
   case — many contain math. */
h2.sec-head, h3.sec-head {
  text-align: center;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-weight: 700;
  color: var(--accent);
  font-size: 1.15rem;
  margin: 2.6rem 0 1.3rem;
}
h3.sec-head { font-size: 1.02rem; }
.sec-num::after { content: " · "; }
.xref { color: var(--accent); }
sup.fn a { color: var(--accent); font-weight: 600; }
section.footnotes { font-size: .9rem; color: var(--muted); }
section.footnotes ol { padding-left: 1.3rem; }
.fn-back { text-decoration: none; }

.math-display { overflow-x: auto; }
.katex-display { overflow-x: auto; overflow-y: hidden; padding: .2rem 0; }

footer.doc {
  margin-top: 3.5rem;
  text-align: center;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .78rem;
  color: var(--muted);
}

#themetoggle {
  position: fixed;
  top: .5rem;
  right: .7rem;
  z-index: 60;
  background: var(--card-bg);
  color: var(--fg);
  border: none;
  padding: .3rem .6rem;
  font-size: 1rem;
  line-height: 1;
  cursor: pointer;
}
#themetoggle:hover { background: var(--code-bg); }
@media (prefers-reduced-motion: no-preference) {
  body { transition: background-color .2s ease, color .2s ease; }
}
"""

# Applies a saved manual theme before first paint (no flash); without a saved
# choice the CSS media query follows the system preference.
THEME_HEAD_SCRIPT = (
    "<script>(function(){try{var t=localStorage.getItem('hwg-theme');"
    "if(t==='light'||t==='dark')document.documentElement.setAttribute("
    "'data-theme',t);}catch(e){}})();</script>"
)

THEME_TOGGLE_HTML = (
    '<button id="themetoggle" aria-label="Toggle light or dark theme" '
    'title="Toggle light/dark"></button>'
)

THEME_TOGGLE_JS = """<script>
(function() {
  var b = document.getElementById("themetoggle");
  if (!b) return;
  function effective() {
    var a = document.documentElement.getAttribute("data-theme");
    if (a) return a;
    return window.matchMedia &&
      matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function icon() { b.textContent = effective() === "dark" ? "\\u2600" : "\\u263E"; }
  b.addEventListener("click", function() {
    var next = effective() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("hwg-theme", next); } catch (e) {}
    icon();
  });
  if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", icon);
  }
  icon();
})();
</script>"""


HWGENIE_URL = "https://github.com/tghyde/hwgenie"

FOOTER_HTML = (
    f'Generated by <a href="{HWGENIE_URL}">hwGenie</a>'
)

DOWNLOAD_ICON = (
    '<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" '
    'aria-hidden="true"><path d="M8.75 1h-1.5v7.1L4.9 5.75 3.85 6.8 8 10.95 '
    '12.15 6.8 11.1 5.75 8.75 8.1V1z"/>'
    '<path d="M2.5 12.5h11V14h-11z"/></svg>'
)


def file_box(href: str, label: str) -> str:
    """A single bordered download button: label + icon, one click target."""
    return (
        f'<a class="filebox" href="{href}" download '
        f'aria-label="Download {label}">{label} {DOWNLOAD_ICON}</a>'
    )


def view_box(href: str, label: str) -> str:
    """A bordered navigation button (no download)."""
    return f'<a class="filebox viewbox" href="{href}">{label}</a>'


# foldeq support: displays emitted as <div class="foldeq" data-tex="..."> are
# re-broken at their \fold{rel} markers to fit the viewport.  Fold levels
# activate from the END of the line; folded rows align under the last
# relation still on row 1 (an author & in the lead segment anchors the fully
# folded form).  Past max fold: shrink font to 70%, then scroll.
FOLD_JS = r"""
function foldParse(tex) {
  var parts = [], rels = [], depth = 0, start = 0;
  for (var i = 0; i < tex.length; i++) {
    var c = tex[i];
    if (c === "\\" && tex.slice(i, i + 5) === "\\fold" && depth === 0) {
      parts.push(tex.slice(start, i).trim());
      i += 5;
      while (i < tex.length && /\s/.test(tex[i])) i++;
      if (tex[i] !== "{") { rels.push(""); start = i; continue; }
      var d = 1, j = i + 1;
      while (j < tex.length && d > 0) {
        if (tex[j] === "{") d++;
        else if (tex[j] === "}") d--;
        j++;
      }
      rels.push(tex.slice(i + 1, j - 1).trim());
      start = j;
      i = j - 1;
    } else if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === "\\") i++;
  }
  parts.push(tex.slice(start).trim());
  return { parts: parts, rels: rels };
}
function foldStripAnchors(s) {
  return s.replace(/(^|[^\\])&/g, "$1");
}
function foldBuild(parts, rels, level) {
  var k = rels.length;
  if (level === 0) {
    return foldStripAnchors(parts.map(function(p, i) {
      return i ? rels[i - 1] + " " + p : p;
    }).join(" "));
  }
  var firstActive = k - level;
  var row1, indent = false;
  if (firstActive === 0) {
    if (/(^|[^\\])&/.test(parts[0])) {
      row1 = parts[0];
    } else {
      row1 = "&" + parts[0];
      indent = true;
    }
  } else {
    var s = foldStripAnchors(parts[0]);
    for (var i = 0; i < firstActive - 1; i++)
      s += " " + rels[i] + " " + foldStripAnchors(parts[i + 1]);
    row1 = s + " &" + rels[firstActive - 1] + " "
             + foldStripAnchors(parts[firstActive]);
  }
  var body = row1;
  for (var i = firstActive; i < k; i++)
    body += " \\\\ &" + (indent ? "\\quad " : "")
          + rels[i] + " " + foldStripAnchors(parts[i + 1]);
  return "\\begin{aligned}" + body + "\\end{aligned}";
}
var foldMeasurer = null;
function foldWidth(tex) {
  if (!foldMeasurer) {
    foldMeasurer = document.createElement("div");
    foldMeasurer.style.cssText =
      "position:absolute;left:-10000px;top:0;visibility:hidden;width:max-content;";
    document.body.appendChild(foldMeasurer);
  }
  katex.render(tex, foldMeasurer,
               { displayMode: true, throwOnError: false, macros: katexMacros });
  var k = foldMeasurer.querySelector(".katex");
  return k ? k.scrollWidth : foldMeasurer.scrollWidth;
}
function fitFolds() {
  if (typeof katex === "undefined") return;
  document.querySelectorAll(".foldeq").forEach(function(el) {
    var src = el.getAttribute("data-tex");
    if (!src) return;
    var parsed = foldParse(src);
    var tag = el.getAttribute("data-tag") || "";
    var target = el.clientWidth - 2;
    var level = parsed.rels.length; // nothing fits: max fold, then shrink
    for (var j = 0; j <= parsed.rels.length; j++) {
      if (foldWidth(foldBuild(parsed.parts, parsed.rels, j) + tag) <= target) {
        level = j;
        break;
      }
    }
    if (el._foldLevel !== level) {
      el._foldLevel = level;
      katex.render(foldBuild(parsed.parts, parsed.rels, level) + tag, el,
                   { displayMode: true, throwOnError: false,
                     macros: katexMacros });
    }
    el.style.fontSize = "";
    if (el.scrollWidth > el.clientWidth + 1) {
      var scale = el.clientWidth / el.scrollWidth;
      if (scale >= 0.7) el.style.fontSize = (scale * 100).toFixed(1) + "%";
      // below 70% keep full size; the container scrolls horizontally
    }
  });
}
"""


def katex_block(macros_json: str) -> str:
    """KaTeX assets + auto-render + fold + wide-display auto-scaling."""
    return f"""<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/contrib/auto-render.min.js"></script>
<script>
var katexMacros = {macros_json};
document.addEventListener("DOMContentLoaded", function() {{
  renderMathInElement(document.body, {{
    delimiters: [
      {{left: "$$", right: "$$", display: true}},
      {{left: "\\\\[", right: "\\\\]", display: true}},
      {{left: "$", right: "$", display: false}},
      {{left: "\\\\(", right: "\\\\)", display: false}}
    ],
    macros: katexMacros,
    throwOnError: false
  }});
  fitAll();
}});
{FOLD_JS}
function fitDisplays() {{
  document.querySelectorAll(".katex-display").forEach(function(d) {{
    if (d.closest(".foldeq")) return; // foldeq manages its own fitting
    d.style.fontSize = "";
    if (d.scrollWidth > d.clientWidth + 1) {{
      var scale = d.clientWidth / d.scrollWidth;
      if (scale >= 0.7) d.style.fontSize = (scale * 100).toFixed(1) + "%";
      // below 70% keep full size; the container scrolls horizontally
    }}
  }});
}}
function fitAll() {{
  fitFolds();
  fitDisplays();
}}
var fitTimer = null;
window.addEventListener("resize", function() {{
  clearTimeout(fitTimer);
  fitTimer = setTimeout(fitAll, 150);
}});
window.addEventListener("load", fitAll);
if (document.fonts && document.fonts.ready) {{
  document.fonts.ready.then(fitAll);
}}
if (window.ResizeObserver) {{
  document.addEventListener("DOMContentLoaded", function() {{
    new ResizeObserver(function() {{
      clearTimeout(fitTimer);
      fitTimer = setTimeout(fitAll, 150);
    }}).observe(document.body);
  }});
}}
</script>"""


NAV_CSS = """
.filebox {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  background: var(--card-bg);
  padding: .25rem .6rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  white-space: nowrap;
  color: var(--accent);
  text-decoration: none;
}
.filebox:hover { background: var(--accent); color: var(--bg); }
.filebox svg { flex-shrink: 0; }
.assignment .filebox { background: var(--bg); }
.assignment .filebox:hover { background: var(--accent); color: var(--bg); }

.scrollbar {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  gap: .9rem;
  padding: .45rem 3.4rem .45rem .9rem;
  background: var(--card-bg);
  box-shadow: 0 1px 10px rgba(0, 0, 0, .14);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  overflow-x: auto;
  transform: translateY(-100%);
  transition: transform .18s ease;
  white-space: nowrap;
}
.scrollbar.visible { transform: translateY(0); }
.scrollbar a { color: var(--accent); padding: .1rem .3rem; }
.scrollbar a:hover { background: var(--hover-bg); }
.scrollbar .sb-label { color: var(--muted); }
.scrollbar .sb-jumps { display: flex; gap: .7rem; }
.scrollbar .sb-top { margin-left: auto; }
@media (max-width: 30rem) {
  .scrollbar { gap: .55rem; padding-left: .7rem; }
}
nav.site {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  margin: 1.2rem 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: .3rem 1rem;
}
nav.site .sep-dot { color: var(--muted); }
.scrollbar .sb-toc {
  background: none;
  border: none;
  font: inherit;
  color: var(--accent);
  cursor: pointer;
  padding: .1rem .3rem;
}
.scrollbar .sb-toc:hover { background: var(--hover-bg); }
.scrollbar .sb-toc::after { content: " ▾"; }
.scrollbar .sb-toc[aria-expanded="true"]::after { content: " ▴"; }

/* Table of contents: a fixed sidebar in the left gutter when the viewport
   is wide enough to hold one beside the 44rem text column; otherwise a
   dropdown panel under the sticky bar, opened by its "Contents" button. */
nav.toc {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .82rem;
  line-height: 1.4;
}
nav.toc .toc-title {
  font-size: .72rem;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 .45rem .6rem;
}
nav.toc ul { list-style: none; margin: 0; padding: 0; }
nav.toc li { margin: 0; }
nav.toc a {
  display: block;
  color: var(--fg);
  padding: .28rem .6rem;
  border-left: 2px solid transparent;
}
nav.toc a:hover { background: var(--hover-bg); }
nav.toc a.active {
  color: var(--accent);
  font-weight: 600;
  border-left-color: var(--accent);
}
nav.toc .toc-num { color: var(--muted); font-weight: 400; margin-right: .45em; }
nav.toc li.toc-l2 a { padding-left: 1.5rem; }
nav.toc li.toc-l3 a { padding-left: 2.4rem; }
@media (min-width: 74rem) {
  nav.toc {
    position: fixed;
    top: 5rem;
    left: calc(50% - 37.5rem);   /* 22rem half-column + 1.5rem gap + 14rem */
    width: 14rem;
    max-height: calc(100vh - 6.5rem);
    overflow-y: auto;
    z-index: 40;
  }
  .scrollbar .sb-toc { display: none; }
}
@media (max-width: 73.99rem) {
  nav.toc {
    position: fixed;
    left: 0; right: 0;
    top: 0;                      /* moved below the sticky bar by script */
    z-index: 49;
    display: none;
    background: var(--card-bg);
    box-shadow: 0 6px 14px rgba(0, 0, 0, .14);
    max-height: min(70vh, 26rem);
    overflow-y: auto;
    padding: .5rem .6rem .7rem;
  }
  nav.toc.open { display: block; }
  nav.toc .toc-title { display: none; }
}
@media print {
  nav.toc, .scrollbar, #themetoggle { display: none; }
}
"""


SCROLLBAR_JS = """
<script>
(function() {
  var bar = document.getElementById("scrollnav");
  if (!bar) return;
  var shown = false;
  window.addEventListener("scroll", function() {
    var want = window.scrollY > 350;
    if (want !== shown) {
      shown = want;
      bar.classList.toggle("visible", want);
    }
  }, {passive: true});
  bar.addEventListener("click", function(ev) {
    var a = ev.target.closest("a");
    if (!a) return;
    var href = a.getAttribute("href");
    if (href === "#top") {
      ev.preventDefault();
      window.scrollTo({top: 0, behavior: "smooth"});
    } else if (href && href.charAt(0) === "#") {
      var t = document.querySelector(href);
      if (t && t.tagName === "DETAILS") t.open = true;
    }
  });
})();
</script>
"""


TOC_JS = """
<script>
(function() {
  var toc = document.getElementById("toc");
  if (!toc) return;
  var bar = document.getElementById("scrollnav");
  var btn = bar ? bar.querySelector(".sb-toc") : null;
  var wide = window.matchMedia ? matchMedia("(min-width: 74rem)") : null;
  var links = Array.prototype.slice.call(toc.querySelectorAll("a[href^='#']"));
  var targets = links.map(function(a) {
    return document.getElementById(a.getAttribute("href").slice(1));
  });

  function setOpen(want) {
    toc.classList.toggle("open", want);
    if (btn) btn.setAttribute("aria-expanded", want ? "true" : "false");
    if (want && bar) toc.style.top = bar.offsetHeight + "px";
  }
  if (btn) {
    btn.addEventListener("click", function(ev) {
      ev.stopPropagation();
      setOpen(!toc.classList.contains("open"));
    });
  }
  toc.addEventListener("click", function(ev) {
    if (ev.target.closest("a")) setOpen(false);
  });
  document.addEventListener("click", function(ev) {
    if (toc.classList.contains("open") && !toc.contains(ev.target)) setOpen(false);
  });
  document.addEventListener("keydown", function(ev) {
    if (ev.key === "Escape") setOpen(false);
  });
  window.addEventListener("resize", function() { setOpen(false); });

  var current = -1;
  function spy() {
    // The panel only makes sense while the bar it hangs from is on screen.
    if (bar && toc.classList.contains("open") && !bar.classList.contains("visible")) {
      setOpen(false);
    }
    var limit = 90, idx = -1;
    for (var i = 0; i < targets.length; i++) {
      if (targets[i] && targets[i].getBoundingClientRect().top <= limit) idx = i;
    }
    if (targets.length &&
        window.innerHeight + window.scrollY >= document.body.scrollHeight - 2) {
      idx = targets.length - 1;
    }
    if (idx === current) return;
    current = idx;
    links.forEach(function(a, i) {
      a.classList.toggle("active", i === idx);
      if (i === idx) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
    if (idx >= 0 && wide && wide.matches) {
      var a = links[idx], top = a.offsetTop, bottom = top + a.offsetHeight;
      if (top < toc.scrollTop) toc.scrollTop = top - 8;
      else if (bottom > toc.scrollTop + toc.clientHeight) {
        toc.scrollTop = bottom - toc.clientHeight + 8;
      }
    }
  }
  var pending = false;
  window.addEventListener("scroll", function() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function() { pending = false; spy(); });
  }, {passive: true});
  window.addEventListener("load", spy);
  spy();
})();
</script>
"""


def toc_html(sections: List[Tuple[int, str, str, str]]) -> str:
    """Table-of-contents nav from (level, number, title html, anchor id)
    entries.  Levels are normalized so the shallowest heading present sits
    flush left (a handout made only of subsections is not indented)."""
    if not sections:
        return ""
    base = min(level for level, _n, _t, _a in sections)
    items = []
    for level, num, title, anchor in sections:
        depth = min(level - base + 1, 3)
        num_html = f'<span class="toc-num">{html_mod.escape(num)}</span>' if num else ""
        items.append(
            f'<li class="toc-l{depth}"><a href="#{html_mod.escape(anchor)}">'
            f"{num_html}{title}</a></li>"
        )
    return (
        '<nav class="toc" id="toc" aria-label="Table of contents">\n'
        '<p class="toc-title">Contents</p>\n<ul>\n'
        + "\n".join(items)
        + "\n</ul>\n</nav>"
    )


def scrollbar_html(
    home_href: Optional[str],
    home_label: str,
    page_label: str,
    jump_links: Optional[List[Tuple[str, str]]] = None,
    toc_toggle: bool = False,
) -> str:
    home = (
        f'<a href="{home_href}">← {html_mod.escape(home_label)}</a>'
        if home_href else ""
    )
    jumps = ""
    if jump_links:
        items = " ".join(
            f'<a href="#{html_mod.escape(anchor)}">{html_mod.escape(num)}</a>'
            for num, anchor in jump_links
        )
        jumps = f'<span class="sb-jumps">{items}</span>'
    toggle = (
        '<button type="button" class="sb-toc" aria-controls="toc" '
        'aria-expanded="false">Contents</button>'
        if toc_toggle else ""
    )
    return (
        f'<div class="scrollbar" id="scrollnav">'
        f"{home}"
        f'<span class="sb-label">{html_mod.escape(page_label)}</span>'
        f"{toggle}"
        f"{jumps}"
        f'<a class="sb-top" href="#top" aria-label="Back to top">↑ Top</a>'
        f"</div>"
    )


def render_page(
    title: str,
    course_line: str,
    heading: str,
    body: str,
    macros: Dict[str, str],
    solutions: bool = False,
    nav: str = "",
    scrollbar: str = "",
    theme: str = DEFAULT_THEME_CSS,
    custom_css: str = "",
    favicon: str = "",
    banner: str = "",
    due: str = "",
    toc: str = "",
) -> str:
    badge = '<div><span class="badge">Solutions</span></div>' if solutions else ""
    due_html = (
        f'<p class="due"><span class="due-label">Due</span>'
        f"{html_mod.escape(due)}</p>"
        if due else ""
    )
    nav_html = f'<nav class="site">{nav}</nav>' if nav else ""
    css_link = (
        f'<link rel="stylesheet" href="{custom_css}">' if custom_css else ""
    )
    icon_link = f'<link rel="icon" href="{favicon}">' if favicon else ""
    macros_json = json.dumps(macros, ensure_ascii=False)
    e = html_mod.escape
    header = (
        f'<header class="doc">\n'
        f'<p class="course">{course_line}</p>\n'
        f"<h1>{heading}</h1>\n"
        f"{due_html}\n"
        f"{badge}\n"
        f"</header>"
    )
    # With banner art the title header floats in a hero above the column.
    hero = f'<div class="hero">\n<img src="{banner}" alt="">\n{header}\n</div>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
{icon_link}
{THEME_HEAD_SCRIPT}
{katex_block(macros_json)}
<style>{theme}{CSS}{NAV_CSS}</style>
{css_link}
</head>
<body>
{THEME_TOGGLE_HTML}
{scrollbar}
{toc}
{hero if banner else ""}
<main>
{nav_html}
{"" if banner else header}
{body}
<footer class="doc">{FOOTER_HTML}</footer>
</main>
{SCROLLBAR_JS if scrollbar else ""}
{TOC_JS if toc else ""}
{THEME_TOGGLE_JS}
</body>
</html>
"""
