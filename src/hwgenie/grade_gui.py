"""Local browser grading app ("hwGrader") for ``hwgenie grade --gui``.

Serves a single-page app on localhost.  Launched on a grading folder it
opens it directly; launched anywhere else it shows a picker that scans for
grading folders (or accepts a Moodle "Download all submissions" zip, which
it runs through ``hwgenie collect`` first).

Two views over the open assignment:

* by-student — every part's score/comment fields, with a collapsible PDF
  panel and a sticky jump-nav;
* by-part — every student's answer to one part stacked vertically, rendered
  from their tex via the hwgenie HTML converter (KaTeX for math), so a
  whole part can be graded consistently in one pass.

Grades autosave to ``grades/<slug>.json`` on every edit (see grade.py for
the schema).  Inline feedback uses numbered anchored markers: a comment's
``anchor`` is an exact substring of the student's tex; markers render at the
anchor position and degrade to the numbered end-of-part list when the anchor
cannot be located in a view.
"""

from __future__ import annotations

import errno
import json
import re
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .grade import (
    MANIFEST_NAME,
    SOLUTION_BEGIN,
    SOLUTION_END,
    GradeError,
    GradeStore,
    _strip_comment,
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
from .webstyle import BASE_CSS

RECENTS_PATH = Path.home() / ".hwgenie" / "grader.json"

# Web app manifest: lets Chrome install the page as a standalone
# "hwGenie" app (needs a FIXED port so the origin is stable — the
# hwGrader.app launcher uses 8461).
MANIFEST = {
    "name": "hwGenie",
    "short_name": "hwGenie",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#15171c",
    "theme_color": "#24589f",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


# One Apple Event per window ("URL of tabs of w" returns the whole list)
# — querying tabs individually makes Dock-click refocusing visibly slow.
_FOCUS_SCRIPT = """
tell application "Google Chrome"
  set found to false
  repeat with w in windows
    set urls to URL of tabs of w
    repeat with i from 1 to count of urls
      if item i of urls starts with "%s" then
        set active tab index of w to i
        set index of w to 1
        set found to true
        exit repeat
      end if
    end repeat
    if found then exit repeat
  end repeat
  if found then activate
  return found
end tell"""


def _open_ui(url: str) -> None:
    """Show the grading UI: focus an existing hwGenie tab in Chrome if
    one is open (so a Dock click never piles up duplicate tabs), else
    open a fresh one in the default browser."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", _FOCUS_SCRIPT % url.rstrip("/")],
            capture_output=True, timeout=8, text=True)
        if proc.returncode == 0 and "true" in proc.stdout:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    webbrowser.open(url)

# A PDF text run that is exactly a problem heading ("Problem 3." or the
# section-numbered "Problem 1.3."); the last number is the problem ordinal.
PROBLEM_RE = re.compile(r"^Problem\s+(\d+(?:\.\d+)*)[.:]?$")


def template_problem_blocks(text: str) -> list[dict]:
    """Problem statements from the assignment's submission-template tex.

    Returns [{"num", "tex", "boxes"}] where tex is the problem body with
    each solution box replaced by an ``HWGRADERBOX<n>`` token (n = the
    box's global ordinal, matching grading part numbers) and boxes lists
    the ordinals appearing in that problem.  Comment-aware, like
    extract_solution_bodies.
    """
    problems: list[dict] = []
    cur: list[str] | None = None
    boxes: list[int] = []
    box = 0
    in_sol = False
    for line in text.splitlines():
        code = _strip_comment(line)
        if in_sol:
            j = code.find(SOLUTION_END)
            if j == -1:
                continue
            in_sol = False
            rest = line[j + len(SOLUTION_END):]
            if cur is not None and rest.strip():
                cur.append(rest)
            continue
        i = code.find(SOLUTION_BEGIN)
        if i != -1:
            box += 1
            if cur is not None:
                cur.append(line[:i])
                cur.append(
                    rf"\begin{{solution}}HWGRADERBOX{box}\end{{solution}}")
                boxes.append(box)
            j = code.find(SOLUTION_END, i + len(SOLUTION_BEGIN))
            if j == -1:
                in_sol = True
            elif cur is not None and line[j + len(SOLUTION_END):].strip():
                cur.append(line[j + len(SOLUTION_END):])
            continue
        b = code.find(r"\begin{problem}")
        if b != -1:
            cur = [line[b + len(r"\begin{problem}"):]]
            boxes = []
            continue
        e = code.find(r"\end{problem}")
        if e != -1 and cur is not None:
            cur.append(line[:e])
            problems.append({"num": len(problems) + 1,
                             "tex": "\n".join(cur), "boxes": boxes})
            cur = None
            continue
        if cur is not None:
            cur.append(line)
    return problems


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
        self._bodies: dict[str, list[str] | None] = {}
        self._preambles: dict[str, str] = {}
        self._parts: dict[tuple[str, int], dict] = {}
        self._pdfmaps: dict[str, dict] = {}
        self._problems: dict | None = None
        self._tmpl_labels: dict = {}   # \label targets from the template
        self.export_state: dict = {"running": False, "error": None,
                                   "summary": None}

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
                # students cite theorems stated in the problem text (e.g.
                # "by Theorem 1.1"): seed the template's \label targets so
                # their \ref's resolve instead of rendering as ??
                conv.labels.update(self.template_labels())
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

    def problems_payload(self) -> dict:
        """The assignment's problem statements, rendered from the template
        recorded in the manifest, with solution boxes tokenized so the app
        can mark and highlight the current part.  Empty when the template
        is unknown or missing."""
        if self._problems is not None:
            return self._problems
        result: dict = {"problems": [], "macros": {}, "warnings": []}
        tmpl = (self.manifest.get("template") or {}).get("path")
        path = Path(tmpl) if tmpl else None
        if path is not None and not path.is_absolute():
            path = self.folder / path
        if path is not None and path.is_file():
            text = path.read_text(errors="replace")
            preamble = split_preamble(text)
            m = re.search(r"\\hwnumber\{(\d+)\}", text)
            section = m.group(1) if m else None
            try:
                result["macros"] = extract_macros(preamble)
            except Exception:
                pass
            for blk in template_problem_blocks(text):
                try:
                    conv = HtmlConverter(blk["tex"], include_solutions=True,
                                         extra_preamble=preamble,
                                         section=section)
                    html = conv.convert()
                    result["warnings"].extend(conv.warnings)
                    self._tmpl_labels.update(conv.labels)
                except Exception as e:
                    html = "<p>(could not render this problem)</p>"
                    result["warnings"].append(
                        f"problem {blk['num']}: {e}")
                result["problems"].append(
                    {"num": blk["num"], "boxes": blk["boxes"], "html": html})
        self._problems = result
        return result

    def template_labels(self) -> dict:
        self.problems_payload()   # cached; populates _tmpl_labels
        return self._tmpl_labels

    def pdf_map(self, slug: str) -> dict:
        """Where each part lives in the student's PDF: {"parts": {"3":
        {"page": 2, "top": 93}}}, top in PDF points from the page top.

        Sources, most to least precise — a wrong jump is worse than a
        coarse one, so each is used only when trustworthy:

        1. named destinations ``hwsol.N`` (a template can plant one per
           solution box; exact even off-template elsewhere);
        2. the n-th "Solution:" text run — but only when the count matches
           the rubric exactly, since one garbled run shifts every later
           box onto the wrong problem;
        3. "Problem k" headings, mapping each part to its problem via the
           leading integer of its rubric label.

        Empty map when nothing is trustworthy (no pypdf, image-only or
        free-form PDF); the client then opens the PDF without jumping.
        """
        if slug in self._pdfmaps:
            return self._pdfmaps[slug]
        result: dict = {"parts": {}}
        pdf = self.pdf_path(slug)
        if pdf is not None:
            try:
                result["parts"] = self._pdf_positions(pdf)
            except Exception:
                result["parts"] = {}
        self._pdfmaps[slug] = result
        return result

    def _pdf_positions(self, pdf: Path) -> dict:
        from pypdf import PdfReader

        reader = PdfReader(pdf)
        sol_hits: list[tuple[int, float, float]] = []
        prob_hits: list[tuple[int, int, float, float]] = []
        for i, page in enumerate(reader.pages):
            h = float(page.mediabox.height)

            def visit(text, cm, tm, fd, fs, _i=i, _h=h):
                t = text.strip()
                if t.startswith("Solution:"):
                    sol_hits.append((_i, float(tm[5]), _h))
                else:
                    m = PROBLEM_RE.match(t)
                    if m:
                        prob_hits.append((int(m.group(1).split(".")[-1]),
                                          _i, float(tm[5]), _h))

            page.extract_text(visitor_text=visit)

        def entry(page_i: int, y: float, h: float, back: int) -> dict:
            return {"page": page_i + 1, "top": max(0, round(h - y) - back)}

        dests: dict[int, dict] = {}
        try:
            for name, dest in reader.named_destinations.items():
                m = re.fullmatch(r"hwsol\.(\d+)", name)
                if m:
                    pg = reader.get_destination_page_number(dest)
                    h = float(reader.pages[pg].mediabox.height)
                    top = getattr(dest, "top", None)
                    y = float(top) if top is not None else h
                    dests[int(m.group(1))] = entry(pg, y, h, back=25)
        except Exception:
            dests = {}
        if dests:
            return {str(n): e for n, e in dests.items()}

        if len(sol_hits) == len(self.rubric):
            sol_hits.sort(key=lambda t: (t[0], -t[1]))
            return {str(n): entry(pg, y, h, back=60)
                    for n, (pg, y, h) in enumerate(sol_hits, start=1)}

        by_prob: dict[int, dict] = {}
        for num, pg, y, h in prob_hits:
            by_prob.setdefault(num, entry(pg, y, h, back=20))
        parts: dict = {}
        for n, rp in enumerate(self.rubric, start=1):
            m = re.match(r"(\d+)", rp.label)
            if m and int(m.group(1)) in by_prob:
                parts[str(n)] = by_prob[int(m.group(1))]
        return parts


# ------------------------------------------------------- picker / holder --

class AppHolder:
    """The server's mutable state: the open assignment (or none — picker
    mode) plus the folder-scan root and the recents file."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.current: GradingApp | None = None
        self.shutdown = threading.Event()
        self.last_ping: float | None = None   # for --auto-exit
        self.bye_at: float | None = None

    def alive(self) -> None:
        self.last_ping = time.monotonic()
        self.bye_at = None

    def recents(self) -> list[str]:
        try:
            data = json.loads(RECENTS_PATH.read_text())
            return [p for p in data.get("recents", [])
                    if (Path(p) / MANIFEST_NAME).is_file()]
        except Exception:
            return []

    def remember(self, path: Path) -> None:
        rec = [str(path)] + [p for p in self.recents() if p != str(path)]
        try:
            RECENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            RECENTS_PATH.write_text(
                json.dumps({"recents": rec[:8]}, indent=2) + "\n")
        except OSError:
            pass

    def scan(self) -> list[dict]:
        """Grading folders (containing manifest.json) up to 3 levels below
        the root."""
        found: list[dict] = []

        def walk(d: Path, depth: int) -> None:
            if len(found) >= 40:
                return
            mf = d / MANIFEST_NAME
            if mf.is_file():
                entry = {"path": str(d), "units": None, "created": ""}
                try:
                    m = json.loads(mf.read_text())
                    entry["units"] = len(m.get("units", []))
                    entry["created"] = (m.get("created") or "")[:10]
                except Exception:
                    pass
                found.append(entry)
                return  # a grading folder has no nested ones
            if depth >= 3:
                return
            try:
                subs = sorted(p for p in d.iterdir()
                              if p.is_dir() and not p.name.startswith("."))
            except OSError:
                return
            for p in subs:
                walk(p, depth + 1)

        walk(self.root, 0)
        return found

    def open_path(self, path: Path) -> GradingApp:
        """Open a grading folder, or collect a Moodle zip first."""
        path = Path(path).expanduser()
        if path.suffix.lower() == ".zip" and path.is_file():
            dest = path.with_name(path.stem + "-grading")
            if not (dest / MANIFEST_NAME).is_file():
                from .collect import collect
                collect(path, dest)
            path = dest
        app = GradingApp(path)
        self.current = app
        self.remember(path)
        return app


