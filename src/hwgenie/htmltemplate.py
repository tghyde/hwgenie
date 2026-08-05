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
  padding: 1.15rem 1.35rem .65rem;
  margin: 1.9rem 0;
}
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
  background: var(--bg);
  padding: .9rem 1.1rem;
  margin: 1.2rem 0;
}
.thm-head { font-weight: 700; }
.proof { margin: 1rem 0 1.2rem; }
.proof-label { font-style: italic; }
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


def katex_block(macros_json: str) -> str:
    """KaTeX assets + auto-render + wide-display auto-scaling."""
    return f"""<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/contrib/auto-render.min.js"></script>
<script>
document.addEventListener("DOMContentLoaded", function() {{
  renderMathInElement(document.body, {{
    delimiters: [
      {{left: "$$", right: "$$", display: true}},
      {{left: "\\\\[", right: "\\\\]", display: true}},
      {{left: "$", right: "$", display: false}},
      {{left: "\\\\(", right: "\\\\)", display: false}}
    ],
    macros: {macros_json},
    throwOnError: false
  }});
  fitDisplays();
}});
function fitDisplays() {{
  document.querySelectorAll(".katex-display").forEach(function(d) {{
    d.style.fontSize = "";
    if (d.scrollWidth > d.clientWidth + 1) {{
      var scale = d.clientWidth / d.scrollWidth;
      if (scale >= 0.7) d.style.fontSize = (scale * 100).toFixed(1) + "%";
      // below 70% keep full size; the container scrolls horizontally
    }}
  }});
}}
var fitTimer = null;
window.addEventListener("resize", function() {{
  clearTimeout(fitTimer);
  fitTimer = setTimeout(fitDisplays, 150);
}});
window.addEventListener("load", fitDisplays);
if (document.fonts && document.fonts.ready) {{
  document.fonts.ready.then(fitDisplays);
}}
if (window.ResizeObserver) {{
  new ResizeObserver(function() {{
    clearTimeout(fitTimer);
    fitTimer = setTimeout(fitDisplays, 150);
  }}).observe(document.body);
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
.filebox:hover { background: var(--code-bg); }
.filebox svg { flex-shrink: 0; }
.assignment .filebox { background: var(--bg); }
.assignment .filebox:hover { background: var(--sol-bg); }

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
nav.site {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  margin: 1.2rem 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: .3rem 1rem;
}
nav.site .sep-dot { color: var(--muted); }
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


def scrollbar_html(
    home_href: str,
    home_label: str,
    page_label: str,
    jump_links: Optional[List[Tuple[str, str]]] = None,
) -> str:
    jumps = ""
    if jump_links:
        items = " ".join(
            f'<a href="#{html_mod.escape(anchor)}">{html_mod.escape(num)}</a>'
            for num, anchor in jump_links
        )
        jumps = f'<span class="sb-jumps">{items}</span>'
    return (
        f'<div class="scrollbar" id="scrollnav">'
        f'<a href="{home_href}">← {html_mod.escape(home_label)}</a>'
        f'<span class="sb-label">{html_mod.escape(page_label)}</span>'
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
) -> str:
    badge = '<div><span class="badge">Solutions</span></div>' if solutions else ""
    nav_html = f'<nav class="site">{nav}</nav>' if nav else ""
    macros_json = json.dumps(macros, ensure_ascii=False)
    e = html_mod.escape
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
{THEME_HEAD_SCRIPT}
{katex_block(macros_json)}
<style>{theme}{CSS}{NAV_CSS}</style>
</head>
<body>
{THEME_TOGGLE_HTML}
{scrollbar}
<main>
{nav_html}
<header class="doc">
<p class="course">{course_line}</p>
<h1>{heading}</h1>
{badge}
</header>
{body}
<footer class="doc">{FOOTER_HTML}</footer>
</main>
{SCROLLBAR_JS if scrollbar else ""}
{THEME_TOGGLE_JS}
</body>
</html>
"""
