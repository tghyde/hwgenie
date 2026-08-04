"""Responsive HTML page shell for hwgenie outputs."""

from __future__ import annotations

import html as html_mod
import json
from typing import Dict, List

KATEX_VERSION = "0.16.21"

CSS = """
:root {
  --bg: #fdfdfb;
  --fg: #1e2126;
  --muted: #5b6270;
  --accent: #1a56b0;
  --alert: #b3223a;
  --border: #ddddd6;
  --card-bg: #ffffff;
  --sol-bg: #f2f7f1;
  --sol-accent: #2e6b3e;
  --code-bg: #f4f4ef;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181d;
    --fg: #e6e4de;
    --muted: #9aa1ad;
    --accent: #7aa7e8;
    --alert: #e87a90;
    --border: #33363e;
    --card-bg: #1d2026;
    --sol-bg: #1c2420;
    --sol-accent: #8cc79b;
    --code-bg: #22252c;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: Charter, "Bitstream Charter", Georgia, "Times New Roman", serif;
  font-size: 1.0625rem;
  line-height: 1.65;
}
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
  padding: .15rem .8rem;
  border-radius: 999px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .8rem;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--sol-accent);
  border: 1.5px solid var(--sol-accent);
}
p { margin: 0 0 .9em; }
hr.sep {
  border: none;
  border-top: 1px solid var(--border);
  width: 55%;
  margin: 2.2rem auto;
}
.center { text-align: center; margin: 1.2rem 0; }
.task { color: var(--accent); }
.alert { color: var(--alert); }

section.problem {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 10px;
  padding: 1.1rem 1.25rem .6rem;
  margin: 1.9rem 0;
}
.problem-title {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .95rem;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 .8rem;
}

details.solution {
  background: var(--sol-bg);
  border-radius: 10px;
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
.solution-body > :last-child::after { content: "\\2002\\220E"; }

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
  border-radius: 6px;
}
figure.fig figcaption {
  font-size: .9rem;
  color: var(--muted);
  margin-top: .4rem;
}

pre.code {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
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
  border-left: 3px solid var(--muted);
  padding: .15rem 0 .15rem 1rem;
  margin: 1.2rem 0;
}
.thm-head { font-weight: 700; }
.proof { margin: 1rem 0 1.2rem; }
.proof-label { font-style: italic; }
.proof > :last-child::after { content: "\\2002\\220E"; color: var(--muted); }
.xref { color: var(--accent); text-decoration: none; }
.xref:hover { text-decoration: underline; }
sup.fn a { color: var(--accent); text-decoration: none; font-weight: 600; }
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
"""


NAV_CSS = """
nav.site {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: .85rem;
  margin: 1.2rem 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: .3rem 1rem;
}
nav.site a { color: var(--accent); text-decoration: none; }
nav.site a:hover { text-decoration: underline; }
nav.site .sep-dot { color: var(--muted); }
a { color: var(--accent); }
"""


def render_page(
    title: str,
    course_line: str,
    heading: str,
    body: str,
    macros: Dict[str, str],
    solutions: bool = False,
    nav: str = "",
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
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/contrib/auto-render.min.js"></script>
<style>{CSS}{NAV_CSS}</style>
</head>
<body>
<main>
{nav_html}
<header class="doc">
<p class="course">{course_line}</p>
<h1>{heading}</h1>
{badge}
</header>
{body}
<footer class="doc">Generated by hwgenie</footer>
</main>
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
}});
</script>
</body>
</html>
"""