def course_roots(root: Path) -> list[Path]:
    """Where the Courses page looks for local course clones: the scan
    root and its parent (the launcher's root is grading-lab, and course
    repos are its siblings in the HWGenie folder)."""
    root = Path(root).resolve()
    return [root, root.parent] if root.parent != root else [root]


def make_handler(holder: AppHolder):
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

        def _app(self) -> GradingApp | None:
            app = holder.current
            if app is None:
                self._json({"error": "no assignment open"}, 409)
            return app

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            app = holder.current
            if url.path in ("/", "/index.html"):
                holder.alive()
                page = render_grader() if app else render_picker()
                self._send(page.encode("utf-8"))
            elif url.path == "/api/scan":
                self._json({"root": str(holder.root),
                            "folders": holder.scan(),
                            "recents": holder.recents()})
            elif url.path == "/api/state":
                if (app := self._app()):
                    self._json(app.state_payload())
            elif url.path == "/api/part":
                if not (app := self._app()):
                    return
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
            elif url.path == "/api/problems":
                if (app := self._app()):
                    self._json(app.problems_payload())
            elif url.path == "/api/export":
                if (app := self._app()):
                    self._json(app.export_state)
            elif url.path == "/api/pdfmap":
                if not (app := self._app()):
                    return
                q = urllib.parse.parse_qs(url.query)
                slug = q.get("slug", [""])[0]
                if slug not in app.by_slug:
                    self._json({"error": f"unknown submission {slug!r}"}, 404)
                    return
                self._json(app.pdf_map(slug))
            elif url.path == "/quotes":
                from .quotebank import render_quotes
                holder.alive()
                self._send(render_quotes().encode("utf-8"))
            elif url.path.startswith("/quotes/api/"):
                from .quotebank import api_get
                res = api_get(url.path)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif url.path == "/courses":
                from .course_admin import render_courses
                holder.alive()
                self._send(render_courses().encode("utf-8"))
            elif url.path.startswith("/courses/api/"):
                from .course_admin import api_get as courses_get
                res = courses_get(url.path)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif url.path == "/new-course":
                from .new_course_gui import render_wizard
                holder.alive()
                self._send(render_wizard(embedded=True).encode("utf-8"))
            elif url.path == "/new-course/status":
                from .new_course_gui import STATE as wizard_state
                self._json(wizard_state.snapshot())
            elif url.path == "/manifest.webmanifest":
                self._send(json.dumps(MANIFEST).encode("utf-8"),
                           "application/manifest+json")
            elif url.path in ("/icon-192.png", "/icon-512.png"):
                from .appicon import icon_png
                size = 192 if "192" in url.path else 512
                self._send(icon_png(size), "image/png")
            elif url.path.startswith("/pdf/"):
                if not (app := self._app()):
                    return
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
                if not (app := self._app()):
                    return
                try:
                    self._json(app.apply_grade(data))
                except GradeError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/open":
                from .collect import CollectError
                try:
                    app = holder.open_path(Path(str(data.get("path", ""))))
                    self._json({"ok": True, "folder": str(app.folder)})
                except (GradeError, CollectError, OSError) as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/close":
                holder.current = None
                self._json({"ok": True})
            elif self.path == "/api/export":
                if not (app := self._app()):
                    return
                if app.export_state["running"]:
                    self._json({"ok": False,
                                "error": "export already running"}, 409)
                    return
                pdf = bool(data.get("pdf", False))
                app.export_state = {"running": True, "error": None,
                                    "summary": None}

                def job():
                    from .feedback import ZIP_NAME, build_feedback
                    try:
                        res = build_feedback(app.folder, pdf=pdf, app=app)
                        app.export_state = {
                            "running": False, "error": None,
                            "summary": {
                                "exported": len(res.exported),
                                "skipped": len(res.skipped),
                                "pdf_failures": len(res.pdf_failures),
                                "out": str(res.out_dir),
                                "warnings": res.warnings,
                                "worksheet": (res.worksheet or {}).get(
                                    "filled"),
                            }}
                    except Exception as e:
                        app.export_state = {"running": False,
                                            "error": str(e), "summary": None}

                threading.Thread(target=job, daemon=True).start()
                self._json({"ok": True})
            elif self.path.startswith("/quotes/api/"):
                from .quotebank import api_post
                res = api_post(self.path, data)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif self.path.startswith("/courses/api/"):
                from .course_admin import api_post as courses_post
                res = courses_post(self.path, data, course_roots(holder.root))
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif self.path == "/new-course/create":
                from .new_course_gui import start_create
                self._json(start_create(data))
            elif self.path == "/ping":
                holder.alive()
                self._json({"ok": True})
            elif self.path == "/bye":
                holder.bye_at = time.monotonic()
                self._json({"ok": True})
            else:
                self._send(b"not found", code=404)

    return Handler


def _watchdog_should_exit(now: float, started: float,
                          last_ping: float | None,
                          bye_at: float | None,
                          bye_grace: float = 10.0,
                          ping_timeout: float = 180.0,
                          startup_timeout: float = 300.0) -> bool:
    """--auto-exit decision, one tick.

    A closed tab sends /bye and then goes silent: exit after a short
    grace (a reload also sends /bye, but its next ping cancels it).  The
    long ping timeout catches browsers that die without /bye — pings from
    background tabs are throttled to ~1/minute, hence the generous
    window.  The startup timeout covers a browser that never connected.
    """
    if bye_at is not None and (last_ping is None or last_ping <= bye_at):
        return now - bye_at > bye_grace
    if last_ping is not None:
        return now - last_ping > ping_timeout
    return now - started > startup_timeout


def serve_app(folder: Path | None, port: int = 0,
              open_browser: bool = True, auto_exit: bool = False) -> int:
    folder = Path(folder) if folder is not None else Path.cwd()
    holder = AppHolder(root=folder if folder.is_dir() else folder.parent)
    try:
        holder.open_path(folder)
    except GradeError:
        print(f"note: {folder} is not a grading folder — "
              "opening the assignment picker instead")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port),
                                     make_handler(holder))
    except OSError as e:
        if port and e.errno == errno.EADDRINUSE:
            # a second launch while one is running: just show that one
            url = f"http://127.0.0.1:{port}/"
            print(f"hwGenie is already running at {url}")
            if open_browser:
                _open_ui(url)
            return 0
        raise
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"hwGenie: {url}")
    if auto_exit:
        print("(Closes by itself when the browser tab does.)")
    else:
        print("(Leave this window open; press Ctrl-C to stop, or run "
              "with --auto-exit to stop when the browser tab closes.)")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if auto_exit:
        def watchdog():
            from .new_course_gui import STATE as wizard_state
            started = time.monotonic()
            while not holder.shutdown.is_set():
                time.sleep(2)
                if wizard_state.phase == "running":
                    continue   # never auto-exit mid course-creation
                if _watchdog_should_exit(time.monotonic(), started,
                                         holder.last_ping, holder.bye_at):
                    holder.shutdown.set()
        threading.Thread(target=watchdog, daemon=True).start()
    if open_browser:
        _open_ui(url)
    try:
        holder.shutdown.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    print("hwGenie closed.")
    return 0


