"""Quote bank: a personal collection of ``\\epigraph`` quotes.

One JSON file (``~/.hwgenie/quotes.json``) holds every quote plus a record
of where it has been used (course, semester, document).  The bank is served
as a page of the hwGenie app (``/quotes`` on the grader server): search,
filter by course/semester, copy the ``\\epigraph`` TeX, edit, and log new
uses.  ``hwgenie quotes import <repo>`` seeds it from a course repo's
existing sources.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

QUOTES_PATH = Path.home() / ".hwgenie" / "quotes.json"


class QuoteBankError(Exception):
    pass


# ------------------------------------------------------------------ store --

class QuoteBank:
    """The quotes file: a list of quote dicts, saved atomically.

    Quote shape::

        {"id": "q3f2a9c1d", "text": "<quote TeX>", "source": "<source TeX>",
         "uses": [{"course": "Math 261", "semester": "Fall 2025",
                   "doc": "ps03"}],
         "created": "2026-08-18T12:00:00", "modified": "..."}
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else QUOTES_PATH
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                raise QuoteBankError(f"cannot read {self.path}: {e}") from e
            self.quotes: list[dict] = data.get("quotes", [])
        else:
            self.quotes = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"quotes": self.quotes}, indent=2,
                                  ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def get(self, qid: str) -> dict:
        for q in self.quotes:
            if q["id"] == qid:
                return q
        raise QuoteBankError(f"unknown quote {qid!r}")

    def add(self, text: str, source: str) -> dict:
        text, source = text.strip(), source.strip()
        if not text:
            raise QuoteBankError("quote text is empty")
        now = datetime.now().isoformat(timespec="seconds")
        q = {"id": "q" + uuid.uuid4().hex[:8], "text": text,
             "source": source, "uses": [], "created": now, "modified": now}
        self.quotes.append(q)
        return q

    def update(self, qid: str, text: str, source: str) -> dict:
        q = self.get(qid)
        text = text.strip()
        if not text:
            raise QuoteBankError("quote text is empty")
        q["text"], q["source"] = text, source.strip()
        q["modified"] = datetime.now().isoformat(timespec="seconds")
        return q

    def delete(self, qid: str) -> None:
        self.quotes.remove(self.get(qid))

    def add_use(self, qid: str, course: str, semester: str,
                doc: str) -> dict:
        q = self.get(qid)
        use = {"course": course.strip(), "semester": semester.strip(),
               "doc": doc.strip()}
        if not any(use.values()):
            raise QuoteBankError("empty use")
        if use not in q["uses"]:
            q["uses"].append(use)
            q["modified"] = datetime.now().isoformat(timespec="seconds")
        return q

    def remove_use(self, qid: str, index: int) -> dict:
        q = self.get(qid)
        if not 0 <= index < len(q["uses"]):
            raise QuoteBankError(f"no use #{index}")
        del q["uses"][index]
        q["modified"] = datetime.now().isoformat(timespec="seconds")
        return q

    def find_text(self, text: str) -> dict | None:
        """The quote whose text matches ``text`` up to whitespace, if any."""
        key = _squash(text)
        for q in self.quotes:
            if _squash(q["text"]) == key:
                return q
        return None


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def epigraph_tex(q: dict) -> str:
    return "\\epigraph{%s}{%s}" % (q["text"], q["source"])


# ----------------------------------------------------------------- import --

def _strip_comment(line: str) -> str:
    return re.split(r"(?<!\\)%", line, maxsplit=1)[0]


def _balanced_group(text: str, start: int) -> tuple[str, int] | None:
    """The ``{...}`` group starting at the first non-space char at/after
    ``start``; returns (content, index-past-group) or None."""
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "{":
        return None
    depth, j = 0, i
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
                return text[i + 1:j], j + 1
        j += 1
    return None


def extract_epigraphs(tex: str) -> list[tuple[str, str]]:
    """All ``\\epigraph{text}{source}`` pairs in a document (comment-aware,
    groups may span lines)."""
    code = "\n".join(_strip_comment(ln) for ln in tex.splitlines())
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"\\epigraph\b", code):
        first = _balanced_group(code, m.end())
        if not first:
            continue
        second = _balanced_group(code, first[1])
        if not second:
            continue
        out.append((first[0].strip(), second[0].strip()))
    return out


