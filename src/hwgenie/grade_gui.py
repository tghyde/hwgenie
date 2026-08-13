"""Local browser grading app for ``hwgenie grade --gui``.

Serves a single-page app on localhost with two views over a collected
grading folder:

* by-student — the submission PDF beside score/comment fields for every part;
* by-part — every student's answer to one part stacked vertically, rendered
  from their tex via the hwgenie HTML converter (KaTeX for math), so a whole
  part can be graded consistently in one pass.

Grades autosave to ``grades/<slug>.json`` on every edit (see grade.py for
the schema).  Inline feedback uses numbered anchored markers: a comment's
``anchor`` is an exact substring of the student's tex; markers render at the
anchor position and degrade to the numbered end-of-part list when the anchor
cannot be located in a view.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .grade import (
    GradeError,
    GradeStore,
    body_is_empty,
    extract_solution_bodies,
    infer_n_parts,
    load_groups,
    load_manifest,
    load_rubric,
    split_preamble,
)
from .htmlgen import HtmlConverter
from .htmltemplate import KATEX_VERSION
from .katexmacros import extract_macros


class GradingApp:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.manifest = load_manifest(self.folder)
        self.units = self.manifest["units"]
        self.by_slug = {u["slug"]: u for u in self.units}
        self.n_parts = infer_n_parts(self.manifest)
        self.rubric = load_rubric(self.folder, self.n_parts)
        self.groups = load_groups(self.folder)
        self.store = GradeStore(self.folder, self.rubric)
        self.lock = threading.Lock()
        self.shutdown = threading.Event()
        self._bodies: dict[str, list[str] | None] = {}
        self._preambles: dict[str, str] = {}
        self._parts: dict[tuple[str, int], dict] = {}

    # ------------------------------------------------------------- tex --

    def bodies(self, slug: str) -> list[str] | None:
        if slug not in self._bodies:
            path = self.folder / "submissions" / slug / "submission.tex"
            if path.is_file():
                text = path.read_text(errors="replace")
                self._bodies[slug] = extract_solution_bodies(text)
                self._preambles[slug] = split_preamble(text)
            else:
                self._bodies[slug] = None
        return self._bodies[slug]

    def part_payload(self, slug: str, n: int) -> dict:
        key = (slug, n)
        if key in self._parts:
            return self._parts[key]
        payload: dict = {"slug": slug, "part": n, "tex": None, "html": None,
                         "empty": False, "warnings": [], "macros": {}}
        bodies = self.bodies(slug)
        if bodies is not None and 1 <= n <= len(bodies):
            body = bodies[n - 1].strip("\n")
            preamble = self._preambles.get(slug, "")
            payload["tex"] = body
            payload["empty"] = body_is_empty(body)
            try:
                payload["macros"] = extract_macros(preamble)
            except Exception:
                pass
            try:
                conv = HtmlConverter(body, include_solutions=True,
                                     extra_preamble=preamble)
                payload["html"] = conv.convert()
                payload["warnings"] = conv.warnings
            except Exception as e:  # malformed student tex: raw fallback
                payload["warnings"] = [
                    f"HTML conversion failed ({e}); showing raw TeX."]
        self._parts[key] = payload
        return payload

    # ----------------------------------------------------------- state --

    def state_payload(self) -> dict:
        slugs = [u["slug"] for u in self.units]
        with self.lock:
            units = []
            for u in self.units:
                data = self.store.load(u["slug"])
                units.append({
                    "slug": u["slug"],
                    "tex": bool(u.get("tex")),
                    "tex_source": u.get("tex_source"),
                    "pdf": bool(u.get("pdf")),
                    "collaborators": u.get("collaborators"),
                    "anomalies": u.get("anomalies", []),
                    "parts_found": u.get("parts_found"),
                    "members": self.groups.get(u["slug"]),
                    "parts": data["parts"],
                })
            graded, total = self.store.progress(slugs)
        return {
            "folder": str(self.folder),
            "n_parts": self.n_parts,
            "rubric": [{"label": rp.label, "max": rp.max}
                       for rp in self.rubric],
            "groups": self.groups,
            "progress": [graded, total],
            "units": units,
        }

    def apply_grade(self, req: dict) -> dict:
        slug = req.get("slug")
        if slug not in self.by_slug:
            raise GradeError(f"unknown submission {slug!r}")
        fields = {k: req[k] for k in ("score", "comments") if k in req}
        with self.lock:
            data = self.store.update(slug, req.get("part"), fields)
            graded, total = self.store.progress(list(self.by_slug))
        return {"ok": True, "slug": slug, "parts": data["parts"],
                "progress": [graded, total]}

    def pdf_path(self, slug: str) -> Path | None:
        if slug not in self.by_slug:
            return None
        p = self.folder / "submissions" / slug / "submission.pdf"
        return p if p.is_file() else None


def make_handler(app: GradingApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence request logging
            pass

        def _send(self, body: bytes, ctype="text/html; charset=utf-8",
                  code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if ctype == "application/pdf":
                self.send_header("Content-Disposition",
                                 'inline; filename="submission.pdf"')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode("utf-8"),
                       "application/json", code)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(render_page().encode("utf-8"))
            elif url.path == "/api/state":
                self._json(app.state_payload())
            elif url.path == "/api/part":
                q = urllib.parse.parse_qs(url.query)
                slug = q.get("slug", [""])[0]
                try:
                    n = int(q.get("part", ["0"])[0])
                except ValueError:
                    n = 0
                if slug not in app.by_slug:
                    self._json({"error": f"unknown submission {slug!r}"}, 404)
                    return
                self._json(app.part_payload(slug, n))
            elif url.path.startswith("/pdf/"):
                slug = urllib.parse.unquote(url.path[len("/pdf/"):])
                pdf = app.pdf_path(slug)
                if pdf is None:
                    self._send(b"not found", code=404)
                    return
                self._send(pdf.read_bytes(), "application/pdf")
            else:
                self._send(b"not found", code=404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "bad json"}, 400)
                return
            if self.path == "/api/grade":
                try:
                    self._json(app.apply_grade(data))
                except GradeError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/quit":
                self._json({"ok": True})
                app.shutdown.set()
            else:
                self._send(b"not found", code=404)

    return Handler


def serve_app(folder: Path, port: int = 0, open_browser: bool = True) -> int:
    app = GradingApp(folder)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"hwgenie grading app: {url}")
    print("(Leave this window open; it closes when you click 'Done grading' "
          "in the browser, or press Ctrl-C.)")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        app.shutdown.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    print("Grading app closed.")
    return 0


# --------------------------------------------------------------- the page --

def render_page() -> str:
    return PAGE.replace("__KATEX__", KATEX_VERSION)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grading — hwgenie</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/contrib/auto-render.min.js"></script>
<style>
  :root {
    --bg: #faf9f6; --fg: #20242a; --muted: #5d646f; --accent: #24589f;
    --alert: #b3223a; --border: #dcdad0; --card-bg: #efeee8;
    --sol-accent: #2c6a3f; --code-bg: #f1f0ea; --hover-bg: #e2e8f3;
    --draft-bg: #f3ecf8; --draft-accent: #7b4ea3; --mark-bg: #ffd76e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15171c; --fg: #e7e5e0; --muted: #9aa1ad; --accent: #8db1ea;
      --alert: #e87a90; --border: #33363e; --card-bg: #1f222a;
      --sol-accent: #98cda5; --code-bg: #22252d; --hover-bg: #2b3242;
      --draft-bg: #2a2233; --draft-accent: #c9a6e8; --mark-bg: #8a6d1d;
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
    padding: .5rem 1rem; border-bottom: 1px solid var(--border);
    background: var(--card-bg); position: sticky; top: 0; z-index: 30;
  }
  header h1 { font-size: 1rem; margin: 0; white-space: nowrap; }
  .tabs { display: flex; gap: .25rem; }
  .tabs button {
    font: inherit; padding: .3rem .9rem; cursor: pointer;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--fg); border-radius: 6px;
  }
  .tabs button.active {
    background: var(--accent); color: var(--bg); border-color: var(--accent);
  }
  .pwrap { flex: 1; min-width: 140px; max-width: 340px; display: flex;
           align-items: center; gap: .6rem; }
  .pbar { flex: 1; height: 8px; background: var(--code-bg);
          border-radius: 4px; overflow: hidden; }
  .pfill { height: 100%; background: var(--sol-accent); width: 0;
           transition: width .3s; }
  .ptext { font-size: .8rem; color: var(--muted); white-space: nowrap; }
  #savedot { font-size: .75rem; color: var(--sol-accent); opacity: 0;
             transition: opacity .4s; }
  #saveerr { font-size: .75rem; color: var(--alert); display: none; }
  button.ghost {
    font: inherit; font-size: .85rem; padding: .25rem .7rem; cursor: pointer;
    border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--accent);
  }
  button.ghost:hover { background: var(--hover-bg); }

  #layout { flex: 1; display: flex; min-height: 0; }
  #sidebar {
    width: 230px; overflow-y: auto; border-right: 1px solid var(--border);
    padding: .4rem 0; flex-shrink: 0;
  }
  #sidebar .stu {
    display: flex; align-items: baseline; gap: .4rem;
    padding: .28rem .7rem; cursor: pointer; font-size: .86rem;
    border-left: 3px solid transparent; overflow: hidden;
  }
  #sidebar .stu:hover { background: var(--hover-bg); }
  #sidebar .stu.active { border-left-color: var(--accent);
                         background: var(--hover-bg); font-weight: 600; }
  #sidebar .stu .nm { flex: 1; white-space: nowrap; overflow: hidden;
                      text-overflow: ellipsis; }
  #sidebar .stu .ct { color: var(--muted); font-size: .75rem; }
  #sidebar .stu .ct.done { color: var(--sol-accent); font-weight: 600; }
  #main { flex: 1; overflow-y: auto; min-width: 0; padding: 1rem 1.2rem 4rem; }

  .badge {
    display: inline-block; font-size: .68rem; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase;
    padding: .1rem .45rem; border-radius: 4px; vertical-align: middle;
  }
  .badge.recon { background: var(--mark-bg); color: var(--fg); }
  .badge.notex { background: var(--alert); color: var(--bg); }
  .badge.grp { background: var(--accent); color: var(--bg); }
  .collab { font-size: .85rem; color: var(--muted); margin: .15rem 0 0; }
  .collab.real { color: var(--fg); }
  .collab.real b { color: var(--accent); }
  .anom { font-size: .8rem; color: var(--alert); margin: .15rem 0 0; }

  .stuhead h2 { margin: 0; font-size: 1.15rem; display: inline; }
  .stuhead { margin-bottom: .8rem; }
  .cols { display: flex; gap: 1.1rem; align-items: flex-start; }
  #pdfpane { position: sticky; top: .5rem; width: 46%; flex-shrink: 0; }
  #pdfpane iframe {
    width: 100%; height: calc(100vh - 8.5rem); border: 1px solid var(--border);
    border-radius: 8px; background: #fff;
  }
  #partspane { flex: 1; min-width: 0; }

  .part {
    background: var(--card-bg); border: 1px solid var(--border);
    border-left: 4px solid var(--border);
    border-radius: 8px; padding: .7rem .9rem; margin: 0 0 .9rem;
  }
  .part.graded { border-left-color: var(--sol-accent); }
  .part-head { display: flex; align-items: center; gap: .55rem;
               flex-wrap: wrap; }
  .plabel { font-weight: 700; font-size: .95rem; min-width: 2.6rem; }
  input.score {
    width: 4.2rem; font: inherit; font-size: .95rem; padding: .2rem .4rem;
    color: var(--fg); background: var(--bg);
    border: 1px solid var(--border); border-radius: 6px;
  }
  input.score:focus { outline: 2px solid var(--accent);
                      border-color: transparent; }
  .pmax { color: var(--muted); font-size: .9rem; }
  .flag-empty { font-size: .78rem; color: var(--alert); font-weight: 600; }
  .flag-warn { font-size: .78rem; color: var(--muted); }
  .part-head .sp { flex: 1; }
  .pcontent {
    margin-top: .6rem; font-family: Charter, Georgia, serif;
    font-size: .98rem; overflow-x: auto;
  }
  .pcontent p { margin: 0 0 .55em; }
  .pcontent .math-display { overflow-x: auto; padding: .15rem 0; }
  .pcontent pre.texsrc {
    font: .8rem/1.45 ui-monospace, Menlo, monospace; white-space: pre-wrap;
    background: var(--code-bg); border-radius: 6px; padding: .6rem .7rem;
    margin: 0;
  }
  .pcontent pre.code { background: var(--code-bg); border-radius: 6px;
                       padding: .5rem .6rem; overflow-x: auto; }
  .pcontent .thmblock, .pcontent .proof {
    border-left: 3px solid var(--border); padding-left: .7rem;
    margin: .6em 0;
  }
  .pcontent .thm-head, .pcontent .proof-label { font-weight: 700;
    font-family: system-ui, sans-serif; font-size: .85rem; margin: 0 0 .2em; }
  .pcontent details.solution > summary { display: none; }
  .pcontent table { border-collapse: collapse; }
  .pcontent td, .pcontent th { border: 1px solid var(--border);
                               padding: .2rem .55rem; }
  .nodata { color: var(--muted); font-style: italic; font-size: .9rem;
            margin-top: .5rem; }

  sup.cmark {
    display: inline-block; cursor: pointer; user-select: none;
    background: var(--mark-bg); color: var(--fg); font: 700 .72rem/1.35
    system-ui, sans-serif; border-radius: 50%; width: 1.35em; height: 1.35em;
    text-align: center; margin: 0 .1em; vertical-align: super;
  }
  .cpop {
    display: block; background: var(--bg); border: 1px solid var(--accent);
    border-radius: 6px; padding: .35rem .6rem; margin: .25rem 0;
    font: .84rem/1.45 system-ui, sans-serif;
  }
  .pcomments { margin-top: .55rem; font-size: .86rem; }
  .pcomments ol { margin: .2rem 0 .3rem; padding-left: 1.4rem; }
  .pcomments li { margin: .25rem 0; }
  .pcomments .crow { display: flex; gap: .4rem; align-items: flex-start; }
  .pcomments textarea {
    flex: 1; font: inherit; font-size: .86rem; padding: .25rem .45rem;
    color: var(--fg); background: var(--bg); resize: vertical;
    border: 1px solid var(--border); border-radius: 6px; min-height: 1.9rem;
  }
  .pcomments .anchor {
    display: block; font: .72rem/1.4 ui-monospace, Menlo, monospace;
    color: var(--muted); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; max-width: 34rem; margin-top: .15rem;
  }
  .pcomments .del { border: none; background: none; color: var(--alert);
                    cursor: pointer; font-size: .95rem; padding: 0 .2rem; }
  .addc { font-size: .8rem; }
  .addhint { font-size: .74rem; color: var(--muted); margin-left: .5rem; }

  .pdraft {
    margin-top: .6rem; background: var(--draft-bg);
    border: 1px dashed var(--draft-accent); border-radius: 8px;
    padding: .5rem .7rem; font-size: .85rem;
  }
  .pdraft .dhead { font-weight: 700; color: var(--draft-accent);
    text-transform: uppercase; font-size: .72rem; letter-spacing: .05em; }
  .pdraft ul { margin: .25rem 0; padding-left: 1.2rem; }
  .pdraft button { margin-left: .5rem; }

  #partnav { display: flex; align-items: center; gap: .6rem;
             margin-bottom: 1rem; }
  #partnav select { font: inherit; padding: .3rem .5rem; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; }
  .pcard { margin-bottom: 1.1rem; }
  .pcard .who { display: flex; align-items: baseline; gap: .5rem;
                flex-wrap: wrap; margin-bottom: .2rem; }
  .pcard .who .nm { font-weight: 700; }
  .pcard .who a { color: var(--accent); font-size: .8rem;
                  text-decoration: none; }
  .pcard .who a:hover { text-decoration: underline; }
</style>
</head>
<body>
<header>
  <h1>hwgenie grading</h1>
  <div class="tabs">
    <button id="tab-student" class="active">By student</button>
    <button id="tab-part">By part</button>
  </div>
  <div class="pwrap">
    <div class="pbar"><div class="pfill" id="pfill"></div></div>
    <span class="ptext" id="ptext"></span>
  </div>
  <span id="savedot">saved ✓</span>
  <span id="saveerr"></span>
  <button class="ghost" id="quit">Done grading</button>
</header>
<div id="layout">
  <nav id="sidebar"></nav>
  <div id="main"></div>
</div>

<script>
"use strict";
const $ = s => document.querySelector(s);
let S = null;                    // /api/state payload
const P = {};                    // part payload cache: "slug|n" -> payload
let view = "student";            // "student" | "part"
let curSlug = null, curPart = 1;
const timers = {};               // debounce timers per save key

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function api(path, body) {
  const opts = body === undefined ? {} :
    {method: "POST", body: JSON.stringify(body)};
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function unit(slug) { return S.units.find(u => u.slug === slug); }
function pdata(slug, n) { return unit(slug).parts[String(n)]; }
function rlabel(n) { return S.rubric[n - 1].label; }
function rmax(n) { return S.rubric[n - 1].max; }

// ------------------------------------------------------------- progress --

function setProgress(g, t) {
  S.progress = [g, t];
  $("#pfill").style.width = t ? (100 * g / t) + "%" : "0";
  $("#ptext").textContent = `${g} / ${t} parts graded`;
}

function gradedCount(u) {
  return Object.values(u.parts).filter(p => p.status === "graded").length;
}

// ---------------------------------------------------------------- saving --

function flashSaved(err) {
  if (err) {
    $("#saveerr").style.display = "inline";
    $("#saveerr").textContent = "save failed: " + err;
    return;
  }
  $("#saveerr").style.display = "none";
  const d = $("#savedot");
  d.style.opacity = 1;
  clearTimeout(d._t);
  d._t = setTimeout(() => { d.style.opacity = 0; }, 1200);
}

function queueSave(slug, n, fields) {
  const key = slug + "|" + n + "|" + Object.keys(fields).join();
  clearTimeout(timers[key]);
  timers[key] = setTimeout(async () => {
    try {
      const r = await api("/api/grade", {slug, part: n, ...fields});
      // The client model stays authoritative for in-flight edits; only the
      // derived status (and progress) come back from the server.
      pdata(slug, n).status = r.parts[String(n)].status;
      setProgress(...r.progress);
      refreshPartChrome(slug, n);
      refreshSidebarRow(slug);
      flashSaved();
    } catch (e) { flashSaved(e.message); }
  }, 400);
}

// ------------------------------------------------------------ part panel --

function partPanel(slug, n, opts) {
  const u = unit(slug);
  const p = pdata(slug, n);
  const el = document.createElement("div");
  el.className = "part" + (p.status === "graded" ? " graded" : "");
  el.dataset.slug = slug; el.dataset.part = n;
  const mx = rmax(n);
  el.innerHTML = `
    <div class="part-head">
      ${opts && opts.who ? "" : `<span class="plabel">${esc(rlabel(n))}</span>`}
      <input class="score" type="number" min="0" step="0.5"
             value="${p.score === null ? "" : p.score}"
             aria-label="score for ${esc(rlabel(n))}">
      <span class="pmax">/ ${mx === null ? "—" : mx}</span>
      <span class="flags"></span>
      <span class="sp"></span>
      <button class="ghost toggle-tex" style="display:none">TeX</button>
    </div>
    <div class="pcontent"></div>
    <div class="pcomments"></div>
    <div class="pdraft" style="display:none"></div>`;

  const scoreEl = el.querySelector(".score");
  scoreEl.addEventListener("input", () => {
    const v = scoreEl.value.trim();
    const val = v === "" ? null : Number(v);
    const pd = pdata(slug, n);
    pd.score = val;
    pd.status = val === null ? "ungraded" : "graded";
    queueSave(slug, n, {score: val});
  });
  scoreEl.addEventListener("keydown", ev => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      const all = [...document.querySelectorAll("input.score")];
      const next = all[all.indexOf(scoreEl) + 1];
      if (next) { next.focus(); next.select();
                  next.scrollIntoView({block: "center"}); }
    }
  });

  fillContent(el, slug, n);
  renderComments(el, slug, n);
  renderDraft(el, slug, n);
  return el;
}

async function fillContent(el, slug, n) {
  const u = unit(slug);
  const box = el.querySelector(".pcontent");
  if (!u.tex) {
    box.innerHTML = `<div class="nodata">No tex — grade from the PDF` +
      (u.pdf ? ` (<a href="/pdf/${encodeURIComponent(slug)}" target="_blank">open ↗</a>)` : "") + `.</div>`;
    return;
  }
  const key = slug + "|" + n;
  if (!P[key]) {
    box.innerHTML = `<div class="nodata">loading…</div>`;
    try { P[key] = await api(`/api/part?slug=${encodeURIComponent(slug)}&part=${n}`); }
    catch (e) { box.innerHTML = `<div class="nodata">failed: ${esc(e.message)}</div>`; return; }
  }
  const pay = P[key];
  const flags = el.querySelector(".flags");
  flags.innerHTML = "";
  if (pay.empty) {
    flags.innerHTML = `<span class="flag-empty">⚠ empty box — check the
      full PDF (some students write outside the boxes)</span>`;
  } else if (pay.warnings.length) {
    flags.innerHTML = `<span class="flag-warn"
      title="${esc(pay.warnings.join("\n"))}">⚑ ${pay.warnings.length} render
      warning${pay.warnings.length > 1 ? "s" : ""}</span>`;
  }
  const toggle = el.querySelector(".toggle-tex");
  if (pay.tex !== null && pay.html !== null) {
    toggle.style.display = "";
    toggle.addEventListener("click", () => {
      el._mode = el._mode === "tex" ? "html" : "tex";
      toggle.textContent = el._mode === "tex" ? "Rendered" : "TeX";
      paintContent(el, slug, n);
    });
  }
  el._mode = pay.html === null ? "tex" : "html";
  paintContent(el, slug, n);
}

function paintContent(el, slug, n) {
  const pay = P[slug + "|" + n];
  const box = el.querySelector(".pcontent");
  if (!pay || pay.tex === null) return;
  const comments = pdata(slug, n).comments;
  if (el._mode === "tex") {
    box.innerHTML = `<pre class="texsrc">${texWithMarkers(pay.tex, comments)}</pre>`;
  } else {
    box.innerHTML = pay.html || `<div class="nodata">(empty)</div>`;
    if (window.renderMathInElement) {
      try {
        renderMathInElement(box, {
          macros: pay.macros, throwOnError: false, strict: false,
          delimiters: [
            {left: "$$", right: "$$", display: true},
            {left: "\\[", right: "\\]", display: true},
            {left: "$", right: "$", display: false},
            {left: "\\(", right: "\\)", display: false},
          ]});
      } catch (e) {}
    }
    placeMarkersRendered(box, comments);
  }
  box.querySelectorAll("sup.cmark").forEach(m => {
    m.addEventListener("click", () => togglePopover(m, comments));
  });
}

// Markers in the TeX view: anchors are exact substrings, so they always
// land unless the anchor is stale.
function texWithMarkers(tex, comments) {
  const inserts = [];   // [pos, commentIndex]
  comments.forEach((c, i) => {
    if (!c.anchor) return;
    const at = tex.indexOf(c.anchor);
    if (at !== -1) inserts.push([at + c.anchor.length, i]);
  });
  inserts.sort((a, b) => a[0] - b[0]);
  let out = "", prev = 0;
  for (const [pos, i] of inserts) {
    out += esc(tex.slice(prev, pos)) +
      `<sup class="cmark" data-ci="${i}">${i + 1}</sup>`;
    prev = pos;
  }
  return out + esc(tex.slice(prev));
}

// Markers in the rendered view: best-effort text search over text nodes
// (skipping KaTeX output); unmatched anchors stay in the list below.
function placeMarkersRendered(box, comments) {
  comments.forEach((c, i) => {
    if (!c.anchor) return;
    const needle = c.anchor.replace(/\s+/g, " ").trim();
    if (!needle) return;
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
      acceptNode: node =>
        node.parentElement && node.parentElement.closest(".katex, .cmark, .cpop")
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    let node;
    while ((node = walker.nextNode())) {
      const hay = node.textContent.replace(/\s+/g, " ");
      const at = hay.indexOf(needle);
      if (at === -1) continue;
      // map the normalized end offset back to a raw offset
      let raw = 0, norm = 0, target = at + needle.length;
      const text = node.textContent;
      while (raw < text.length && norm < target) {
        if (/\s/.test(text[raw])) {
          while (raw < text.length && /\s/.test(text[raw])) raw++;
          norm++;
        } else { raw++; norm++; }
      }
      const rest = node.splitText(raw);
      const mark = document.createElement("sup");
      mark.className = "cmark"; mark.dataset.ci = i;
      mark.textContent = i + 1;
      node.parentNode.insertBefore(mark, rest);
      return;
    }
  });
}

function togglePopover(mark, comments) {
  const open = mark.nextElementSibling;
  if (open && open.classList.contains("cpop")) { open.remove(); return; }
  const c = comments[Number(mark.dataset.ci)];
  if (!c) return;
  const pop = document.createElement("span");
  pop.className = "cpop";
  pop.textContent = (Number(mark.dataset.ci) + 1) + ". " + c.text;
  mark.after(pop);
}

// ---------------------------------------------------------------- comments --

function renderComments(el, slug, n) {
  const box = el.querySelector(".pcomments");
  const comments = pdata(slug, n).comments;
  const u = unit(slug);
  let html = "";
  if (comments.length) {
    html += "<ol>" + comments.map((c, i) => `
      <li><div class="crow">
        <textarea data-ci="${i}" rows="1"
          placeholder="comment text">${esc(c.text)}</textarea>
        <button class="del" data-ci="${i}" title="delete comment">✕</button>
      </div>${c.anchor ? `<span class="anchor" title="${esc(c.anchor)}">⚓ ${esc(c.anchor)}</span>` : ""}
      </li>`).join("") + "</ol>";
  }
  html += `<button class="ghost addc">+ comment</button>` +
    (u.tex ? `<span class="addhint">select text in the TeX view first to
      anchor it</span>` : "");
  box.innerHTML = html;

  // Handlers read the model fresh at event time (never a captured array):
  // a completed autosave must not strand them on stale objects.
  box.querySelectorAll("textarea").forEach(t => {
    t.addEventListener("input", () => {
      const cs = pdata(slug, n).comments;
      cs[Number(t.dataset.ci)].text = t.value;
      queueSave(slug, n, {comments: cs});
    });
  });
  box.querySelectorAll(".del").forEach(b => {
    b.addEventListener("click", () => {
      const cs = pdata(slug, n).comments;
      cs.splice(Number(b.dataset.ci), 1);
      queueSave(slug, n, {comments: cs});
      renderComments(el, slug, n);
      paintContent(el, slug, n);
    });
  });
  box.querySelector(".addc").addEventListener("click", () => {
    let anchor = null;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const range = sel.getRangeAt(0);
      const pre = el.querySelector("pre.texsrc");
      if (pre && pre.contains(range.commonAncestorContainer)) {
        // range.toString(), not sel.toString(): the latter is empty when
        // the document lacks focus (e.g. right after a toolbar click).
        const text = range.toString();
        const pay = P[slug + "|" + n];
        if (pay && pay.tex && pay.tex.includes(text)) anchor = text;
      }
      sel.removeAllRanges();
    }
    const cs = pdata(slug, n).comments;
    cs.push({anchor, text: ""});
    queueSave(slug, n, {comments: cs});
    renderComments(el, slug, n);
    paintContent(el, slug, n);
    const areas = el.querySelectorAll(".pcomments textarea");
    if (areas.length) areas[areas.length - 1].focus();
  });
}

// --------------------------------------------------------------- ai draft --

function renderDraft(el, slug, n) {
  const box = el.querySelector(".pdraft");
  const p = pdata(slug, n);
  const d = p.ai_draft;
  if (!d) { box.style.display = "none"; return; }
  box.style.display = "";
  const mx = rmax(n);
  let html = `<div class="dhead">AI draft — review before use</div>`;
  if (d.suggested_score !== undefined && d.suggested_score !== null) {
    html += `<div>Suggested score: <b>${d.suggested_score}</b>` +
      `${mx !== null ? " / " + mx : ""}
       <button class="ghost use-score">Use score</button></div>`;
  }
  if (d.feedback) {
    html += `<div style="margin-top:.25rem">${esc(d.feedback)}
      <button class="ghost use-fb">Add as comment</button></div>`;
  }
  if (d.issues && d.issues.length) {
    html += "<ul>" + d.issues.map(i => `<li>${esc(i)}</li>`).join("") + "</ul>";
  }
  box.innerHTML = html;
  const us = box.querySelector(".use-score");
  if (us) us.addEventListener("click", () => {
    const scoreEl = el.querySelector(".score");
    scoreEl.value = d.suggested_score;
    const pd = pdata(slug, n);
    pd.score = Number(d.suggested_score);
    pd.status = "graded";
    queueSave(slug, n, {score: pd.score});
  });
  const uf = box.querySelector(".use-fb");
  if (uf) uf.addEventListener("click", () => {
    const cs = pdata(slug, n).comments;
    cs.push({anchor: null, text: d.feedback});
    queueSave(slug, n, {comments: cs});
    renderComments(el, slug, n);
  });
}

// After a save: update graded highlight without rebuilding the panel.
function refreshPartChrome(slug, n) {
  document.querySelectorAll(
    `.part[data-slug="${CSS.escape(slug)}"][data-part="${n}"]`)
    .forEach(el => {
      el.classList.toggle("graded", pdata(slug, n).status === "graded");
    });
}

function refreshSidebarRow(slug) {
  const row = document.querySelector(
    `#sidebar .stu[data-slug="${CSS.escape(slug)}"] .ct`);
  if (!row) return;
  const done = gradedCount(unit(slug));
  row.textContent = `${done}/${S.n_parts}`;
  row.classList.toggle("done", done === S.n_parts);
}

// ------------------------------------------------------------ badges/head --

function badges(u) {
  let b = "";
  if (u.tex_source === "reconstructed")
    b += ` <span class="badge recon" title="This tex was reconstructed from
      the student's PDF — it is not their original source.">reconstructed tex</span>`;
  if (!u.tex) b += ` <span class="badge notex">no tex</span>`;
  if (u.members) b += ` <span class="badge grp">group of ${u.members.length}</span>`;
  return b;
}

function unitHeader(u, opts) {
  let h = `<div class="stuhead"><h2>${esc(u.slug)}</h2>${badges(u)}`;
  if (u.members)
    h += `<div class="collab">Members: ${u.members.map(esc).join(", ")}</div>`;
  if (u.collaborators !== null && u.collaborators !== undefined) {
    const real = u.collaborators && u.collaborators.toLowerCase() !== "none";
    h += `<div class="collab${real ? " real" : ""}">Collaborators &amp;
      sources: ${real ? "<b>" + esc(u.collaborators) + "</b>" : esc(u.collaborators)}</div>`;
  }
  if (u.anomalies && u.anomalies.length)
    h += `<div class="anom">⚠ ${u.anomalies.map(esc).join("; ")}</div>`;
  return h + "</div>";
}

// -------------------------------------------------------- by-student view --

function renderSidebarStudents() {
  const sb = $("#sidebar");
  sb.innerHTML = S.units.map(u => {
    const done = gradedCount(u);
    const star = u.tex_source === "reconstructed" ? "*" :
                 (!u.tex ? "†" : "");
    return `<div class="stu${u.slug === curSlug ? " active" : ""}"
      data-slug="${esc(u.slug)}">
      <span class="nm">${esc(u.slug)}${star}</span>
      <span class="ct${done === S.n_parts ? " done" : ""}">${done}/${S.n_parts}</span>
    </div>`;
  }).join("") + `<div style="padding:.5rem .7rem;font-size:.72rem;
    color:var(--muted)">* reconstructed tex &nbsp; † no tex</div>`;
  sb.querySelectorAll(".stu").forEach(row => {
    row.addEventListener("click", () => showStudent(row.dataset.slug));
  });
}

function showStudent(slug) {
  curSlug = slug;
  renderSidebarStudents();
  const u = unit(slug);
  const main = $("#main");
  main.innerHTML = unitHeader(u) + `
    <div class="cols">
      ${u.pdf ? `<div id="pdfpane">
        <iframe src="/pdf/${encodeURIComponent(slug)}" title="submission PDF"></iframe>
      </div>` : ""}
      <div id="partspane"></div>
    </div>`;
  const pane = $("#partspane");
  for (let n = 1; n <= S.n_parts; n++) pane.appendChild(partPanel(slug, n));
  main.scrollTop = 0;
}

// ----------------------------------------------------------- by-part view --

function renderSidebarParts() {
  const sb = $("#sidebar");
  sb.innerHTML = S.rubric.map((rp, i) => {
    const n = i + 1;
    const done = S.units.filter(
      u => u.parts[String(n)].status === "graded").length;
    return `<div class="stu${n === curPart ? " active" : ""}" data-n="${n}">
      <span class="nm">${esc(rp.label)}</span>
      <span class="ct${done === S.units.length ? " done" : ""}">${done}/${S.units.length}</span>
    </div>`;
  }).join("");
  sb.querySelectorAll(".stu").forEach(row => {
    row.addEventListener("click", () => showPart(Number(row.dataset.n)));
  });
}

function showPart(n) {
  curPart = n;
  renderSidebarParts();
  const main = $("#main");
  const mx = rmax(n);
  main.innerHTML = `
    <div id="partnav">
      <button class="ghost" id="pprev" ${n <= 1 ? "disabled" : ""}>←</button>
      <select id="psel">${S.rubric.map((rp, i) =>
        `<option value="${i + 1}"${i + 1 === n ? " selected" : ""}>
         ${esc(rp.label)}</option>`).join("")}</select>
      <button class="ghost" id="pnext"
        ${n >= S.n_parts ? "disabled" : ""}>→</button>
      <span class="ptext">${mx === null ? "" : "out of " + mx + " points"}
        — Enter moves down the list</span>
    </div>
    <div id="pcards"></div>`;
  $("#psel").addEventListener("change", e => showPart(Number(e.target.value)));
  $("#pprev").addEventListener("click", () => showPart(n - 1));
  $("#pnext").addEventListener("click", () => showPart(n + 1));
  const cards = $("#pcards");
  for (const u of S.units) {
    const card = document.createElement("div");
    card.className = "pcard";
    card.innerHTML = `<div class="who"><span class="nm">${esc(u.slug)}</span>
      ${badges(u)}
      ${u.pdf ? `<a href="/pdf/${encodeURIComponent(u.slug)}" target="_blank">PDF ↗</a>` : ""}
      </div>`;
    card.appendChild(partPanel(u.slug, n, {who: true}));
    cards.appendChild(card);
  }
  main.scrollTop = 0;
}

// ------------------------------------------------------------------ tabs --

function setView(v) {
  view = v;
  $("#tab-student").classList.toggle("active", v === "student");
  $("#tab-part").classList.toggle("active", v === "part");
  if (v === "student") showStudent(curSlug || S.units[0].slug);
  else showPart(curPart);
}

$("#tab-student").addEventListener("click", () => setView("student"));
$("#tab-part").addEventListener("click", () => setView("part"));

$("#quit").addEventListener("click", async () => {
  await api("/quit", {});
  document.body.innerHTML = "<main style='padding:3rem;font-family:system-ui'>" +
    "<h1>Grading app closed.</h1><p>You can close this tab.</p></main>";
});

// ------------------------------------------------------------------ init --

(async function init() {
  S = await api("/api/state");
  setProgress(...S.progress);
  document.title = `Grading — ${S.folder.split("/").pop()} — hwgenie`;
  setView("student");
})();
</script>
</body>
</html>
"""