# -------------------------------------------------------------- the pages --

def render_grader() -> str:
    from .appicon import LAMP_SVG
    return GRADER_PAGE.replace("__KATEX__", KATEX_VERSION) \
                      .replace("__LAMP__", LAMP_SVG)


def render_picker() -> str:
    from .appicon import LAMP_SVG
    from .webstyle import nav_header
    return (PICKER_PAGE.replace("__NAV__", nav_header("grading"))
                       .replace("__LAMP__", LAMP_SVG))




GRADER_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hwGenie</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/contrib/auto-render.min.js"></script>
<style>
__BASE__
  header {
    display: flex; align-items: center; gap: .8rem; flex-wrap: wrap;
    padding: .5rem 1rem; background: var(--bar-bg);
    position: sticky; top: 0; z-index: 30;
    box-shadow: 0 1px 6px rgba(0,0,0,.15);
  }
  header h1 { font-size: 1rem; margin: 0; white-space: nowrap; }
  /* logo principle: lamp bottom sits ON the text baseline, lamp height
     matches the text size (inline svg + vertical-align: baseline) */
  .lamp { height: .72em;  /* cap height: lamp tip = top of G */ width: auto; color: var(--accent);
          vertical-align: baseline; margin-left: .15rem; }
  #switch { font-size: .8rem; padding: .15rem .5rem; }
  .tabs { display: flex; gap: .25rem; }
  .tabs button {
    padding: .3rem .9rem; cursor: pointer; border: none;
    background: transparent; color: var(--fg);
  }
  .tabs button:hover { background: var(--hover-bg); }
  .tabs button.active { background: var(--accent); color: var(--bg); }
  .pwrap { flex: 1; min-width: 120px; max-width: 320px; display: flex;
           align-items: center; gap: .6rem; }
  .pbar { flex: 1; height: 8px; background: var(--code-bg); overflow: hidden; }
  .pfill { height: 100%; background: var(--sol-accent); width: 0;
           transition: width .3s; }
  .ptext { font-size: .8rem; color: var(--muted); white-space: nowrap; }
  #saveerr { font-size: .75rem; color: var(--alert); display: none; }
  #notice {
    position: fixed; right: 1rem; bottom: 1rem; z-index: 50;
    max-width: 26rem; background: var(--bar-bg); color: var(--fg);
    padding: .7rem 1rem; font-size: .85rem; cursor: pointer;
    box-shadow: 0 4px 16px rgba(0,0,0,.3);
  }
  #savestat { display: inline-flex; align-items: center; min-width: 2rem;
              justify-content: center; }
  #savestat .ok { color: var(--sol-accent); font-weight: 700; }
  #savestat .dot {
    width: 5px; height: 5px; background: var(--muted); margin: 0 1.5px;
    display: inline-block; animation: pulse 1s infinite ease-in-out;
  }
  #savestat .dot:nth-child(2) { animation-delay: .18s; }
  #savestat .dot:nth-child(3) { animation-delay: .36s; }
  @keyframes pulse { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }

  #layout { flex: 1; display: flex; min-height: 0; }
  #sidebar {
    width: 230px; overflow-y: auto; background: var(--card-bg);
    padding: .4rem 0; flex-shrink: 0; overscroll-behavior: contain;
  }
  #sidebar.collapsed { display: none; }
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
  #main { flex: 1; overflow-y: auto; min-width: 300px;
          padding: 0 1.2rem 4rem; overscroll-behavior: contain; }
  /* the sticky nav overlays the pane: the zero-height wrapper keeps it
     out of the flow, so showing it never shifts the content under a
     just-completed jump */
  #stunav-wrap {
    position: sticky; top: 0; height: 0; z-index: 20;
    margin: 0 -1.2rem;
  }
  #stunav {
    display: none; align-items: center; gap: .3rem; flex-wrap: wrap;
    background: var(--bar-bg); padding: .35rem 1.2rem;
    width: fit-content; max-width: 100%; margin: 0 auto;
    box-shadow: 0 2px 8px rgba(0,0,0,.18);
  }
  #stunav.show { display: flex; }
  #stunav .nm { font-weight: 700; font-size: .9rem; margin-right: .3rem; }
  #stunav .jump {
    font-size: .74rem; padding: .05rem .5rem; border: none;
    cursor: pointer; background: var(--bg); color: var(--accent);
  }
  #stunav .jump:hover, #stunav .jump.cur { background: var(--hover-bg); }

  #pdfpanel {
    display: none; flex-direction: column; width: 44%; min-width: 320px;
    flex-shrink: 0; background: var(--card-bg);
  }
  #pdfpanel.open { display: flex; }
  .pdfhead {
    display: flex; align-items: center; gap: .5rem; font-size: .85rem;
    padding: .3rem .6rem; background: var(--bar-bg);
  }
  .pdfhead .nm { font-weight: 600; white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; }
  .pdfhead a { color: var(--accent); text-decoration: none;
               padding: 0 .3rem; }
  #pdfframe { flex: 1; border: none; background: #fff; }
  /* cards hug the right edge so the mouse can live there */
  #partspane, #pcards { max-width: 62rem; margin-left: auto; }

  #stmtpanel {
    display: none; flex-direction: column; width: 30%;
    min-width: 280px; max-width: 34rem; flex-shrink: 0;
    background: var(--card-bg);
  }
  #stmtpanel.open { display: flex; }
  #stmtbody {
    flex: 1; overflow-y: auto; overscroll-behavior: contain;
    padding: .6rem .9rem 2rem;
    font-family: Charter, Georgia, serif; font-size: .92rem;
  }
  #stmtbody h3 {
    font-family: system-ui, sans-serif; font-size: .95rem;
    margin: .3rem 0 .6rem;
  }
  #stmtbody .thmblock, #stmtbody .proof {
    border-left: 3px solid var(--border); padding-left: .7rem;
    margin: .6em 0;
  }
  #stmtbody .thm-head { font-weight: 700;
    font-family: system-ui, sans-serif; font-size: .82rem; margin: 0 0 .2em; }
  #stmtbody p { margin: 0 0 .55em; }
  #stmtbody .math-display { overflow-x: auto; padding: .15rem 0; }
  #stmtbody pre.code { background: var(--code-bg); padding: .5rem .6rem;
                       overflow-x: auto; }
  .pbox {
    font: 600 .78rem/1.4 system-ui, sans-serif; color: var(--muted);
    background: var(--code-bg); padding: .15rem .55rem; margin: .5em 0;
  }
  .pbox.cur { color: var(--bg); background: var(--sol-accent); }
  /* z-index: 0 makes .pprob a stacking context — without it the
     negative-z card paints behind the panel's opaque background and is
     invisible */
  .pprob { position: relative; z-index: 0; }
  /* one continuous card behind the active part's statement region */
  .stmt-cardbg {
    position: absolute; left: -.35rem; right: -.35rem; z-index: -1;
    background: var(--hover-bg);
    background: color-mix(in srgb, var(--accent) 14%, var(--bg));
    border-left: 3px solid var(--sol-accent);
  }
  .task { color: var(--accent); font-weight: 600; }
  .alert { color: var(--alert); }
  /* inline math + trailing punctuation glue from the converter */
  .nw { white-space: nowrap; }

  .vdiv { width: 5px; flex-shrink: 0; cursor: col-resize; display: none; }
  .vdiv:hover { background: var(--hover-bg); }
  body.dragging { cursor: col-resize; user-select: none; }
  body.dragging #pdfframe { pointer-events: none; }
  #main.collapsed { display: none; }
  .panelbtns { display: flex; gap: .1rem; }
  .part.activecard {
    box-shadow: 0 0 0 1.5px
      color-mix(in srgb, var(--accent) 55%, transparent);
  }

  .badge {
    display: inline-block; font-size: .68rem; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase;
    padding: .1rem .45rem; vertical-align: middle;
  }
  .badge.recon { background: var(--mark-bg); color: var(--fg); }
  .badge.notex { background: var(--alert); color: var(--bg); }
  .badge.grp { background: var(--accent); color: var(--bg); }
  .collab { font-size: .85rem; color: var(--muted); margin: .15rem 0 0; }
  .collab.real { color: var(--fg); }
  .collab.real b { color: var(--accent); }
  .anom { font-size: .8rem; color: var(--alert); margin: .15rem 0 0; }

  .stuhead { margin: 1rem 0 .8rem; }
  .stuhead .sturow { display: flex; align-items: center; gap: .5rem;
                     flex-wrap: wrap; }
  .stuhead h2 { margin: 0; font-size: 1.15rem; }

  .part {
    background: var(--card-bg);
    border-left: 4px solid transparent;
    padding: .7rem .9rem; margin: 0 0 .9rem;
    scroll-margin-top: 3.4rem;
  }
  .part.graded { border-left-color: var(--sol-accent); }
  .part-head { display: flex; align-items: center; gap: .55rem;
               flex-wrap: wrap; }
  .part-head.who {
    margin: -.7rem -.9rem .6rem; padding: .3rem .9rem;
    background: var(--code-bg); font-size: .85rem;
  }
  .part-head.who .nm { font-weight: 700; }
  .part-head.who .collab { margin: 0; font-size: .78rem; }
  .plabel { font-weight: 700; font-size: .95rem; min-width: 2.6rem; }
  input.score {
    width: 4.2rem; font: inherit; font-size: .95rem; padding: .2rem .4rem;
    color: var(--fg); background: var(--bg);
    border: 1px solid var(--border);
  }
  input.score:focus { outline: 2px solid var(--accent);
                      border-color: transparent; }
  /* no spinners: nudging a typed grade while scrolling is too easy */
  input.score::-webkit-outer-spin-button,
  input.score::-webkit-inner-spin-button {
    -webkit-appearance: none; margin: 0;
  }
  input.score { -moz-appearance: textfield; appearance: textfield; }
  .pmax { color: var(--muted); font-size: .9rem; }
  .flag-empty { font-size: .78rem; color: var(--alert); font-weight: 600; }
  button.flag-warn {
    border: none; background: none; cursor: pointer; padding: 0;
    font-size: .78rem; color: var(--muted);
    text-decoration: underline dotted;
  }
  .warnlist {
    font-size: .78rem; color: var(--muted); background: var(--code-bg);
    padding: .35rem .6rem; margin-top: .45rem;
  }
  .pcontent {
    margin-top: .6rem; font-family: Charter, Georgia, serif;
    font-size: .98rem; overflow-x: auto;
  }
  .pcontent p { margin: 0 0 .55em; }
  .pcontent .math-display { overflow-x: auto; padding: .15rem 0; }
  .pcontent pre.texsrc {
    font: .8rem/1.45 ui-monospace, Menlo, monospace; white-space: pre-wrap;
    background: var(--code-bg); padding: .6rem .7rem; margin: 0;
  }
  .pcontent pre.code { background: var(--code-bg); padding: .5rem .6rem;
                       overflow-x: auto; }
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
  .nodata a { color: var(--accent); }

  sup.cmark, .cpop .cmark {
    display: inline-block; cursor: pointer; user-select: none;
    background: var(--mark-bg); color: var(--fg); font: 700 .72rem/1.35
    system-ui, sans-serif; border-radius: 50%; width: 1.35em; height: 1.35em;
    text-align: center; margin: 0 .1em; vertical-align: super;
  }
  .cpop {
    display: flex; gap: .55rem; align-items: flex-start;
    background: var(--hover-bg);
    background: color-mix(in srgb, var(--sol-accent) 13%, var(--bg));
    border-left: 3px solid var(--sol-accent);
    padding: .45rem .6rem; margin: .3rem 0;
    font: .84rem/1.45 system-ui, sans-serif;
  }
  .cpop .cmark { flex-shrink: 0; vertical-align: baseline;
    margin-top: .1em; cursor: default; }
  .pcomments { margin-top: .55rem; font-size: .86rem; }
  .pcomments ol { margin: .2rem 0 .3rem; padding-left: 1.4rem; }
  .pcomments li { margin: .25rem 0; }
  .pcomments .crow { display: flex; gap: .4rem; align-items: flex-start; }
  .pcomments textarea {
    flex: 1; font: inherit; font-size: .86rem; padding: .25rem .45rem;
    color: var(--fg); background: var(--bg); resize: none;
    overflow: hidden;   /* autosized to fit the text */
    border: 1px solid var(--border); min-height: 1.9rem;
  }
  .pcomments .anchor {
    display: block; font: .72rem/1.4 ui-monospace, Menlo, monospace;
    color: var(--muted); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; max-width: 34rem; margin-top: .15rem;
  }
  .pcomments .del { border: none; background: none; color: var(--alert);
                    cursor: pointer; font-size: .95rem; padding: 0 .2rem; }
  .addc { font-size: .8rem; }
  .addrow { display: flex; align-items: center; gap: .5rem;
            margin-top: .4rem; }
  .addhint { font-size: .74rem; color: var(--muted); }

  .pdraft {
    margin-top: .6rem; background: var(--draft-bg);
    padding: .5rem .7rem; font-size: .85rem;
  }
  .pdraft .dhead { font-weight: 700; color: var(--draft-accent);
    text-transform: uppercase; font-size: .72rem; letter-spacing: .05em; }
  .pdraft ul { margin: .25rem 0; padding-left: 1.2rem; }
  .pdraft ol.dclist { margin: .3rem 0; padding-left: 1.2rem; }
  .pdraft ol.dclist li { margin: .25rem 0; }
  .pdraft .anchor {
    display: block; font: .72rem/1.4 ui-monospace, Menlo, monospace;
    color: var(--muted); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; max-width: 30rem;
  }
  .pdraft button { margin-left: .5rem; }

  #partnav { display: flex; align-items: center; gap: .6rem;
             margin: 1rem 0; }
  #partnav select { font: inherit; padding: .3rem .5rem; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border); }