def _course_values(repo: Path) -> tuple[str, str]:
    """(course, semester) from the repo's coursedata.tex (last match wins —
    the first can be a template placeholder)."""
    cd = repo / "coursedata.tex"
    course = semester = ""
    if cd.is_file():
        text = cd.read_text()
        for name in ("hwcourse", "hwsemester"):
            hits = re.findall(
                r"\\(?:re)?newcommand\{\\%s\}\{([^{}]*)\}" % name, text)
            if hits:
                if name == "hwcourse":
                    course = hits[-1].strip()
                else:
                    semester = hits[-1].strip()
    return course, semester


def import_course(repo: Path, bank: QuoteBank) -> dict:
    """Scan a course repo's ``source/`` for epigraphs; new quotes are added,
    known quotes (same text up to whitespace) just gain the use."""
    repo = Path(repo)
    src = repo / "source"
    if not src.is_dir():
        raise QuoteBankError(f"{repo} has no source/ folder — "
                             "point at a course repo root")
    course, semester = _course_values(repo)
    added, used, files = 0, 0, 0
    for tex in sorted(src.rglob("*.tex")):
        # authored sources only — not build/ outputs or _experiments/
        rel = tex.relative_to(src).parts
        if any(p == "build" or p.startswith("_") for p in rel):
            continue
        eps = extract_epigraphs(tex.read_text())
        if not eps:
            continue
        files += 1
        for text, source in eps:
            q = bank.find_text(text)
            if q is None:
                q = bank.add(text, source)
                added += 1
            before = len(q["uses"])
            bank.add_use(q["id"], course, semester, tex.stem)
            used += len(q["uses"]) - before
    bank.save()
    return {"course": course, "semester": semester, "files": files,
            "added": added, "uses": used}


# -------------------------------------------------------------------- api --

def api_get(path: str, bank_path: Path | None = None):
    """GET handlers under ``/quotes/api/``; returns (payload, status) or
    None if the path is not ours."""
    if path == "/quotes/api/list":
        try:
            bank = QuoteBank(bank_path)
        except QuoteBankError as e:
            return {"ok": False, "error": str(e)}, 500
        return {"ok": True, "quotes": bank.quotes}, 200
    return None