</style>
</head>
<body>
<header>
  <h1>hwGenie __LAMP__</h1>
  <button class="ghost" id="switch" title="Grade a different assignment">
    ⇄ <span id="foldname"></span></button>
  <div class="tabs">
    <button id="tab-student" class="active">By Student</button>
    <button id="tab-part">By Part</button>
  </div>
  <div class="pwrap">
    <div class="pbar"><div class="pfill" id="pfill"></div></div>
    <span class="ptext" id="ptext"></span>
  </div>
  <div class="panelbtns">
    <button class="ghost" id="tg-pdf" title="Show/hide the PDF panel (q)">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
        stroke="currentColor" stroke-width="1.6" aria-hidden="true"
        style="vertical-align:-2px"><rect x="1.5" y="2" width="13"
        height="12"></rect><line x1="6.2" y1="2" x2="6.2" y2="14"></line>
      </svg> PDF</button>
    <button class="ghost" id="tg-stmt"
      title="Show/hide the problem statement (a)">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
        stroke="currentColor" stroke-width="1.6" aria-hidden="true"
        style="vertical-align:-2px"><rect x="2.5" y="1.5" width="11"
        height="13"></rect><line x1="5" y1="5" x2="11" y2="5"></line>
        <line x1="5" y1="8" x2="11" y2="8"></line>
        <line x1="5" y1="11" x2="9" y2="11"></line></svg> Problem</button>
    <button class="ghost" id="tg-main" title="Show/hide the solutions (e)">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
        stroke="currentColor" stroke-width="1.6" aria-hidden="true"
        style="vertical-align:-2px"><rect x="1.5" y="2" width="13"
        height="12"></rect><line x1="4.5" y1="6" x2="11.5" y2="6"></line>
        <line x1="4.5" y1="10" x2="11.5" y2="10"></line></svg>
      Solutions</button>
    <button class="ghost" id="tg-list"
      title="Show/hide the student/part list (d)">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
        stroke="currentColor" stroke-width="1.6" aria-hidden="true"
        style="vertical-align:-2px"><rect x="1.5" y="2" width="13"
        height="12"></rect><line x1="9.8" y1="2" x2="9.8" y2="14"></line>
      </svg> List</button>
  </div>
  <span class="sp"></span>
  <button class="ghost" id="export"
          title="Create the Moodle return files (feedback + zip + CSV)">
    Export</button>
  <span id="saveerr"></span>
  <span id="savestat" title="Save status"></span>
</header>
<div id="notice" style="display:none"></div>
<div id="layout">
  <aside id="pdfpanel">
    <div class="pdfhead">
      <span class="nm" id="pdfname"></span>
      <span class="sp"></span>
      <a id="pdfext" target="_blank" title="Open in a new tab">↗</a>
      <button class="ghost" id="pdfclose" title="Close">✕</button>
    </div>
    <iframe id="pdfframe" title="submission PDF"></iframe>
  </aside>
  <div class="vdiv" data-for="pdfpanel"></div>
  <aside id="stmtpanel">
    <div class="pdfhead">
      <span class="nm">Problem statement</span>
      <span class="sp"></span>
      <button class="ghost" id="stmtclose" title="Close">✕</button>
    </div>
    <div id="stmtbody" class="pcontent"></div>
  </aside>
  <div class="vdiv" data-for="stmtpanel"></div>
  <div id="main"></div>
  <div class="vdiv" data-for="sidebar" data-side="right"></div>
  <nav id="sidebar"></nav>
</div>

<script>
"use strict";
const $ = s => document.querySelector(s);
let S = null;                    // /api/state payload
const P = {};                    // part payload cache: "slug|n" -> payload
let view = "student";            // "student" | "part"
let curSlug = null, curPart = 1;
let pdfSlug = null;              // student shown in the PDF panel

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

function typeset(el, macros) {
  if (!window.renderMathInElement) return;
  try {
    renderMathInElement(el, {
      macros: macros || {}, throwOnError: false, strict: false,
      delimiters: [
        {left: "$$", right: "$$", display: true},
        {left: "\\[", right: "\\]", display: true},
        {left: "$", right: "$", display: false},
        {left: "\\(", right: "\\)", display: false},
      ]});
  } catch (e) {}
}

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

const pending = {};              // save key -> {timer, run}
let inflight = 0, saveError = null;

function saveState() {
  if (saveError) return "error";
  return (Object.keys(pending).length || inflight) ? "busy" : "clean";
}

function updateSaveStat() {
  const st = saveState();
  $("#savestat").innerHTML =
    st === "busy" ?
      '<span class="dot"></span><span class="dot"></span><span class="dot"></span>'
    : st === "error" ? '<span style="color:var(--alert)">✗</span>'
    : '<span class="ok">✓</span>';
  $("#saveerr").textContent = saveError || "";
  $("#saveerr").style.display = saveError ? "inline" : "none";
}

function queueSave(slug, n, fields) {
  const key = slug + "|" + n + "|" + Object.keys(fields).join();
  if (pending[key]) clearTimeout(pending[key].timer);
  const run = async () => {
    if (!pending[key]) return;    // already flushed
    delete pending[key];
    inflight++; updateSaveStat();
    try {
      const r = await api("/api/grade", {slug, part: n, ...fields});
      // The client model stays authoritative for in-flight edits; only
      // the derived status (and progress) come back from the server.
      pdata(slug, n).status = r.parts[String(n)].status;
      setProgress(...r.progress);
      refreshPartChrome(slug, n);
      refreshSidebarRow(slug);
      saveError = null;
    } catch (e) {
      saveError = "save failed: " + e.message;
    } finally {
      inflight--; updateSaveStat();
    }
  };
  pending[key] = {timer: setTimeout(run, 400), run};
  updateSaveStat();
}

async function settleSaves() {
  const runs = Object.values(pending).map(p => {
    clearTimeout(p.timer);
    return p.run();
  });
  await Promise.all(runs);
  while (inflight) await new Promise(r => setTimeout(r, 100));
}

// ------------------------------------------------------------- pdf panel --

const PDFMAP = {};   // slug -> {parts: {"1": {page, top}, ...}}
let pdfSeq = 1;      // cache-buster so a new #page fragment always applies

async function pdfFragment(slug, part) {
  if (!(slug in PDFMAP)) {
    try {
      PDFMAP[slug] = await api("/api/pdfmap?slug=" + encodeURIComponent(slug));
    } catch (e) { PDFMAP[slug] = {parts: {}}; }
  }
  const info = part && PDFMAP[slug].parts[String(part)];
  return info ? `#navpanes=0&page=${info.page}&view=FitH,${info.top}`
              : "#navpanes=0&view=FitH";
}

async function openPdfPanel(slug, part) {
  const u = slug && unit(slug);
  if (!u || !u.pdf) return;
  pdfSlug = slug;
  $("#pdfname").textContent = slug;
  $("#pdfext").href = "/pdf/" + encodeURIComponent(slug);
  const frag = await pdfFragment(slug, part);
  $("#pdfframe").src =
    "/pdf/" + encodeURIComponent(slug) + "?v=" + (pdfSeq++) + frag;
  $("#pdfpanel").classList.add("open");
  updatePanelBtns();
}

function closePdfPanel() {
  $("#pdfpanel").classList.remove("open");
  updatePanelBtns();
}

function togglePdfPanel(slug) {
  if ($("#pdfpanel").classList.contains("open") && pdfSlug === slug)
    closePdfPanel();
  else openPdfPanel(slug);
}

function toggleSidebar() {
  $("#sidebar").classList.toggle("collapsed");
  updatePanelBtns();
}

function updatePanelBtns() {
  const open = {
    pdfpanel: $("#pdfpanel").classList.contains("open"),
    stmtpanel: $("#stmtpanel").classList.contains("open"),
    main: !$("#main").classList.contains("collapsed"),
    sidebar: !$("#sidebar").classList.contains("collapsed"),
  };
  $("#tg-pdf").classList.toggle("active", open.pdfpanel);
  $("#tg-stmt").classList.toggle("active", open.stmtpanel);
  $("#tg-main").classList.toggle("active", open.main);
  $("#tg-list").classList.toggle("active", open.sidebar);
  document.querySelectorAll(".vdiv").forEach(d => {
    d.style.display = open[d.dataset.for] ? "block" : "none";
  });
}

// Scroll a pane so el is visible — only that pane. (scrollIntoView also
// scrolls ancestors, including the overflow:hidden body, which is exactly
// the "page lurches down and sticks" bug.)
function scrollPaneTo(container, el, mode) {
  const c = container.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  let top = container.scrollTop;
  if (mode === "center") {
    top += (r.top - c.top) - (c.height - r.height) / 2;
  } else if (mode === "start") {
    top += (r.top - c.top) - 12;
    // the sticky nav overlays the pane top once scrolled: leave room for
    // it so the jump target's top edge stays visible
    const nav = container.querySelector("#stunav");
    if (nav && top > 150) {
      const shown = nav.classList.contains("show");
      if (!shown) nav.classList.add("show");
      top -= nav.offsetHeight + 4;
      if (!shown) nav.classList.remove("show");
    }
  } else {  // "nearest"
    if (r.top >= c.top && r.bottom <= c.bottom) return;
    top += (r.top - c.top) - (c.height - r.height) / 2;
  }
  container.scrollTo({top: Math.max(0, top), behavior: "smooth"});
}

// belt and braces: the page itself must never scroll
addEventListener("scroll", () => {
  if (window.scrollX || window.scrollY) window.scrollTo(0, 0);
}, {passive: true});

// ------------------------------------------------------- panel resizing --

document.querySelectorAll(".vdiv").forEach(d => {
  d.addEventListener("mousedown", e => {
    e.preventDefault();
    const panel = document.getElementById(d.dataset.for);
    const fromRight = d.dataset.side === "right";
    const startX = e.clientX;
    const startW = panel.getBoundingClientRect().width;
    document.body.classList.add("dragging");
    const move = ev => {
      const w = startW + (fromRight ? startX - ev.clientX
                                    : ev.clientX - startX);
      panel.style.width =
        Math.max(180, Math.min(w, innerWidth * 0.7)) + "px";
      panel.style.minWidth = "0";
      panel.style.maxWidth = "none";
    };
    const up = () => {
      removeEventListener("mousemove", move);
      removeEventListener("mouseup", up);
      document.body.classList.remove("dragging");
      updateStmtPane();   // the statement card tracks element positions
    };
    addEventListener("mousemove", move);
    addEventListener("mouseup", up);
  });
});

// -------------------------------------------------------- statement pane --

let stmtData = null;     // /api/problems payload
let stmtBuilt = false;
let activePart = 1;      // the part currently being graded (see tracking)

async function ensureStmtPane() {
  if (!stmtData) {
    try { stmtData = await api("/api/problems"); }
    catch (e) { stmtData = {problems: [], macros: {}}; }
  }
  if (stmtBuilt) return;
  const body = $("#stmtbody");
  body.innerHTML = stmtData.problems.map(p =>
    `<div class="pprob" data-num="${p.num}" style="display:none">
       <h3>Problem ${p.num}</h3>${p.html}</div>`).join("") ||
    `<div class="nodata">No assignment template on record — re-run
     collect with --template to enable problem statements.</div>`;
  // the template's solution boxes became tokens; show them as part chips
  body.querySelectorAll("details.solution").forEach(d => {
    const m = d.textContent.match(/HWGRADERBOX(\d+)/);
    if (!m) return;
    const n = Number(m[1]);
    const div = document.createElement("div");
    div.className = "pbox"; div.dataset.part = n;
    div.textContent = "✎ " + (S.rubric[n - 1] ? S.rubric[n - 1].label : n);
    d.replaceWith(div);
  });
  typeset(body, stmtData.macros);
  stmtBuilt = true;
}

// Put one continuous card behind the statement region belonging to the
// active part: everything between the previous part chip (or the problem
// start) and this one.  A single absolutely-positioned backdrop, so the
// card has no seams between paragraphs.
function highlightStmt(body, n) {
  body.querySelectorAll(".stmt-cardbg").forEach(e => e.remove());
  body.querySelectorAll(".pbox.cur")
    .forEach(e => e.classList.remove("cur"));
  const marker = body.querySelector(`.pbox[data-part="${n}"]`);
  if (!marker) return;
  marker.classList.add("cur");
  const prob = marker.closest(".pprob");
  const all = [...prob.querySelectorAll("*")];
  const idx = all.indexOf(marker);
  let start = 0;
  for (let i = idx - 1; i >= 0; i--)
    if (all[i].classList.contains("pbox")) { start = i + 1; break; }
  const probR = prob.getBoundingClientRect();
  const mR = marker.getBoundingClientRect();
  let top = mR.top, bottom = mR.bottom;
  for (let i = start; i < idx; i++) {
    const r = all[i].getBoundingClientRect();
    if (r.height === 0) continue;
    top = Math.min(top, r.top);
    bottom = Math.max(bottom, r.bottom);
  }
  const bg = document.createElement("div");
  bg.className = "stmt-cardbg";
  bg.style.top = (top - probR.top - 8) + "px";
  bg.style.height = (bottom - top + 16) + "px";
  prob.prepend(bg);
  scrollPaneTo(body, marker, "center");
}

function updateStmtPane() {
  if (!$("#stmtpanel").classList.contains("open") || !stmtData) return;
  const prob = stmtData.problems.find(p => p.boxes.includes(activePart));
  $("#stmtbody").querySelectorAll(".pprob").forEach(d => {
    d.style.display =
      prob && Number(d.dataset.num) === prob.num ? "" : "none";
  });
  if (prob) highlightStmt($("#stmtbody"), activePart);
}

async function openStmtPanel() {
  $("#stmtpanel").classList.add("open");
  await ensureStmtPane();
  updateStmtPane();
  updatePanelBtns();
}