def api_post(path: str, data: dict, bank_path: Path | None = None):
    """POST handlers under ``/quotes/api/``; returns (payload, status) or
    None if the path is not ours."""
    actions = {"/quotes/api/save", "/quotes/api/use",
               "/quotes/api/deluse", "/quotes/api/delete"}
    if path not in actions:
        return None
    try:
        bank = QuoteBank(bank_path)
        if path == "/quotes/api/save":
            qid = str(data.get("id") or "")
            text = str(data.get("text") or "")
            source = str(data.get("source") or "")
            q = (bank.update(qid, text, source) if qid
                 else bank.add(text, source))
        elif path == "/quotes/api/use":
            q = bank.add_use(str(data.get("id") or ""),
                             str(data.get("course") or ""),
                             str(data.get("semester") or ""),
                             str(data.get("doc") or ""))
        elif path == "/quotes/api/deluse":
            q = bank.remove_use(str(data.get("id") or ""),
                                int(data.get("index", -1)))
        else:  # delete
            bank.delete(str(data.get("id") or ""))
            q = None
        bank.save()
        return {"ok": True, "quote": q}, 200
    except (QuoteBankError, ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400


# -------------------------------------------------------------------- cli --

def add_parser(sub) -> None:
    p = sub.add_parser("quotes", help="Manage the epigraph quote bank.")
    qsub = p.add_subparsers(dest="quotes_cmd", required=True)
    imp = qsub.add_parser(
        "import",
        help="Scan a course repo for \\epigraph{...}{...} and record the "
        "quotes + uses in the bank.")
    imp.add_argument("repo", help="Course repo root (has coursedata.tex "
                     "and source/).")
    imp.add_argument("--bank", type=Path, default=None,
                     help=f"Quotes file (default: {QUOTES_PATH}).")
    lst = qsub.add_parser("list", help="Print the quote bank.")
    lst.add_argument("--bank", type=Path, default=None,
                     help=f"Quotes file (default: {QUOTES_PATH}).")


def run_quotes(args) -> int:
    try:
        bank = QuoteBank(args.bank)
        if args.quotes_cmd == "import":
            res = import_course(Path(args.repo), bank)
            where = " ".join(x for x in (res["course"], res["semester"])
                             if x) or "(unknown course)"
            print(f"{where}: {res['files']} files scanned, "
                  f"{res['added']} new quotes, {res['uses']} new uses "
                  f"-> {bank.path}")
        else:
            if not bank.quotes:
                print(f"No quotes yet in {bank.path}")
            for q in bank.quotes:
                uses = "; ".join(
                    " ".join(x for x in (u["course"], u["semester"],
                                         u["doc"]) if x)
                    for u in q["uses"]) or "unused"
                text = _squash(q["text"])
                if len(text) > 70:
                    text = text[:67] + "..."
                print(f"[{q['id']}] {text}\n    -- {q['source']}\n"
                      f"    ({uses})")
        return 0
    except QuoteBankError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------- page --

def render_quotes() -> str:
    from .appicon import LAMP_SVG
    from .webstyle import BASE_CSS, nav_header
    return (PAGE.replace("__BASE__", BASE_CSS)
                .replace("__NAV__", nav_header("quotes"))
                .replace("__LAMP__", LAMP_SVG))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quote bank &mdash; hwGenie</title>
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<style>
__BASE__
  /* height:auto so the body box spans the full content: the sticky
     .appnav sticks for the whole scroll, not just the first viewport */
  html, body { height: auto; min-height: 100%; }
  body { overflow: auto; display: block; }
  main { max-width: 760px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }
  .sub { color: var(--muted); margin: 0 0 1.5rem; }
  #bar { display: flex; gap: .5rem; flex-wrap: wrap; margin: 0 0 1.1rem;
         position: sticky; top: var(--navh); background: var(--bg);
         padding: .6rem 0 .6rem; z-index: 5; }
  #bar input, #bar select {
    font: inherit; padding: .4rem .6rem; color: var(--fg);
    background: var(--card-bg); border: 1px solid var(--border);
  }
  #bar input { flex: 1; min-width: 10rem; }
  #bar input:focus, #bar select:focus {
    outline: 2px solid var(--accent); border-color: transparent; }
  #newbtn { padding: .4rem 1rem; cursor: pointer; border: none;
            background: var(--accent); color: var(--bg); }
  #count { color: var(--muted); font-size: .85rem; margin: 0 0 .8rem; }
  .qcard { background: var(--card-bg); padding: .9rem 1.1rem;
           margin: 0 0 .8rem; }
  .qtext { font-family: Charter, Georgia, serif; font-size: 1.02rem;
           line-height: 1.55; }
  .qsource { font-family: Charter, Georgia, serif; color: var(--muted);
             text-align: right; margin-top: .35rem; }
  .uses { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center;
          margin-top: .7rem; }
  .use { font-size: .78rem; background: var(--bg); padding: .15rem .55rem;
         color: var(--muted); white-space: nowrap; }
  .use b { color: var(--fg); font-weight: 600; }
  .use button { border: none; background: none; color: var(--alert);
                cursor: pointer; font-size: .8rem; padding: 0 0 0 .3rem; }
  .acts { display: flex; gap: .3rem; margin-top: .55rem; align-items: center; }
  .copied { color: var(--sol-accent); font-size: .8rem; }
  form.qedit label, #newform label {
    display: block; margin: .6rem 0 .2rem; font-size: .85rem;
    color: var(--muted); }
  textarea, .miniuse input, .qedit input[type=text],
  #newform input[type=text] {
    width: 100%; font: inherit; padding: .45rem .6rem; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border);
  }
  textarea { font-family: ui-monospace, Menlo, monospace; font-size: .85rem;
             min-height: 5.2rem; resize: vertical; }
  textarea:focus, .miniuse input:focus, .qedit input:focus,
  #newform input:focus {
    outline: 2px solid var(--accent); border-color: transparent; }
  .miniuse { display: flex; gap: .4rem; margin-top: .5rem; }
  .miniuse input { background: var(--bg); }
  .btnrow { display: flex; gap: .4rem; margin-top: .7rem; }
  button.solid { padding: .35rem 1rem; cursor: pointer; border: none;
                 background: var(--accent); color: var(--bg); }
  button.danger { color: var(--alert); }
  #newform { background: var(--card-bg); padding: .9rem 1.1rem;
             margin: 0 0 1rem; display: none; }
  #err { color: var(--alert); margin: .6rem 0; display: none; }
  .none { color: var(--muted); font-style: italic; }