function closeStmtPanel() {
  $("#stmtpanel").classList.remove("open");
  updatePanelBtns();
}

function toggleStmtPanel() {
  if ($("#stmtpanel").classList.contains("open")) closeStmtPanel();
  else openStmtPanel();
}

function setActivePart(n) {
  const changed = n !== activePart;
  activePart = n;
  document.querySelectorAll("#stunav .jump").forEach(b =>
    b.classList.toggle("cur", Number(b.dataset.n) === n));
  if (changed) updateStmtPane();
}

// By-student: the active part follows the scroll position (topmost card
// in view) and any field the grader focuses.
let trackTick = false;
function trackActivePart() {
  if (view !== "student") return;
  const main = $("#main");
  const topEdge = main.getBoundingClientRect().top + 80;
  let cur = null;
  for (const p of main.querySelectorAll(".part")) {
    if (p.getBoundingClientRect().top <= topEdge) cur = p;
    else break;
  }
  if (cur) setActivePart(Number(cur.dataset.part));
}

$("#pdfclose").addEventListener("click", closePdfPanel);
$("#stmtclose").addEventListener("click", () => closeStmtPanel());
$("#tg-pdf").addEventListener("click", () => {
  if (!S) return;
  const first = S.units.find(u => u.pdf);
  togglePdfPanel((view === "student" && curSlug) || pdfSlug ||
                 curSlug || (first && first.slug));
});
$("#tg-stmt").addEventListener("click", toggleStmtPanel);
function toggleMain() {
  $("#main").classList.toggle("collapsed");
  updatePanelBtns();
}
$("#tg-main").addEventListener("click", toggleMain);
$("#tg-list").addEventListener("click", toggleSidebar);

// ------------------------------------------------------------ part panel --