</style>
</head>
<body>
__NAV__
<main>
  <p class="sub">Epigraphs for problem sets &mdash; search, copy the TeX,
  and log where each one has appeared.</p>

  <div id="bar">
    <input id="search" type="search" placeholder="Search quotes&hellip;"
           spellcheck="false">
    <select id="fcourse"><option value="">All courses</option></select>
    <select id="fsem"><option value="">All semesters</option></select>
    <button id="newbtn">New Quote</button>
  </div>

  <div id="newform">
    <label for="ntext">Quote TeX</label>
    <textarea id="ntext" spellcheck="false"
      placeholder="Use \\ for line breaks, \emph{...} for titles &mdash; whatever goes inside \epigraph{...}"></textarea>
    <label for="nsource">Source TeX</label>
    <input type="text" id="nsource" spellcheck="false"
      placeholder="From \emph{One Hundred Years of Solitude} by Gabriel Garc\'ia M\'arquez">
    <label>Where used (optional)</label>
    <div class="miniuse">
      <input id="ncourse" placeholder="Math 261">
      <input id="nsem" placeholder="Fall 2025">
      <input id="ndoc" placeholder="ps03">
    </div>
    <div class="btnrow">
      <button class="solid" id="nadd">Add Quote</button>
      <button class="ghost" id="ncancel">Cancel</button>
    </div>
  </div>

  <div id="err"></div>
  <p id="count"></p>
  <div id="list"><span class="none">loading&hellip;</span></div>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);
let QUOTES = [];          // server order (oldest first); rendered newest-first
let editing = null;       // quote id with the edit form open
let useOpen = null;       // quote id with the add-use form open

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* Light display rendering of quote TeX: real line breaks, italics/bold,
   curly quotes, accents. Copying always copies the exact TeX. */
const ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302",
                 '"': "\u0308", "~": "\u0303", "=": "\u0304",
                 ".": "\u0307"};