function partPanel(slug, n, opts) {
  const u = unit(slug);
  const p = pdata(slug, n);
  const el = document.createElement("div");
  el.className = "part" + (p.status === "graded" ? " graded" : "");
  el.dataset.slug = slug; el.dataset.part = n;
  const mx = rmax(n);
  const collab = u.collaborators && u.collaborators.toLowerCase() !== "none"
    ? `<span class="collab real" title="Collaborators &amp; sources">
       collab: <b>${esc(u.collaborators)}</b></span>` : "";
  const headLeft = opts && opts.who
    ? `<span class="nm">${esc(slug)}</span>${badges(u)} ${collab}`
    : `<span class="plabel">${esc(rlabel(n))}</span>`;
  el.innerHTML = `
    <div class="part-head${opts && opts.who ? " who" : ""}">
      ${headLeft}
      <span class="flags"></span>
      <span class="sp"></span>
      <input class="score" type="number" min="0" step="0.5"
             value="${p.score === null ? "" : p.score}"
             aria-label="score for ${esc(rlabel(n))}">
      <span class="pmax">/ ${mx === null ? "—" : mx}</span>
      ${u.pdf ? `<button class="ghost pdfbtn">PDF</button>` : ""}
      <button class="ghost toggle-tex" style="display:none">TeX</button>
    </div>
    <div class="pcontent"></div>
    <div class="pdraft" style="display:none"></div>
    <div class="pcomments"></div>`;

  const pdfBtn = el.querySelector(".pdfbtn");
  if (pdfBtn) pdfBtn.addEventListener("click", () => openPdfPanel(slug, n));

  const scoreEl = el.querySelector(".score");
  // scrolling past a focused number input must never change its value
  scoreEl.addEventListener("wheel", () => scoreEl.blur());
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
      if (next) {
        next.focus(); next.select();
        scrollPaneTo($("#main"), next.closest(".part"), "center");
      }
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
      (u.pdf ? ` (<a href="#" class="openpdf">open panel</a>)` : "") +
      `.</div>`;
    const link = box.querySelector(".openpdf");
    if (link) link.addEventListener("click", ev => {
      ev.preventDefault();
      openPdfPanel(slug, n);
    });
    return;
  }
  const key = slug + "|" + n;
  if (!P[key]) {
    box.innerHTML = `<div class="nodata">loading…</div>`;
    try { P[key] = await api(`/api/part?slug=${encodeURIComponent(slug)}&part=${n}`); }
    catch (e) { box.innerHTML = `<div class="nodata">failed: ${esc(e.message)}</div>`; return; }
  }
  const pay = P[key];
  if (orderComments(slug, n)) {   // renumber to match rendered order
    queueSave(slug, n, {comments: pdata(slug, n).comments});
    renderComments(el, slug, n);
  }
  const flags = el.querySelector(".flags");
  flags.innerHTML = "";
  if (pay.empty) {
    flags.innerHTML = `<span class="flag-empty">⚠ empty box — check the
      full PDF (some students write outside the boxes)</span>`;
  } else if (pay.warnings.length) {
    flags.innerHTML = `<button class="flag-warn">⚑ ${pay.warnings.length}
      render warning${pay.warnings.length > 1 ? "s" : ""}</button>`;
    flags.querySelector(".flag-warn").addEventListener("click", () => {
      const open = el.querySelector(".warnlist");
      if (open) { open.remove(); return; }
      const wl = document.createElement("div");
      wl.className = "warnlist";
      wl.innerHTML = pay.warnings.map(w => `<div>• ${esc(w)}</div>`).join("");
      el.querySelector(".part-head").after(wl);
    });
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
    // Markers go in BEFORE KaTeX runs: at that point the DOM still holds
    // the raw math source that anchors quote, so anchors in or across
    // math environments land too.
    placeMarkersRendered(box, comments);
    typeset(box, pay.macros);
  }
  box.querySelectorAll("sup.cmark").forEach(m => {
    m.addEventListener("click", () => togglePopover(m, comments, pay.macros));
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

// Spans of inline math ($...$, \(...\), \[...\]) within one text node.
function mathSpans(t) {
  const spans = [];
  const re = /\\\(|\\\)|\\\[|\\\]|\$/g;
  let m, open = -1, closer = null;
  while ((m = re.exec(t))) {
    if (m[0] === "$" && m.index > 0 && t[m.index - 1] === "\\") continue;
    if (open === -1) {
      closer = {"$": "$", "\\(": "\\)", "\\[": "\\]"}[m[0]];
      if (closer) open = m.index;
    } else if (m[0] === closer) {
      spans.push([open, m.index + m[0].length]);
      open = -1;
    }
  }
  return spans;
}

// Markers in the rendered view. Runs BEFORE KaTeX renders, so text nodes
// still hold the raw math source the anchors quote — anchors inside or
// across math match too. A marker element can't live inside math, so an
// insertion point that falls within an inline span moves past its closing
// delimiter, and one inside a display block lands just after the block.
// Anchors that still don't match degrade to the numbered list below.
function placeMarkersRendered(box, comments) {
  const jobs = [];
  comments.forEach((c, i) => {
    const needle = c.anchor && c.anchor.replace(/\s+/g, " ").trim();
    if (needle) jobs.push({i, needle});
  });
  if (!jobs.length) return;

  const blockSel = "p,div,li,td,th,blockquote,pre,h1,h2,h3,figcaption";
  const nodes = [];
  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
    acceptNode: nd => nd.parentElement &&
      nd.parentElement.closest("sup.cmark, .cpop")
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
  let tn;
  while ((tn = walker.nextNode())) nodes.push(tn);

  // One whitespace-normalized string over all text nodes (block element
  // boundaries count as whitespace), with a char-by-char map back to
  // (node index, offset).
  let norm = "";
  const map = [];
  let lastSpace = true, lastBlock = null;
  nodes.forEach((node, ni) => {
    const blk = node.parentElement.closest(blockSel) || box;
    if (blk !== lastBlock && !lastSpace && norm) {
      norm += " "; map.push({ni, off: 0}); lastSpace = true;
    }
    lastBlock = blk;
    const t = node.textContent;
    for (let off = 0; off < t.length; off++) {
      if (/\s/.test(t[off])) {
        if (lastSpace) continue;
        norm += " "; map.push({ni, off}); lastSpace = true;
      } else {
        norm += t[off]; map.push({ni, off}); lastSpace = false;
      }
    }
  });

  const inserts = [];
  for (const j of jobs) {
    const at = norm.indexOf(j.needle);
    if (at === -1) continue;
    // needle is trimmed, so its last char is real text, never a virtual
    // boundary space
    let {ni, off} = map[at + j.needle.length - 1];
    off += 1;
    for (const [s, e] of mathSpans(nodes[ni].textContent))
      if (off > s && off < e) { off = e; break; }
    inserts.push({ni, off, i: j.i});
  }
  // insert back-to-front so earlier offsets stay valid; ties by index
  // DESCENDING so same-position markers end up left-to-right ascending
  inserts.sort((a, b) => b.ni - a.ni || b.off - a.off || b.i - a.i);
  for (const ins of inserts) {
    const node = nodes[ins.ni];
    const mark = document.createElement("sup");
    mark.className = "cmark"; mark.dataset.ci = ins.i;
    mark.textContent = ins.i + 1;
    const disp = node.parentElement.closest(".math-display");
    if (disp) { disp.after(mark); continue; }
    const rest = node.splitText(Math.min(ins.off, node.textContent.length));
    node.parentNode.insertBefore(mark, rest);
  }
}

function togglePopover(mark, comments, macros) {
  const open = mark.nextElementSibling;
  if (open && open.classList.contains("cpop")) { open.remove(); return; }
  const c = comments[Number(mark.dataset.ci)];
  if (!c) return;
  const pop = document.createElement("span");
  pop.className = "cpop";
  const badge = document.createElement("span");
  badge.className = "cmark";
  badge.textContent = Number(mark.dataset.ci) + 1;
  const body = document.createElement("div");
  body.textContent = c.text;
  pop.append(badge, body);
  mark.after(pop);
  typeset(body, macros);   // comments may contain $math$ of their own
}

// ---------------------------------------------------------------- comments --

// Keep the comment list in the order the markers appear in the student's
// work: anchored comments sort by their anchor's position in the tex,
// unanchored ones keep their relative order at the end.  Returns true if
// the order changed (caller re-renders and saves).
function orderComments(slug, n) {
  const pay = P[slug + "|" + n];
  const cs = pdata(slug, n).comments;
  if (!pay || pay.tex === null || cs.length < 2) return false;
  const keyed = cs.map((c, i) => {
    const at = c.anchor ? pay.tex.indexOf(c.anchor) : -1;
    // markers sit at the anchor's END — sort by that, or a comment whose
    // anchor is a prefix of a longer one at the same spot lists backwards
    return {c, i, at: at === -1 ? Infinity : at + c.anchor.length};
  });
  keyed.sort((a, b) => a.at - b.at || a.i - b.i);
  if (keyed.every((k, j) => k.i === j)) return false;
  cs.length = 0;
  keyed.forEach(k => cs.push(k.c));
  return true;
}

// comment boxes grow to fit their text (also when AI feedback lands)
function autosize(t) {
  t.style.height = "auto";
  t.style.height = (t.scrollHeight + 2) + "px";
}

function focusComment(el, idx) {
  const ta = el.querySelector(`.pcomments textarea[data-ci="${idx}"]`);
  if (ta) { ta.focus(); scrollPaneTo($("#main"), ta, "nearest"); }
}

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
  html += `<div class="addrow">
    <span class="addhint">${u.tex ?
      "select text in the TeX view to anchor it — $math$ renders" : ""}</span>
    <span class="sp"></span>
    <button class="ghost addc">Comment</button></div>`;
  box.innerHTML = html;

  // Handlers read the model fresh at event time (never a captured array):
  // a completed autosave must not strand them on stale objects.
  box.querySelectorAll("textarea").forEach(t => {
    autosize(t);
    t.addEventListener("input", () => {
      autosize(t);
      const cs = pdata(slug, n).comments;
      cs[Number(t.dataset.ci)].text = t.value;
      queueSave(slug, n, {comments: cs});
    });
    // Enter finishes the comment; Shift+Enter makes a new line
    t.addEventListener("keydown", ev => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        t.blur();
      }
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
        // Read the selection from a cleaned clone: a selection that
        // sweeps over another comment's numbered marker must not carry
        // the marker's digits into the anchor text.  (And range, not
        // sel.toString() — the latter is empty when the document lacks
        // focus, e.g. right after a toolbar click.)
        const frag = range.cloneContents();
        frag.querySelectorAll("sup.cmark, .cpop").forEach(x => x.remove());
        const text = frag.textContent;
        const pay = P[slug + "|" + n];
        if (pay && pay.tex && pay.tex.includes(text)) anchor = text;
      }
      sel.removeAllRanges();
    }
    const cs = pdata(slug, n).comments;
    // an empty comment gets reused instead of stacking up blanks: refocus
    // it, attaching the newly captured anchor if there is one
    const empty = cs.find(c => !c.text.trim());
    if (empty && !anchor) { focusComment(el, cs.indexOf(empty)); return; }
    const c = empty || {anchor: null, text: ""};
    if (anchor) c.anchor = anchor;
    if (!empty) cs.push(c);
    orderComments(slug, n);
    queueSave(slug, n, {comments: cs});
    renderComments(el, slug, n);
    paintContent(el, slug, n);
    focusComment(el, cs.indexOf(c));
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
       <button class="ghost use-score">Use Score</button></div>`;
  }
  if (d.feedback) {
    html += `<div style="margin-top:.25rem">${esc(d.feedback)}
      <button class="ghost use-fb">Add as Comment</button></div>`;
  }
  if (d.comments && d.comments.length) {
    html += "<ol class=\"dclist\">" + d.comments.map((c, i) => `
      <li>${esc(c.text)}
        <button class="ghost use-dc" data-i="${i}">Add</button>
        ${c.anchor ? `<span class="anchor" title="${esc(c.anchor)}">⚓ ${esc(c.anchor)}</span>` : ""}
      </li>`).join("") + "</ol>";
  }
  if (d.issues && d.issues.length) {
    html += "<ul>" + d.issues.map(i => `<li>${esc(i)}</li>`).join("") + "</ul>";
  }
  box.innerHTML = html;
  box.querySelectorAll(".use-dc").forEach(b => {
    b.addEventListener("click", () => {
      const dc = d.comments[Number(b.dataset.i)];
      const cs = pdata(slug, n).comments;
      if (!cs.some(c => c.anchor === (dc.anchor || null) &&
                        c.text === dc.text)) {
        cs.push({anchor: dc.anchor || null, text: dc.text});
        orderComments(slug, n);
        queueSave(slug, n, {comments: cs});
        renderComments(el, slug, n);
        paintContent(el, slug, n);
      }
      b.disabled = true;
      b.textContent = "Added";
    });
  });
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

function unitHeader(u, buttons) {
  let h = `<div class="stuhead"><div class="sturow">
    <h2>${esc(u.slug)}</h2>${badges(u)}
    <span class="sp"></span>${buttons || ""}</div>`;
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
  const jumps = S.rubric.map((rp, i) =>
    `<button class="jump" data-n="${i + 1}">${esc(rp.label)}</button>`
  ).join("");
  main.innerHTML = `
    <div id="stunav-wrap"><div id="stunav">
      <span class="nm">${esc(slug)}</span>
      ${jumps}
      <button class="ghost" id="totop" title="Back to top">↑ Top</button>
    </div></div>` +
    unitHeader(u) +
    `<div id="partspane"></div>`;
  const pane = $("#partspane");
  for (let n = 1; n <= S.n_parts; n++) pane.appendChild(partPanel(slug, n));
  main.querySelectorAll("#stunav .jump").forEach(b =>
    b.addEventListener("click", () => {
      const t = document.querySelector(`.part[data-part="${b.dataset.n}"]`);
      if (t) { setActiveCard(t, false); scrollPaneTo(main, t, "start"); }
    }));
  $("#totop").addEventListener("click", () =>
    main.scrollTo({top: 0, behavior: "smooth"}));
  activeCard = null;
  setActiveCard(pane.querySelector(`.part[data-part="${activePart}"]`) ||
                pane.querySelector(".part"), false);
  updatePanelBtns();
  if ($("#pdfpanel").classList.contains("open")) openPdfPanel(slug);
  main.scrollTop = 0;
}

// the sticky per-student nav appears once the header has scrolled away;
// the statement pane follows the topmost card in view
$("#main").addEventListener("scroll", () => {
  const nav = document.getElementById("stunav");
  if (nav) nav.classList.toggle("show", $("#main").scrollTop > 150);
  if (!trackTick) {
    trackTick = true;
    requestAnimationFrame(() => { trackTick = false; trackActivePart(); });
  }
});

// ------------------------------------------------------------ active card --

// The card being graded: activated by click/focus, moved with the keys.
let activeCard = null;

function setActiveCard(el, scroll) {
  if (!el) return;
  if (activeCard && activeCard !== el)
    activeCard.classList.remove("activecard");
  activeCard = el;
  el.classList.add("activecard");
  if (view === "student") setActivePart(Number(el.dataset.part));
  if (scroll) scrollPaneTo($("#main"), el, "start");
}

function moveActive(dir, skipGraded) {
  const list = [...$("#main").querySelectorAll(".part")];
  if (!list.length) return;
  let i = activeCard ? list.indexOf(activeCard) : -1;
  while (true) {
    i += dir;
    if (i < 0 || i >= list.length) return;
    const el = list[i];
    if (skipGraded &&
        pdata(el.dataset.slug, Number(el.dataset.part)).status === "graded")
      continue;
    setActiveCard(el, true);
    return;
  }
}

// clicking or focusing anywhere in a card makes it the active one
$("#main").addEventListener("mousedown", e => {
  const p = e.target.closest(".part");
  if (p) setActiveCard(p, false);
});
$("#main").addEventListener("focusin", e => {
  const p = e.target.closest(".part");
  if (p) setActiveCard(p, false);
});

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
      <span class="sp"></span>
    </div>
    <div id="pcards"></div>`;
  $("#psel").addEventListener("change", e => showPart(Number(e.target.value)));
  $("#pprev").addEventListener("click", () => showPart(n - 1));
  $("#pnext").addEventListener("click", () => showPart(n + 1));
  const cards = $("#pcards");
  for (const u of S.units)
    cards.appendChild(partPanel(u.slug, n, {who: true}));
  setActivePart(n);
  activeCard = null;
  setActiveCard(cards.querySelector(".part"), false);
  updatePanelBtns();
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

// ---------------------------------------------------------------- keyboard --

// Single-letter shortcuts act on the active card and only fire outside
// text fields.  ↓/↑ (w also = up) move between cards, Shift skips to the
// next ungraded one; s or Enter focus the score; c comments (on the TeX
// selection if there is one); t toggles TeX.  Panels: q = PDF (opened at
// the active part), a = problem, e = solutions, d = list.  Esc leaves a
// field / closes popovers.
document.addEventListener("keydown", e => {
  const t = e.target;
  const editing = /INPUT|TEXTAREA|SELECT/.test(t.tagName) ||
                  t.isContentEditable;
  if (e.key === "Escape") {
    if (editing) t.blur();
    else document.querySelectorAll(".cpop").forEach(p => p.remove());
    return;
  }
  if (editing || e.metaKey || e.ctrlKey || e.altKey || !S) return;
  // a button focused by an earlier click would otherwise keep a focus
  // ring through the whole keyboard session
  if (document.activeElement &&
      document.activeElement.tagName === "BUTTON")
    document.activeElement.blur();
  const k = e.key.toLowerCase();
  if (e.key === "ArrowDown" || e.key === "ArrowUp" || k === "w") {
    e.preventDefault();
    moveActive(e.key === "ArrowDown" ? 1 : -1, e.shiftKey);
  } else if (k === "q" || k === "p") {
    if ($("#pdfpanel").classList.contains("open")) closePdfPanel();
    else if (activeCard) openPdfPanel(activeCard.dataset.slug,
                                      Number(activeCard.dataset.part));
  } else if (k === "a") {
    toggleStmtPanel();
  } else if (k === "e") {
    toggleMain();
  } else if (k === "d") {
    toggleSidebar();
  } else if (k === "t") {
    const b = activeCard && activeCard.querySelector(".toggle-tex");
    if (b && b.style.display !== "none") b.click();
  } else if (k === "u") {
    // use the AI draft's suggested score on the active card
    const b = activeCard && activeCard.querySelector(".use-score");
    if (b) b.click();
  } else if (e.key === "Enter" || k === "s") {
    if (activeCard) {
      e.preventDefault();
      const s = activeCard.querySelector(".score");
      s.focus(); s.select();
      scrollPaneTo($("#main"), activeCard, "nearest");
    }
  } else if (k === "c") {
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && sel.rangeCount) {
      const node = sel.getRangeAt(0).commonAncestorContainer;
      const elc = node.nodeType === 1 ? node : node.parentElement;
      const panel = elc && elc.closest("pre.texsrc") && elc.closest(".part");
      if (panel) {
        e.preventDefault();
        setActiveCard(panel, false);
        panel.querySelector(".addc").click();
        return;
      }
    }
    if (activeCard) {
      e.preventDefault();
      activeCard.querySelector(".addc").click();
    }
  }
});

$("#switch").addEventListener("click", async () => {
  await settleSaves();
  if (saveState() !== "clean") { updateSaveStat(); return; }
  await api("/api/close", {});
  location.reload();
});

// ---------------------------------------------------------------- export --

function notice(msg) {
  const n = $("#notice");
  n.textContent = msg;
  n.style.display = "block";
}
$("#notice").addEventListener("click", () =>
  $("#notice").style.display = "none");

$("#export").addEventListener("click", async () => {
  await settleSaves();
  if (saveState() !== "clean") { updateSaveStat(); return; }
  try { await api("/api/export", {pdf: false}); }
  catch (e) { notice("Export: " + e.message); return; }
  $("#export").disabled = true;
  $("#export").textContent = "Exporting…";
  const poll = setInterval(async () => {
    let st;
    try { st = await api("/api/export"); } catch (e) { return; }
    if (st.running) return;
    clearInterval(poll);
    $("#export").disabled = false;
    $("#export").textContent = "Export";
    if (st.error) { notice("Export failed: " + st.error); return; }
    const s = st.summary || {};
    notice(`Exported ${s.exported} submissions` +
      (s.skipped ? ` (${s.skipped} with nothing graded were skipped)` : "") +
      (s.pdf_failures ? `; ${s.pdf_failures} PDF sheets failed` : "") +
      (s.worksheet != null ?
        `; grading worksheet filled with ${s.worksheet} totals` : "") +
      `. Files are in ${s.out}` +
      (s.warnings && s.warnings.length ? ` — ${s.warnings.join("; ")}` : "") +
      ". Click to dismiss.");
  }, 1000);
});

// ------------------------------------------------------------------ init --

(async function init() {
  S = await api("/api/state");
  setProgress(...S.progress);
  $("#foldname").textContent = S.folder.split("/").filter(Boolean).slice(-2).join("/");
  document.title = `hwGenie — ${S.folder.split("/").pop()}`;
  updateSaveStat();
  setView("student");
  await ensureStmtPane();
  if (stmtData.problems.length) {
    $("#stmtpanel").classList.add("open");
    updateStmtPane();
    updatePanelBtns();
  }
})();

// the statement card is position-computed; keep it right after reflows
addEventListener("resize", () => updateStmtPane());

// liveness for --auto-exit servers: heartbeat plus a goodbye beacon so
// closing the tab shuts hwGrader down (a reload's next ping cancels it)
setInterval(() => {
  fetch("/ping", {method: "POST", body: "{}"}).catch(() => {});
}, 2000);
addEventListener("pagehide", () => {
  try { navigator.sendBeacon("/bye", "{}"); } catch (e) {}
});
</script>
</body>
</html>
""".replace("__BASE__", BASE_CSS)


PICKER_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hwGenie</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<style>
__BASE__
  /* height:auto so the body box spans the full content: the sticky
     .appnav sticks for the whole scroll, not just the first viewport */
  html, body { height: auto; min-height: 100%; }
  body { overflow: auto; display: block; }
  main { max-width: 620px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }
  .sub { color: var(--muted); margin: 0 0 1.75rem; }
  h2 { font-size: .95rem; letter-spacing: .04em; text-transform: uppercase;
       color: var(--muted); margin: 1.6rem 0 .5rem; }
  .row {
    display: flex; align-items: baseline; gap: .8rem; cursor: pointer;
    background: var(--card-bg); padding: .55rem .8rem; margin: 0 0 .45rem;
  }
  .row:hover { background: var(--hover-bg); }
  a.row { text-decoration: none; color: inherit; }
  .row .path { flex: 1; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  .row .meta { color: var(--muted); font-size: .8rem; white-space: nowrap; }
  .manual { display: flex; gap: .5rem; margin-top: .4rem; }
  .manual input {
    flex: 1; font: inherit; padding: .45rem .6rem; color: var(--fg);
    background: var(--card-bg); border: 1px solid var(--border);
  }
  .manual input:focus { outline: 2px solid var(--accent);
                        border-color: transparent; }
  .manual button {
    padding: .45rem 1.2rem; cursor: pointer; border: none;
    background: var(--accent); color: var(--bg);
  }
  .hint { color: var(--muted); font-size: .8rem; margin-top: .4rem; }
  #err { color: var(--alert); margin-top: .8rem; display: none; }
  .none { color: var(--muted); font-style: italic; font-size: .9rem; }
</style>
</head>
<body>
__NAV__
<main>
  <p class="sub">Pick the assignment to grade.</p>
  <h2>Recent</h2>
  <div id="recents"><span class="none">nothing yet</span></div>
  <h2>Found in <span id="root"></span></h2>
  <div id="found"><span class="none">scanning…</span></div>
  <h2>Somewhere else</h2>
  <div class="manual">
    <input id="path" spellcheck="false"
      placeholder="/path/to/grading-folder or moodle-download.zip">
    <button id="open">Open</button>
  </div>
  <p class="hint">Paste a grading folder (made by <code>hwgenie
  collect</code>) or a Moodle &ldquo;Download all submissions&rdquo; .zip
  &mdash; a zip is collected into a folder next to it first.</p>
  <div id="err"></div>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function openPath(p) {
  $("#err").style.display = "none";
  try {
    const r = await fetch("/api/open",
      {method: "POST", body: JSON.stringify({path: p})});
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "could not open");
    location.reload();
  } catch (e) {
    $("#err").textContent = e.message;
    $("#err").style.display = "block";
  }
}

function rows(el, items, rootPrefix) {
  if (!items.length) return;
  el.innerHTML = items.map(f => {
    const rel = rootPrefix && f.path.startsWith(rootPrefix)
      ? f.path.slice(rootPrefix.length).replace(/^\//, "") : f.path;
    const meta = [f.units !== null && f.units !== undefined ?
                  f.units + " submissions" : "", f.created]
                 .filter(Boolean).join(" · ");
    return `<div class="row" data-p="${esc(f.path)}">
      <span class="path">${esc(rel || f.path)}</span>
      <span class="meta">${esc(meta)}</span></div>`;
  }).join("");
  el.querySelectorAll(".row").forEach(r =>
    r.addEventListener("click", () => openPath(r.dataset.p)));
}

(async function init() {
  const s = await (await fetch("/api/scan")).json();
  $("#root").textContent = s.root;
  rows($("#found"), s.folders, s.root);
  if (!s.folders.length)
    $("#found").innerHTML = '<span class="none">no grading folders ' +
      'found — collect a Moodle zip below</span>';
  rows($("#recents"), s.recents.map(p => ({path: p})), null);
})();

$("#open").addEventListener("click", () => {
  const p = $("#path").value.trim();
  if (p) openPath(p);
});
$("#path").addEventListener("keydown", e => {
  if (e.key === "Enter") $("#open").click();
});
setInterval(() => {
  fetch("/ping", {method: "POST", body: "{}"}).catch(() => {});
}, 2000);
addEventListener("pagehide", () => {
  try { navigator.sendBeacon("/bye", "{}"); } catch (e) {}
});
</script>
</body>
</html>
""".replace("__BASE__", BASE_CSS)