function detex(s) {
  // character-level TeX first (plain unicode results), then HTML-escape,
  // then the transforms that emit tags
  let t = s;
  t = t.replace(/\\([\'`^"~=.])\{\\?([a-zA-Z])\}/g,
    (m, a, c) => (c + ACCENTS[a]).normalize("NFC"));
  t = t.replace(/\\([\'`^"~=.])([a-zA-Z])/g,
    (m, a, c) => (c + ACCENTS[a]).normalize("NFC"));
  t = t.replace(/\{(\p{L}[\u0300-\u030f]?)\}/gu, "$1");
  t = t.replace(/``/g, "\u201c").replace(/''/g, "\u201d");
  t = t.replace(/`/g, "\u2018").replace(/'/g, "\u2019");   // TeX ` ' = \u2018 \u2019
  t = t.replace(/\\ldots(\{\})?/g, "\u2026");
  t = t.replace(/---/g, "\u2014").replace(/--/g, "\u2013");
  t = t.replace(/(^|[^\\])~/g, "$1\u00a0");
  t = t.replace(/\\([&%$#_])/g, "$1");
  let h = esc(t);
  h = h.replace(/\\emph\{([^{}]*)\}/g, "<i>$1</i>");
  h = h.replace(/\\textit\{([^{}]*)\}/g, "<i>$1</i>");
  h = h.replace(/\\textbf\{([^{}]*)\}/g, "<b>$1</b>");
  h = h.replace(/\\\\\*?(\[[^\]]*\])?/g, "<br>");
  return h;
}

function useLabel(u) {
  return [u.course, u.semester, u.doc].filter(Boolean);
}

function epigraphTex(q) {
  return "\\epigraph{" + q.text + "}{" + q.source + "}";
}

async function api(path, body) {
  $("#err").style.display = "none";
  const r = await fetch(path, {method: "POST", body: JSON.stringify(body)});
  const data = await r.json();
  if (!data.ok) {
    $("#err").textContent = data.error || "something went wrong";
    $("#err").style.display = "block";
    throw new Error(data.error);
  }
  return data;
}

async function copyTex(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const t = document.createElement("textarea");
    t.value = text; document.body.appendChild(t);
    t.select(); document.execCommand("copy"); t.remove();
  }
  const old = btn.textContent;
  btn.textContent = "Copied \u2713";
  btn.classList.add("copied");
  setTimeout(() => { btn.textContent = old;
                     btn.classList.remove("copied"); }, 1200);
}

function facets() {
  const courses = new Set(), sems = new Set();
  QUOTES.forEach(q => q.uses.forEach(u => {
    if (u.course) courses.add(u.course);
    if (u.semester) sems.add(u.semester);
  }));
  const semKey = s => {
    const m = s.match(/(Spring|Summer|Fall|Winter)\s+(\d{4})/i);
    if (!m) return [0, s];
    return [-(+m[2] * 10 + {spring: 1, summer: 2, fall: 3, winter: 4}[
      m[1].toLowerCase()]), ""];
  };
  const fill = (sel, vals) => {
    const cur = sel.value;
    sel.length = 1;
    vals.forEach(v => sel.add(new Option(v, v)));
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  };
  fill($("#fcourse"), [...courses].sort());
  fill($("#fsem"), [...sems].sort((a, b) =>
    semKey(a)[0] - semKey(b)[0] || a.localeCompare(b)));
}

function visible() {
  const needle = $("#search").value.trim().toLowerCase();
  const fc = $("#fcourse").value, fs = $("#fsem").value;
  return QUOTES.slice().reverse().filter(q => {
    if (fc || fs) {
      const hit = q.uses.some(u =>
        (!fc || u.course === fc) && (!fs || u.semester === fs));
      if (!hit) return false;
    }
    if (!needle) return true;
    // match both the raw TeX and the displayed text (so "marquez"
    // finds M\'arquez), accents folded away on both sides
    const plain = detex(q.text + " " + q.source)
      .replace(/<[^>]*>/g, " ");
    const fold = s => s.toLowerCase().normalize("NFD")
      .replace(/[̀-ͯ]/g, "");
    const hay = fold(q.text + " " + q.source + " " + plain + " " +
      q.uses.map(u => useLabel(u).join(" ")).join(" "));
    return needle.split(/\s+/).every(w => hay.includes(fold(w)));
  });
}

function usesHtml(q) {
  const chips = q.uses.map((u, i) =>
    `<span class="use"><b>${esc(useLabel(u).join(" \u00b7 ") || "?")}</b>` +
    (editing === q.id
      ? `<button data-deluse="${i}" title="remove">&times;</button>` : "") +
    `</span>`).join("");
  const addbtn = editing === q.id
    ? "" : '<button class="ghost" data-use="1">+ Use</button>';
  return `<div class="uses">${chips || '<span class="use">unused</span>'}
    ${addbtn}</div>`;
}

function cardHtml(q) {
  if (editing === q.id) {
    return `<div class="qcard" data-id="${q.id}"><form class="qedit"
      onsubmit="return false;">
      <label>Quote TeX</label>
      <textarea data-etext spellcheck="false">${esc(q.text)}</textarea>
      <label>Source TeX</label>
      <input type="text" data-esource spellcheck="false"
             value="${esc(q.source)}">
      ${usesHtml(q)}
      <div class="btnrow">
        <button class="solid" data-esave>Save</button>
        <button class="ghost" data-ecancel>Cancel</button>
        <span class="sp"></span>
        <button class="ghost danger" data-edel>Delete Quote</button>
      </div></form></div>`;
  }
  let mini = "";
  if (useOpen === q.id) {
    const last = q.uses[q.uses.length - 1] || {};
    const pc = $("#fcourse").value || last.course || "";
    const ps = $("#fsem").value || last.semester || "";
    mini = `<div class="miniuse">
      <input data-ucourse placeholder="Math 261" value="${esc(pc)}">
      <input data-usem placeholder="Fall 2025" value="${esc(ps)}">
      <input data-udoc placeholder="ps03">
      <button class="solid" data-uadd>Add</button></div>`;
  }
  return `<div class="qcard" data-id="${q.id}">
    <div class="qtext">${detex(q.text)}</div>
    <div class="qsource">&mdash; ${detex(q.source) || "?"}</div>
    ${usesHtml(q)}${mini}
    <div class="acts">
      <button class="ghost" data-copy>Copy TeX</button>
      <button class="ghost" data-edit>Edit</button>
    </div></div>`;
}

function render() {
  facets();
  const vis = visible();
  $("#count").textContent = QUOTES.length
    ? `${vis.length} of ${QUOTES.length} quotes`
    : "";
  $("#list").innerHTML = vis.map(cardHtml).join("") ||
    (QUOTES.length
      ? '<span class="none">no quotes match</span>'
      : '<span class="none">no quotes yet &mdash; add one above, or run ' +
        '<code>hwgenie quotes import &lt;course-repo&gt;</code></span>');
}

$("#list").addEventListener("click", async e => {
  const card = e.target.closest(".qcard");
  if (!card) return;
  const q = QUOTES.find(x => x.id === card.dataset.id);
  const t = e.target;
  if (t.hasAttribute("data-copy")) copyTex(epigraphTex(q), t);
  else if (t.hasAttribute("data-edit")) { editing = q.id; render(); }
  else if (t.hasAttribute("data-ecancel")) { editing = null; render(); }
  else if (t.hasAttribute("data-use")) {
    useOpen = useOpen === q.id ? null : q.id; render();
    const el = document.querySelector(
      `[data-id="${q.id}"] [data-udoc]`);
    if (el) el.focus();
  }
  else if (t.hasAttribute("data-uadd")) {
    const g = s => card.querySelector(s).value;
    const d = await api("/quotes/api/use", {id: q.id,
      course: g("[data-ucourse]"), semester: g("[data-usem]"),
      doc: g("[data-udoc]")});
    Object.assign(q, d.quote); useOpen = null; render();
  }
  else if (t.hasAttribute("data-deluse")) {
    const d = await api("/quotes/api/deluse",
      {id: q.id, index: +t.dataset.deluse});
    Object.assign(q, d.quote); render();
  }
  else if (t.hasAttribute("data-esave")) {
    const d = await api("/quotes/api/save", {id: q.id,
      text: card.querySelector("[data-etext]").value,
      source: card.querySelector("[data-esource]").value});
    Object.assign(q, d.quote); editing = null; render();
  }
  else if (t.hasAttribute("data-edel")) {
    if (!confirm("Delete this quote (and its use history)?")) return;
    await api("/quotes/api/delete", {id: q.id});
    QUOTES = QUOTES.filter(x => x.id !== q.id);
    editing = null; render();
  }
});

$("#newbtn").addEventListener("click", () => {
  const f = $("#newform");
  f.style.display = f.style.display === "block" ? "none" : "block";
  if (f.style.display === "block") $("#ntext").focus();
});
$("#ncancel").addEventListener("click", () => {
  $("#newform").style.display = "none";
});
$("#nadd").addEventListener("click", async () => {
  const d = await api("/quotes/api/save",
    {text: $("#ntext").value, source: $("#nsource").value});
  let q = d.quote;
  const [c, s, doc] = [$("#ncourse").value.trim(),
                       $("#nsem").value.trim(), $("#ndoc").value.trim()];
  if (c || s || doc) {
    const d2 = await api("/quotes/api/use",
      {id: q.id, course: c, semester: s, doc: doc});
    q = d2.quote;
  }
  QUOTES.push(q);
  ["#ntext", "#nsource", "#ndoc"].forEach(x => $(x).value = "");
  $("#newform").style.display = "none";
  render();
});

["#search", "#fcourse", "#fsem"].forEach(s =>
  $(s).addEventListener("input", render));

(async function init() {
  const d = await (await fetch("/quotes/api/list")).json();
  QUOTES = d.quotes || [];
  render();
})();

setInterval(() => {
  fetch("/ping", {method: "POST", body: "{}"}).catch(() => {});
}, 2000);
addEventListener("pagehide", () => {
  try { navigator.sendBeacon("/bye", "{}"); } catch (e) {}
});
</script>
</body>
</html>
"""
