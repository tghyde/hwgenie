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
import html
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
    RUBRIC_NAME,
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

    Returns [{"num", "tex", "boxes", "solutions"}] where tex is the problem
    body with each solution box replaced by an ``HWGRADERBOX<n>`` token
    (n = the box's global ordinal, matching grading part numbers), boxes
    lists the ordinals appearing in that problem, and solutions maps an
    ordinal to the box's own content when the file carries any — the
    assignment *source* (or a solutions variant) rather than the blank
    submission template — so graders can see the instructor's solution.
    Comment-aware, like extract_solution_bodies.
    """
    problems: list[dict] = []
    cur: list[str] | None = None
    boxes: list[int] = []
    sols: dict[int, str] = {}
    sol_lines: list[str] = []
    box = 0
    in_sol = False

    def close_solution(body: list[str]) -> None:
        text_ = "\n".join(body)
        code_ = "\n".join(_strip_comment(ln) for ln in body).strip()
        if code_ and cur is not None:
            sols[box] = text_.strip("\n")

    for line in text.splitlines():
        code = _strip_comment(line)
        if in_sol:
            j = code.find(SOLUTION_END)
            if j == -1:
                sol_lines.append(line)
                continue
            in_sol = False
            sol_lines.append(line[:j])
            close_solution(sol_lines)
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
                sol_lines = [line[i + len(SOLUTION_BEGIN):]]
            else:
                close_solution([line[i + len(SOLUTION_BEGIN):j]])
                if cur is not None and line[j + len(SOLUTION_END):].strip():
                    cur.append(line[j + len(SOLUTION_END):])
            continue
        b = code.find(r"\begin{problem}")
        if b != -1:
            cur = [line[b + len(r"\begin{problem}"):]]
            boxes = []
            sols = {}
            continue
        e = code.find(r"\end{problem}")
        if e != -1 and cur is not None:
            cur.append(line[:e])
            problems.append({"num": len(problems) + 1,
                             "tex": "\n".join(cur), "boxes": boxes,
                             "solutions": sols})
            cur = None
            continue
        if cur is not None:
            cur.append(line)
    return problems


class GradingApp:
    def __init__(self, folder: Path, grader_only: bool = False):
        self.folder = Path(folder)
        self.grader_only = grader_only   # hosted: no course gradebook here
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
        self._course_preamble: str | None = None
        self.export_state: dict = {"running": False, "error": None,
                                   "summary": None}

    # ---------------------------------------------------------- macros --

    def course_preamble(self) -> str:
        """The course's own macro definitions (hwgenie.sty + coursedata),
        from the copy collect leaves in the folder, else found next to
        the template / the course clone on this machine.  Prepended to
        every preamble handed to the HTML converter."""
        if self._course_preamble is not None:
            return self._course_preamble
        from .collect import COURSE_MACROS, course_macro_text, find_course_dir
        text = ""
        name = self.manifest.get("macros") or COURSE_MACROS
        p = self.folder / name
        if p.is_file():
            text = p.read_text(errors="replace")
        else:
            tmpl = (self.manifest.get("template") or {}).get("path")
            tp = Path(tmpl) if tmpl else None
            if tp is not None and not tp.is_absolute():
                tp = self.folder / tp
            course = find_course_dir(tp if tp and tp.is_file() else None,
                                     self.folder)
            if course is not None:
                text = course_macro_text(course)
        self._course_preamble = text
        return text

    # ------------------------------------------------------------ late --

    def late_context(self):
        """Fresh each call: rubric.yml's due date, late.json decisions and
        the course gradebook are all small files other tools edit.  The
        hosted grader has no course gradebook, so it shows the policy tier
        but leaves the free-late question to the instructor's export."""
        from . import late as late_mod
        out_of = sum(rp.max or 0 for rp in self.rubric if not rp.ec)
        try:
            return late_mod.LateContext(self.folder, out_of=out_of,
                                        with_book=not self.grader_only), None
        except late_mod.LateError as e:
            return None, str(e)

    def set_late(self, req: dict) -> dict:
        from . import late as late_mod
        slug = req.get("slug")
        if slug not in self.by_slug:
            raise GradeError(f"unknown submission {slug!r}")
        ctx, err = self.late_context()
        if ctx is None:
            raise GradeError(err or "late policy unavailable")
        try:
            late_mod.save_decision(
                self.folder, slug, str(req.get("action", "auto")),
                note=str(req.get("note") or ""),
                extension=req.get("extension") or None, tz=ctx.tz)
        except late_mod.LateError as e:
            raise GradeError(str(e))
        ctx, _ = self.late_context()
        return {"ok": True, "slug": slug,
                "late": ctx.payload(self.by_slug[slug])}

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
            preamble = self.course_preamble() + "\n" + preamble
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
        ctx, late_err = self.late_context()
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
                    "late": ctx.payload(u) if ctx else None,
                })
            graded, total = self.store.progress(slugs)
        late_summary = ctx.summary() if ctx else {"due": None}
        if late_err:
            late_summary["error"] = late_err
        return {
            "folder": str(self.folder),
            "n_parts": self.n_parts,
            "rubric": [{"label": rp.label, "max": rp.max, "ec": rp.ec}
                       for rp in self.rubric],
            "groups": self.groups,
            "progress": [graded, total],
            "units": units,
            "late": late_summary,
        }

    def apply_grade(self, req: dict) -> dict:
        slug = req.get("slug")
        if slug not in self.by_slug:
            raise GradeError(f"unknown submission {slug!r}")
        fields = {k: req[k] for k in ("score", "comments") if k in req}
        by = req.get("by")
        with self.lock:
            data = self.store.update(slug, req.get("part"), fields,
                                     by=by if isinstance(by, str) else None)
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
        result: dict = {"problems": [], "macros": {}, "warnings": [],
                        "solutions": {}}
        tmpl = (self.manifest.get("template") or {}).get("path")
        path = Path(tmpl) if tmpl else None
        if path is not None and not path.is_absolute():
            path = self.folder / path
        if path is not None and path.is_file():
            text = path.read_text(errors="replace")
            preamble = self.course_preamble() + "\n" + split_preamble(text)
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
                # instructor solutions (only when the template file has
                # them, i.e. it is the assignment source) — grader-facing
                # only; the feedback export never touches these
                for n, stex in blk.get("solutions", {}).items():
                    try:
                        sconv = HtmlConverter(stex, include_solutions=True,
                                              extra_preamble=preamble,
                                              section=section)
                        result["solutions"][str(n)] = sconv.convert()
                    except Exception as e:
                        result["warnings"].append(
                            f"solution for part {n}: {e}")
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
    """The server's mutable state: the open assignments (keyed by resolved
    folder path, so several browser tabs — or several graders on a hosted
    server — can work different assignments at once), the default one for
    URLs without a ``folder`` param, the folder-scan root and the recents
    file.

    ``grader_only`` locks the server down for hosting to graders: only
    grading folders under the scan root may be opened, and the handler
    hides everything except the grading pages."""

    def __init__(self, root: Path, grader_only: bool = False):
        self.root = Path(root).resolve()
        self.grader_only = grader_only
        self.current: GradingApp | None = None
        self.apps: dict[str, tuple[GradingApp, tuple]] = {}
        self.apps_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.last_ping: float | None = None   # for --auto-exit
        self.bye_at: float | None = None

    @staticmethod
    def _folder_sig(p: Path) -> tuple:
        """What a re-push changes: the manifest and rubric.  (A push also
        touches the manifest, so new submission files invalidate too.)"""
        def mt(f: Path):
            try:
                return f.stat().st_mtime_ns
            except OSError:
                return None
        return (mt(p / MANIFEST_NAME), mt(p / RUBRIC_NAME))

    def get_app(self, path: Path | str) -> GradingApp:
        """The (cached) GradingApp for a grading folder.  One instance per
        folder no matter how many clients use it, so all writes share its
        lock and render caches.  The instance is rebuilt when the folder's
        manifest or rubric changes on disk (an instructor re-push to a
        hosted server), so its cached units/rubric/bodies never go stale.
        Raises GradeError for anything that is not an openable grading
        folder."""
        p = Path(path).expanduser().resolve()
        if self.grader_only and self.root not in (p, *p.parents):
            raise GradeError(f"{p} is outside the served folder")
        key = str(p)
        sig = self._folder_sig(p)
        with self.apps_lock:
            entry = self.apps.get(key)
            if entry is None or entry[1] != sig:
                entry = self.apps[key] = (
                    GradingApp(p, grader_only=self.grader_only), sig)
        return entry[0]

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
        if self.grader_only:   # graders don't touch the recents file
            return
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
            if self.grader_only:
                raise GradeError("collecting a zip is disabled on a "
                                 "grader-only server")
            path = self.run_collect(zip_path=path)["folder"]
        app = self.get_app(path)
        self.current = app
        self.remember(app.folder)
        return app

    # ------------------------------------------------ assignment layout --

    NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    def lab_courses(self) -> list[dict]:
        """Course folders directly under the lab root, with their
        assignment folders (anything holding moodle-raw/, build/ or a
        grading folder)."""
        out = []
        try:
            dirs = sorted(d for d in self.root.iterdir()
                          if d.is_dir() and not d.name.startswith("."))
        except OSError:
            return out
        for d in dirs:
            asg = []
            for a in sorted(x for x in d.iterdir()
                            if x.is_dir() and not x.name.startswith(".")):
                if any((a / sub).is_dir() for sub in ("moodle-raw", "build",
                                                      "grading")) \
                        or (a / MANIFEST_NAME).is_file():
                    asg.append({"name": a.name, "path": str(a),
                                "collected": (a / "grading" / MANIFEST_NAME)
                                .is_file() or (a / MANIFEST_NAME).is_file()})
            if asg or (d / "gradebook.json").is_file() \
                    or d.name.lower().startswith(("math", "cs", "stat")):
                out.append({"name": d.name, "path": str(d),
                            "assignments": asg})
        return out

    def new_assignment(self, course: str, name: str) -> dict:
        """Create <root>/<course>/<name>/{moodle-raw,build} (idempotent)."""
        if self.grader_only:
            raise GradeError("not available on a grader-only server")
        course, name = (course or "").strip(), (name or "").strip()
        for label, val in (("course", course), ("assignment", name)):
            if not self.NAME_RE.match(val):
                raise GradeError(f"{label} name {val!r}: use letters, digits, "
                                 ". _ - (e.g. math301, ps01)")
        asg = self.root / course / name
        existed = asg.is_dir()
        for sub in ("moodle-raw", "build"):
            (asg / sub).mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(asg), "existed": existed,
                "course": course, "name": name,
                "files": self.assignment_files(asg)}

    @staticmethod
    def assignment_files(asg: Path) -> dict:
        def ls(sub):
            d = asg / sub
            return sorted(f.name for f in d.iterdir()
                          if f.is_file() and not f.name.startswith(".")) \
                if d.is_dir() else []
        return {"moodle-raw": ls("moodle-raw"), "build": ls("build"),
                "collected": (asg / "grading" / MANIFEST_NAME).is_file()}

    def save_upload(self, course: str, name: str, filename: str,
                    body: bytes) -> dict:
        """Drop a browser-uploaded file into the assignment: zips and
        worksheets go to moodle-raw/, .tex to build/."""
        if self.grader_only:
            raise GradeError("not available on a grader-only server")
        fname = Path(filename or "").name
        if not fname or fname.startswith("."):
            raise GradeError("bad filename")
        ext = Path(fname).suffix.lower()
        sub = {".zip": "moodle-raw", ".csv": "moodle-raw",
               ".tex": "build"}.get(ext)
        if sub is None:
            raise GradeError(f"{fname}: only .zip, .csv and .tex files go "
                             "into an assignment")
        asg = self.root / course / name
        if not asg.is_dir() or not self.NAME_RE.match(course) \
                or not self.NAME_RE.match(name):
            raise GradeError(f"no assignment {course}/{name} — create it first")
        dest = asg / sub / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return {"ok": True, "saved": str(dest), "kind": sub, "name": fname,
                "size": len(body), "files": self.assignment_files(asg)}

    def run_collect(self, zip_path=None, folder=None, due=None,
                    timezone_name=None) -> dict:
        """Collect (or re-collect) from the assignment-folder layout; the
        result is the report the picker page shows."""
        from . import late as late_mod
        from .collect import collect, locate
        if self.grader_only:
            raise GradeError("collecting is done by the instructor, not "
                             "on the hosted grader")
        plan = locate(zip_path=zip_path, folder=folder)
        res = collect(plan["zip"], plan["dest"], template=plan["template"],
                      due=(due or None), timezone_name=(timezone_name or None))
        # a fresh view for anyone who has the folder open
        with self.apps_lock:
            self.apps.pop(str(plan["dest"].resolve()), None)
        lines = []
        for u in res.units:
            tag = ("+new" if u.slug in res.added else
                   "~resubmitted (kept)" if u.slug in res.resubmitted else
                   "~replaced" if u.slug in res.replaced else "")
            lt = res.late.get(u.slug)
            late = (f"LATE {lt['late_text']} ({lt['label']})"
                    if lt and lt.get("is_late") else "")
            notes = "; ".join(u.anomalies)
            bits = [b for b in (tag, late, ("!! " + notes) if notes else "")
                    if b]
            if bits or not res.update:
                lines.append(f"{u.slug}: " + "  ".join(bits) if bits
                             else u.slug)
        settings = late_mod.read_settings(plan["dest"])
        return {
            "ok": True, "folder": str(plan["dest"]), "zip": str(plan["zip"]),
            "template": str(plan["template"]) if plan["template"] else None,
            "update": res.update, "units": len(res.units),
            "added": res.added, "replaced": res.replaced,
            "resubmitted": res.resubmitted, "unchanged": len(res.unchanged),
            "skipped": res.skipped,
            "worksheet": str(res.worksheet) if res.worksheet else None,
            "due": settings.get("due"),
            "late": sorted(s_ for s_, lt in res.late.items()
                           if lt.get("is_late")),
            "lines": lines,
        }


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

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _app(self, folder: str | None = None) -> GradingApp | None:
            """The assignment a request addresses: its ``folder`` query/body
            param, else the server's default (the CLI-opened folder)."""
            if folder:
                try:
                    return holder.get_app(folder)
                except (GradeError, OSError) as e:
                    self._json({"error": str(e)}, 404)
                    return None
            app = holder.current
            if app is None:
                self._json({"error": "no assignment open"}, 409)
            return app

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            folder = q.get("folder", [None])[0]
            grader_only = holder.grader_only
            if url.path in ("/", "/index.html", "/courses"):
                if grader_only:
                    self._redirect("/grading")
                    return
                # course management is the app's home page
                from .course_admin import render_courses
                holder.alive()
                self._send(render_courses().encode("utf-8"))
            elif url.path == "/grading":
                holder.alive()
                if "pick" in q:
                    app = None
                elif folder:
                    try:
                        app = holder.get_app(folder)
                        holder.remember(app.folder)
                    except (GradeError, OSError) as e:
                        self._redirect("/grading?pick=1&err="
                                       + urllib.parse.quote(str(e)))
                        return
                else:
                    app = holder.current
                page = (render_grader(str(app.folder), grader_only) if app
                        else render_picker(grader_only))
                self._send(page.encode("utf-8"))
            elif url.path == "/api/lab" and not grader_only:
                self._json({"root": str(holder.root),
                            "courses": holder.lab_courses()})
            elif url.path == "/api/scan":
                self._json({"root": str(holder.root),
                            "folders": holder.scan(),
                            "recents": ([] if grader_only
                                        else holder.recents())})
            elif url.path == "/grading/howto" and not grader_only:
                from .howto import render_howto
                holder.alive()
                self._send(render_howto().encode("utf-8"))
            elif url.path == "/api/remote" and not grader_only:
                from .remote_grading import api_get as remote_get
                res = remote_get(url.path)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif url.path == "/gradebook" and not grader_only:
                from .late import course_dir
                course = q.get("course", [None])[0]
                if not course and folder:
                    course = str(course_dir(Path(folder)))
                if not course and holder.current:
                    course = str(course_dir(holder.current.folder))
                if not course:
                    self._redirect("/grading?pick=1&err="
                                   + urllib.parse.quote("no course given"))
                    return
                self._send(render_gradebook(Path(course)).encode("utf-8"))
            elif url.path == "/overview" and not grader_only:
                if not folder:
                    self._redirect("/grading?pick=1&err="
                                   + urllib.parse.quote("no assignment given"))
                    return
                try:
                    app = holder.get_app(folder)
                except (GradeError, OSError) as e:
                    self._redirect("/grading?pick=1&err="
                                   + urllib.parse.quote(str(e)))
                    return
                from .overview import render_overview
                with app.lock:
                    page = render_overview(app)
                self._send(page.encode("utf-8"))
            elif url.path == "/api/state":
                if (app := self._app(folder)):
                    self._json(app.state_payload())
            elif url.path == "/api/part":
                if not (app := self._app(folder)):
                    return
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
                if (app := self._app(folder)):
                    self._json(app.problems_payload())
            elif url.path == "/api/export":
                if (app := self._app(folder)):
                    self._json(app.export_state)
            elif url.path == "/api/pdfmap":
                if not (app := self._app(folder)):
                    return
                slug = q.get("slug", [""])[0]
                if slug not in app.by_slug:
                    self._json({"error": f"unknown submission {slug!r}"}, 404)
                    return
                self._json(app.pdf_map(slug))
            elif url.path == "/quotes" and not grader_only:
                from .quotebank import render_quotes
                holder.alive()
                self._send(render_quotes().encode("utf-8"))
            elif url.path.startswith("/quotes/api/") and not grader_only:
                from .quotebank import api_get
                res = api_get(url.path)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif url.path.startswith("/courses/api/") and not grader_only:
                from .course_admin import api_get as courses_get
                res = courses_get(url.path)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif url.path == "/problem-sets" and not grader_only:
                from .problem_sets import render_problem_sets
                holder.alive()
                self._send(render_problem_sets().encode("utf-8"))
            elif (url.path.startswith("/problem-sets/api/")
                  and not grader_only):
                from .problem_sets import api_get as sets_get
                res = sets_get(url.path)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif url.path == "/new-course" and not grader_only:
                from .new_course_gui import render_wizard
                holder.alive()
                self._send(render_wizard(embedded=True).encode("utf-8"))
            elif url.path == "/new-course/status" and not grader_only:
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
                if not (app := self._app(folder)):
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
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/assignment/upload":
                # raw file body (a browser file picker), not JSON
                if holder.grader_only:
                    self._json({"ok": False, "error": "not available on a "
                                "grader-only server"}, 403)
                    return
                if n > 500 * 1024 * 1024:
                    self._json({"ok": False, "error": "file too large"}, 413)
                    return
                qs = urllib.parse.parse_qs(url.query)
                body = self.rfile.read(n)
                try:
                    self._json(holder.save_upload(
                        qs.get("course", [""])[0], qs.get("name", [""])[0],
                        qs.get("filename", [""])[0], body))
                except (GradeError, OSError) as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                return
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "bad json"}, 400)
                return
            folder = urllib.parse.parse_qs(url.query).get(
                "folder", [None])[0]
            grader_only = holder.grader_only
            self.path = url.path   # route on the bare path below
            if self.path == "/api/grade":
                if not (app := self._app(folder)):
                    return
                try:
                    self._json(app.apply_grade(data))
                except GradeError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/open":
                if grader_only:
                    self._json({"ok": False, "error": "not available on a "
                                "grader-only server"}, 403)
                    return
                from .collect import CollectError
                try:
                    app = holder.open_path(Path(str(data.get("path", ""))))
                    self._json({"ok": True, "folder": str(app.folder)})
                except (GradeError, CollectError, OSError) as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/close":
                if not grader_only:
                    holder.current = None
                self._json({"ok": True})
            elif self.path == "/api/assignment/new":
                if grader_only:
                    self._json({"ok": False, "error": "not available on a "
                                "grader-only server"}, 403)
                    return
                try:
                    self._json(holder.new_assignment(
                        str(data.get("course", "")), str(data.get("name", ""))))
                except (GradeError, OSError) as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/collect":
                if grader_only:
                    self._json({"ok": False, "error": "not available on a "
                                "grader-only server"}, 403)
                    return
                from .collect import CollectError
                try:
                    res = holder.run_collect(
                        zip_path=data.get("zip") or None,
                        folder=data.get("folder") or None,
                        due=data.get("due"), timezone_name=data.get("timezone"))
                    self._json(res)
                except (GradeError, CollectError, OSError) as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/late":
                if grader_only:
                    self._json({"ok": False, "error": "late-work decisions "
                                "are the instructor's, made on their own "
                                "copy"}, 403)
                    return
                if not (app := self._app(folder)):
                    return
                try:
                    self._json(app.set_late(data))
                except GradeError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
            elif self.path == "/api/export":
                if grader_only:
                    self._json({"ok": False, "error": "exporting is done by "
                                "the instructor, not on the hosted "
                                "grader"}, 403)
                    return
                if not (app := self._app(folder)):
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
                                "extra_credit": (res.extra_credit
                                                 or {}).get("rows"),
                                "late": res.late,
                            }}
                    except Exception as e:
                        app.export_state = {"running": False,
                                            "error": str(e), "summary": None}

                threading.Thread(target=job, daemon=True).start()
                self._json({"ok": True})
            elif self.path.startswith("/api/remote/") and not grader_only:
                from .remote_grading import api_post as remote_post
                res = remote_post(self.path, data)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif self.path.startswith("/quotes/api/") and not grader_only:
                from .quotebank import api_post
                res = api_post(self.path, data)
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif self.path.startswith("/courses/api/") and not grader_only:
                from .course_admin import api_post as courses_post
                res = courses_post(self.path, data, course_roots(holder.root))
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif (self.path.startswith("/problem-sets/api/")
                  and not grader_only):
                from .problem_sets import api_post as sets_post
                res = sets_post(self.path, data, course_roots(holder.root))
                if res is None:
                    self._send(b"not found", code=404)
                else:
                    self._json(res[0], res[1])
            elif self.path == "/new-course/create" and not grader_only:
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
              open_browser: bool = True, auto_exit: bool = False,
              host: str = "127.0.0.1", grader_only: bool = False) -> int:
    folder = Path(folder) if folder is not None else Path.cwd()
    holder = AppHolder(root=folder if folder.is_dir() else folder.parent,
                       grader_only=grader_only)
    try:
        holder.open_path(folder)
    except GradeError:
        print(f"note: {folder} is not a grading folder — "
              "opening the assignment picker instead")
    local = host in ("127.0.0.1", "localhost", "::1")
    try:
        server = ThreadingHTTPServer((host, port), make_handler(holder))
    except OSError as e:
        if port and local and e.errno == errno.EADDRINUSE:
            # a second launch while one is running: just show that one
            url = f"http://127.0.0.1:{port}/"
            print(f"hwGenie is already running at {url}")
            if open_browser:
                _open_ui(url)
            return 0
        raise
    # a CLI-opened grading folder goes straight to the grader; the bare
    # launcher lands on the Courses home page (grading in grader mode)
    path = "/grading" if (holder.current or grader_only) else "/"
    shown_host = "127.0.0.1" if local else host
    url = f"http://{shown_host}:{server.server_address[1]}{path}"
    print(f"hwGenie: {url}")
    if not local:
        print(f"(Serving on {host} — reachable from other machines. "
              + ("Grading pages only.)" if grader_only else
                 "Consider --grader-only when hosting for graders.)"))
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
    if open_browser and local:
        _open_ui(url)
    try:
        holder.shutdown.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    print("hwGenie closed.")
    return 0


# -------------------------------------------------------------- the pages --

def render_grader(folder: str, grader_only: bool = False) -> str:
    from .appicon import LAMP_SVG
    cfg = json.dumps({"folder": folder, "grader": grader_only})
    return GRADER_PAGE.replace("__KATEX__", KATEX_VERSION) \
                      .replace("__LAMP__", LAMP_SVG) \
                      .replace("__CFG__", cfg)


def course_assignments(course: Path) -> list[Path]:
    """Grading folders of a course: <course>/<ps>/grading or a direct
    <course>/<x>-grading folder, in name order."""
    course = Path(course)
    out: list[Path] = []
    if not course.is_dir():
        return out
    for d in sorted(course.iterdir()):
        if not d.is_dir():
            continue
        if (d / "grading" / MANIFEST_NAME).is_file():
            out.append(d / "grading")
        elif (d / MANIFEST_NAME).is_file():
            out.append(d)
    return out


def gradebook_data(course: Path) -> dict:
    """The live course gradebook: every assignment folder under the course
    × every student seen in any of them.  Exported totals come from
    gradebook.json; everything else is read straight from the grading
    folders (progress, provisional total under the late policy)."""
    from . import late as late_mod
    from .feedback import (_split_totals, _worksheet_people, display_name,
                           find_worksheet)
    course = Path(course).resolve()
    errors: list[str] = []
    try:
        book = late_mod.Gradebook(course / late_mod.GRADEBOOK_JSON)
    except late_mod.LateError as e:
        errors.append(str(e))
        book = late_mod.Gradebook(course / "missing-gradebook.json")
    students: dict[str, dict] = {}
    for mid, rec in book.data["students"].items():
        students[mid] = {"moodle_id": mid, "name": rec.get("name", ""),
                         "email": rec.get("email", ""), "cells": {}}
    keys: list[str] = []
    folders: list[str] = []
    for folder in course_assignments(course):
        key = late_mod.assignment_key(folder)
        keys.append(key)
        folders.append(str(folder))
        try:
            app = GradingApp(folder)
        except Exception as e:  # noqa: BLE001 — one bad folder, not the page
            errors.append(f"{key}: {e}")
            continue
        ctx, err = app.late_context()
        if err:
            errors.append(f"{key}: {err}")
        ws = find_worksheet(folder)
        people = _worksheet_people(ws) if ws else {}
        out_of = sum(rp.max or 0 for rp in app.rubric if not rp.ec)
        for u in app.units:
            mid = str(u["moodle_id"])
            st = students.setdefault(mid, {"moodle_id": mid, "name": "",
                                           "email": "", "cells": {}})
            person = people.get(mid) or {}
            st["name"] = st["name"] or person.get("name") or \
                display_name(u["slug"])
            st["email"] = st["email"] or person.get("email", "")
            data = app.store.load(u["slug"])
            raw, ec = _split_totals(app, data)
            graded = sum(1 for part in data["parts"].values()
                         if part["status"] == "graded")
            ls = ctx.status(u) if ctx else None
            exported = ((book.data["students"].get(mid) or {})
                        .get("assignments", {}).get(key))
            st["cells"][key] = {
                "slug": u["slug"], "folder": str(folder),
                "graded": graded, "n_parts": app.n_parts,
                "raw": raw, "ec": ec, "out_of": out_of,
                "provisional": (late_mod.apply_penalty(raw, ls) if ls
                                else raw),
                "hold": bool(ls and ls.hold),
                "late": ls.to_json(ctx.tz) if ls else None,
                "exported": exported,
            }
    for st in students.values():
        used = book.free_late_used_on(st["moodle_id"])
        prov = None
        if not used:
            for key in keys:
                c = st["cells"].get(key)
                if c and c["late"] and c["late"].get("is_late") \
                        and c["late"].get("action") == "free":
                    prov = key
                    break
        st["free_late"] = {"used": used, "provisional": prov}
    def by_last(st: dict) -> tuple:
        words = st["name"].split()
        return ((words[-1].lower() if words else ""), st["name"].lower(),
                st["moodle_id"])
    rows = sorted(students.values(), key=by_last)
    return {"course": str(course), "name": course.name, "keys": keys,
            "folders": folders,
            "students": rows, "errors": errors, "book": str(book.path),
            "has_book": book.path.is_file()}


def render_gradebook(course: Path) -> str:
    """Instructor-only: the course gradebook page (live progress +
    exported totals + who has spent their free late)."""
    from . import late as late_mod
    from .appicon import LAMP_SVG
    from .webstyle import BASE_CSS, KEEPALIVE_JS, nav_header
    d = gradebook_data(course)
    esc = html.escape
    num = late_mod._num
    body = ""
    if d["errors"]:
        body += '<p class="err">' + " · ".join(map(esc, d["errors"])) + "</p>"
    if not d["keys"]:
        body += ('<p class="none">No grading folders under this course '
                 'folder yet — collect an assignment first.</p>')
    else:
        head = "".join(
            f'<th><a href="/overview?folder={urllib.parse.quote(f)}" '
            f'title="assignment overview">{esc(k)}</a></th>'
            for k, f in zip(d["keys"], d["folders"]))
        trs = []
        for st in d["students"]:
            cells = []
            for k in d["keys"]:
                c = st["cells"].get(k)
                if not c:
                    cells.append('<td class="none">—</td>')
                    continue
                link = ("/grading?folder=" + urllib.parse.quote(c["folder"]))
                L = c.get("late") or {}
                late_bits = ""
                cls = ""
                if L.get("is_late"):
                    cls = " late " + esc(L.get("action") or "")
                    late_bits = (f'<span class="lt" title="{esc(L.get("label") or "")}">'
                                 f'{esc(L.get("late_text") or "")} late</span>')
                ex = c.get("exported")
                if ex:
                    tot = "pending" if ex.get("total") is None else num(ex["total"])
                    tip = (f'exported {ex.get("exported", "")[:10]}; raw '
                           f'{num(ex.get("raw"))}'
                           + (f'; −{num(ex.get("penalty_pts"))} late'
                              if ex.get("penalty_pts") else ""))
                    cells.append(
                        f'<td class="final{cls}" title="{esc(tip)}">'
                        f'<a href="{link}"><b>{tot}</b>'
                        f'<span class="oo">/{num(ex["out_of"])}</span></a>'
                        f'{late_bits}</td>')
                    continue
                g, n = c["graded"], c["n_parts"]
                if g == 0:
                    prog = '<span class="muted">not graded</span>'
                else:
                    prov = "pending" if c["hold"] else num(c["provisional"])
                    prog = (f'<span class="prog">{g}/{n} graded</span> '
                            f'<span class="prov" title="provisional total under '
                            f'the late policy">{prov}<span class="oo">/'
                            f'{num(c["out_of"])}</span></span>')
                cells.append(f'<td class="live{cls}"><a href="{link}">{prog}</a>'
                             f'{late_bits}</td>')
            fl = st["free_late"]
            if fl["used"]:
                free = f'<span class="used">used on {esc(fl["used"])}</span>'
            elif fl["provisional"]:
                free = (f'<span class="prov">{esc(fl["provisional"])} '
                        '(when exported)</span>')
            else:
                free = '<span class="ok">available</span>'
            trs.append(
                f'<tr><td class="nm" title="{esc(st["email"])}">'
                f'{esc(st["name"] or st["moodle_id"])}</td>'
                + "".join(cells) + f'<td class="free">{free}</td></tr>')
        body += (f'<table class="gb"><thead><tr><th>Student</th>{head}'
                 '<th>Free late</th></tr></thead><tbody>'
                 + "".join(trs) + "</tbody></table>")
        body += ('<p class="src">Bold totals are final (exported). Other '
                 'cells show grading progress with a provisional total '
                 'under the late policy; shaded cells were late — hover '
                 'for details, click to open the assignment. '
                 f'Records: <code>{esc(d["book"])}</code>'
                 + (" (with a CSV twin)." if d["has_book"] else
                    " — written at the first export.") + "</p>")
    return (GRADEBOOK_PAGE.replace("__NAV__", nav_header("grading"))
                          .replace("__KEEPALIVE__", KEEPALIVE_JS)
                          .replace("__LAMP__", LAMP_SVG)
                          .replace("__CSS__", BASE_CSS)
                          .replace("__COURSE__", esc(d["name"]))
                          .replace("__BODY__", body))


GRADEBOOK_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>hwGenie — Gradebook</title>
<style>
__CSS__
html, body { height: auto; min-height: 100%; }
body { overflow: auto; display: block; }
main { max-width: 74rem; margin: 1.5rem auto; padding: 0 1rem; }
h1 { margin: 0 0 .2rem; }
table.gb { border-collapse: collapse; font-size: .88rem; }
table.gb th, table.gb td { padding: .4rem .6rem; text-align: right;
  border-bottom: 1px solid var(--border, #ddd); white-space: nowrap;
  vertical-align: baseline; }
table.gb th { font-size: .75rem; letter-spacing: .04em;
  text-transform: uppercase; color: var(--muted); }
table.gb th:first-child, table.gb td.nm { text-align: left; }
table.gb td a, table.gb th a { color: inherit; text-decoration: none; }
table.gb td a:hover, table.gb th a:hover { text-decoration: underline; }
table.gb td .oo { color: var(--muted); font-size: .8em; margin-left: .1em; }
table.gb td .prog { color: var(--muted); font-size: .8em; }
table.gb td .prov { margin-left: .35em; }
table.gb td .lt { display: block; font-size: .7rem; color: var(--alert);
  letter-spacing: .03em; text-transform: uppercase; }
table.gb td.late { background: color-mix(in srgb, var(--alert, #c60) 16%,
  transparent); }
table.gb td.late.free, table.gb td.late.waive, table.gb td.late.extension {
  background: color-mix(in srgb, var(--accent, #08c) 14%, transparent); }
table.gb td.free { text-align: left; font-size: .85rem; }
table.gb td.free .used { color: var(--alert); }
table.gb td.free .ok { color: var(--sol-accent, var(--accent)); }
table.gb td.free .prov { color: var(--muted); }
.none, p.src { color: var(--muted); font-size: .85rem; }
p.err { color: var(--alert); }
</style></head><body>
__NAV__
<main>
<h1>Gradebook — __COURSE__</h1>
<p class="src"><a href="/grading?pick=1">← Grading</a></p>
__BODY__
</main>
__KEEPALIVE__
</body></html>"""


def render_picker(grader_only: bool = False) -> str:
    from .appicon import LAMP_SVG
    from .webstyle import nav_header
    nav = ('<div class="appnav">'
           '<a class="brand" href="/grading">hwGenie __LAMP__</a></div>'
           if grader_only else nav_header("grading"))
    return (PICKER_PAGE.replace("__NAV__", nav)
                       .replace("__LAMP__", LAMP_SVG)
                       .replace("__CFG__",
                                json.dumps({"grader": grader_only})))




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
  .badge.late { background: var(--alert); color: var(--bg); }
  .badge.late.ok { background: var(--mark-bg); color: var(--fg); }
  .badge.late.hold { outline: 2px solid var(--alert); outline-offset: -2px;
                     background: transparent; color: var(--alert); }
  .ltdot { display: inline-block; font-size: .62rem; font-weight: 700;
    line-height: 1; padding: .1rem .25rem; margin-left: .3rem;
    background: var(--alert); color: var(--bg); vertical-align: middle; }
  .ltdot.hold { background: transparent; color: var(--alert);
    outline: 1.5px solid var(--alert); }
  .latebar { display: flex; flex-wrap: wrap; align-items: center;
    gap: .4rem; margin-top: .4rem; font-size: .85rem; color: var(--muted);
    padding: .35rem .6rem; border-left: 3px solid var(--line, #ccc); }
  .latebar.islate { border-left-color: var(--alert); color: var(--fg); }
  .latebar .lt { color: var(--alert); }
  .latebar .muted { color: var(--muted); }
  .latebar .verdict { font-weight: 600; padding: .05rem .4rem;
    background: var(--mark-bg); }
  .latebar .verdict.apply, .latebar .verdict.discuss { background: var(--alert);
    color: var(--bg); }
  .latebar select, .latebar input { font-size: .8rem; padding: .15rem .3rem; }
  .latebar .latenote { width: 12rem; }
  .latebar .lateext { width: 9.5rem; }
  .latenotes { flex-basis: 100%; font-size: .78rem; color: var(--alert); }
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
  .pcontent details.isol { margin: .3rem 0 .8rem; padding: 0 .6rem;
    border-left: 3px solid var(--sol-accent, var(--accent));
    font-size: .92em; }
  .pcontent details.isol > summary { cursor: pointer; font-weight: 600;
    font-family: system-ui, sans-serif; font-size: .8rem; padding: .2rem 0;
    color: var(--sol-accent, var(--accent)); }
  .pcontent details.isol .isolbody { padding: .2rem 0 .4rem; }
  .pcontent .isolall { display: block; font-size: .78rem;
    color: var(--muted); font-family: system-ui, sans-serif;
    margin: 0 0 .5rem; }
  .pcontent .isolall input { vertical-align: middle; margin-right: .3rem; }
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

  .ecbadge { font-size: .66rem; font-weight: 700; letter-spacing: .05em;
    padding: .08rem .32rem; background: var(--accent); color: var(--bg);
    white-space: nowrap; }
  .gby { font-size: .72rem; color: var(--muted); white-space: nowrap; }

  #nameov { position: fixed; inset: 0; z-index: 60; display: flex;
    align-items: center; justify-content: center;
    background: rgba(0,0,0,.45); }
  .namebox { background: var(--card-bg); padding: 1.4rem 1.6rem;
    width: min(22rem, 90vw); box-shadow: 0 8px 32px rgba(0,0,0,.4); }
  .namebox h2 { margin: 0 0 .5rem; font-size: 1.05rem; }
  .namebox p { margin: 0 0 .9rem; font-size: .85rem; color: var(--muted); }
  .namebox input { width: 100%; font: inherit; padding: .45rem .6rem;
    color: var(--fg); background: var(--bg);
    border: 1px solid var(--border); margin-bottom: .9rem; }
  .namebox input:focus { outline: 2px solid var(--accent);
                         border-color: transparent; }
  .namebox button { padding: .45rem 1.2rem; cursor: pointer; border: none;
    background: var(--accent); color: var(--bg); }
</style>
</head>
<body>
<header>
  <button class="ghost" id="home" title="Back to the hwGenie home page">
    ← Home</button>
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
  <button class="ghost" id="whoami" style="display:none"
          title="Your name — recorded on the grades you enter (click to change)"></button>
  <button class="ghost" id="overview"
          title="How the assignment went: averages per part, distribution, highlights">
    Overview</button>
  <button class="ghost" id="gradebook"
          title="Course gradebook: totals, late work, free lates used">
    Gradebook</button>
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
const CFG = __CFG__;             // {folder, grader} from the server
const $ = s => document.querySelector(s);
let S = null;                    // /api/state payload
const P = {};                    // part payload cache: "slug|n" -> payload
let view = "student";            // "student" | "part"
let curSlug = null, curPart = 1;
let pdfSlug = null;              // student shown in the PDF panel

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// every request names its assignment so several tabs/graders can work
// different folders on one server
function withFolder(path) {
  return path + (path.includes("?") ? "&" : "?") +
         "folder=" + encodeURIComponent(CFG.folder);
}

async function api(path, body) {
  const opts = body === undefined ? {} :
    {method: "POST", body: JSON.stringify(body)};
  const r = await fetch(withFolder(path), opts);
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

// ---------------------------------------------------------- grader name --
// On a hosted (grader-only) server each grader identifies themselves
// once; the name rides along with every save for attribution.

function graderName() {
  try { return localStorage.hwgName || null; } catch (e) { return null; }
}

function updateWhoami() {
  if (!CFG.grader) return;
  const b = $("#whoami");
  b.style.display = "";
  b.textContent = "\u{1F464} " + (graderName() || "who?");
}

function promptName(force) {
  if (!CFG.grader || (!force && graderName())) return;
  if ($("#nameov")) return;
  const ov = document.createElement("div");
  ov.id = "nameov";
  ov.innerHTML = `<div class="namebox">
    <h2>Who&rsquo;s grading?</h2>
    <p>Your name is recorded on each grade and comment you enter, so
       everyone can see who graded what.</p>
    <input id="namein" maxlength="60" placeholder="e.g. Alex">
    <button id="namego">Start grading</button></div>`;
  document.body.appendChild(ov);
  const inp = ov.querySelector("#namein");
  inp.value = graderName() || "";
  const go = () => {
    const v = inp.value.trim();
    if (!v) { inp.focus(); return; }
    try { localStorage.hwgName = v; } catch (e) {}
    updateWhoami();
    ov.remove();
  };
  ov.querySelector("#namego").addEventListener("click", go);
  inp.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  inp.focus();
}

$("#whoami").addEventListener("click", () => promptName(true));

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
      const req = {slug, part: n, ...fields};
      const who = graderName();
      if (who) req.by = who;
      const r = await api("/api/grade", req);
      // The client model stays authoritative for in-flight edits; only
      // the derived status (and progress) come back from the server.
      pdata(slug, n).status = r.parts[String(n)].status;
      pdata(slug, n).by = r.parts[String(n)].by;
      setProgress(...r.progress);
      refreshPartChrome(slug, n);
      refreshSidebarCounts(slug, n);
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
  $("#pdfext").href = withFolder("/pdf/" + encodeURIComponent(slug));
  const frag = await pdfFragment(slug, part);
  $("#pdfframe").src =
    withFolder("/pdf/" + encodeURIComponent(slug) + "?v=" + (pdfSeq++)) + frag;
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
function scrollPaneTo(container, el, mode, instant) {
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
  container.scrollTo({top: Math.max(0, top),
                      behavior: instant ? "auto" : "smooth"});
}

// A freshly built pane: jump to el now (null = the top), then again once
// every card's content has arrived — the "loading…" placeholders above
// the target are shorter than the real answers, so the first landing is
// only approximate.  The correction is skipped if the grader has already
// scrolled away in the meantime.
function scrollWhenReady(container, el, mode) {
  const go = () => {
    if (!el || !el.isConnected) container.scrollTop = 0;
    else scrollPaneTo(container, el, mode, true);
    return container.scrollTop;
  };
  const landed = go();
  const cards = [...container.querySelectorAll(".part")];
  Promise.all(cards.map(c => c._ready)).then(() =>
    requestAnimationFrame(() => {
      if (container.scrollTop === landed) go();
    }));
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
    `<div class="nodata">No assignment template on record — put the
     assignment's .tex (the source, so solutions show too) in the
     assignment's build/ folder and re-collect (↻ on the Grading tab).</div>`;
  // the template's solution boxes became tokens; show them as part chips
  // (+ the instructor's solution, collapsed, when the file carries one)
  const isols = stmtData.solutions || {};
  body.querySelectorAll("details.solution").forEach(d => {
    const m = d.textContent.match(/HWGRADERBOX(\d+)/);
    if (!m) return;
    const n = Number(m[1]);
    const div = document.createElement("div");
    div.className = "pbox"; div.dataset.part = n;
    div.textContent = "✎ " + (S.rubric[n - 1] ? S.rubric[n - 1].label : n);
    d.replaceWith(div);
    if (isols[String(n)]) {
      const det = document.createElement("details");
      det.className = "isol"; det.dataset.part = n;
      det.innerHTML = `<summary>Solution</summary>
        <div class="isolbody">${isols[String(n)]}</div>`;
      div.after(det);
    }
  });
  if (Object.keys(isols).length) {
    let on = true;
    try { on = localStorage.getItem("hwg-isol-all") !== "0"; } catch (e) {}
    const all = document.createElement("label");
    all.className = "isolall";
    all.innerHTML = `<input type="checkbox"${on ? " checked" : ""}> show all solutions`;
    const setAll = v => body.querySelectorAll("details.isol").forEach(d => d.open = v);
    setAll(on);
    all.querySelector("input").addEventListener("change", e => {
      setAll(e.target.checked);
      try { localStorage.setItem("hwg-isol-all", e.target.checked ? "1" : "0"); } catch (err) {}
    });
    body.prepend(all);
  }
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
  // a card counts as current once its top is near the visible edge — the
  // part below the sticky nav when that is showing (a jump lands the card
  // 12px under it, so the slack must cover that)
  const nav = document.getElementById("stunav");
  const navH = nav && nav.classList.contains("show") ? nav.offsetHeight : 0;
  const topEdge = main.getBoundingClientRect().top + navH + 60;
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
  const ec = S.rubric[n - 1].ec
    ? ` <span class="ecbadge" title="Extra credit — not part of the
        assignment total; exported separately for Moodle">EC</span>` : "";
  const headLeft = opts && opts.who
    ? `<span class="nm">${esc(slug)}</span>${ec}${badges(u)} ${collab}`
    : `<span class="plabel">${esc(rlabel(n))}</span>${ec}`;
  el.innerHTML = `
    <div class="part-head${opts && opts.who ? " who" : ""}">
      ${headLeft}
      <span class="flags"></span>
      <span class="sp"></span>
      <span class="gby" title="Graded by">${p.by ? esc(p.by) : ""}</span>
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
    refreshSidebarCounts(slug, n);
    queueSave(slug, n, {score: val});
  });
  scoreEl.addEventListener("keydown", ev => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      const next = nextScoreInput(scoreEl);
      if (next) {
        next.focus(); next.select();
        scrollPaneTo($("#main"), next.closest(".part"), "center");
      }
    }
  });

  el._ready = fillContent(el, slug, n);
  renderComments(el, slug, n);
  renderDraft(el, slug, n);
  return el;
}

// Enter in a score box: by-student, the next part down; by-part, the next
// answer still to grade (wrapping round to any skipped above), so a partly
// graded list finishes without hunting.  Falls back to the next box when
// everything is graded.
function nextScoreInput(cur) {
  const all = [...document.querySelectorAll("input.score")];
  const i = all.indexOf(cur);
  if (view !== "part") return all[i + 1];
  const open = s => {
    const p = s.closest(".part");
    return pdata(p.dataset.slug, Number(p.dataset.part)).status !== "graded";
  };
  return all.slice(i + 1).find(open) || all.slice(0, i).find(open) ||
         all[i + 1];
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

// comment boxes grow to fit their text (also when AI feedback lands).
// Part panels are built before they are attached to the page, where a
// textarea has no scrollHeight — measure once it is in the document, or
// a multi-line comment comes back one line tall on every revisit.
function autosize(t, retried) {
  if (!t.isConnected) {
    if (!retried) requestAnimationFrame(() => autosize(t, true));
    return;
  }
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
    refreshSidebarCounts(slug, n);
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
      const p = pdata(slug, n);
      el.classList.toggle("graded", p.status === "graded");
      const gby = el.querySelector(".gby");
      if (gby) gby.textContent = p.by || "";
    });
}

// Sidebar tallies — the student row (by-student) or the part row
// (by-part), whichever is showing.  Runs as soon as a score is typed and
// again when the server confirms the status.
function refreshSidebarCounts(slug, n) {
  refreshSidebarRow(slug);
  refreshSidebarPart(n);
}

function refreshSidebarRow(slug) {
  const row = document.querySelector(
    `#sidebar .stu[data-slug="${CSS.escape(slug)}"] .ct`);
  if (!row) return;
  const done = gradedCount(unit(slug));
  row.textContent = `${done}/${S.n_parts}`;
  row.classList.toggle("done", done === S.n_parts);
}

function partGradedCount(n) {
  return S.units.filter(u => u.parts[String(n)].status === "graded").length;
}

function refreshSidebarPart(n) {
  const row = document.querySelector(`#sidebar .stu[data-n="${n}"] .ct`);
  if (!row) return;
  const done = partGradedCount(n);
  row.textContent = `${done}/${S.units.length}`;
  row.classList.toggle("done", done === S.units.length);
}

// ------------------------------------------------------------ badges/head --

function badges(u) {
  let b = "";
  const L = u.late;
  if (L && L.is_late) {
    const cls = L.hold ? " hold" : (L.action === "free" || L.action === "waive"
      || (L.action === "extension" && !L.penalty_pts)) ? " ok" : "";
    b += ` <span class="badge late${cls}" title="Submitted ${esc(L.submitted_text)}
      — ${esc(L.label)}">${esc(L.late_text)} late</span>`;
  }
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
  return h + lateBlock(u) + "</div>";
}

// ------------------------------------------------------------- late work --

const LATE_ACTIONS = [
  ["auto", "Follow the policy"],
  ["free", "Use the free late"],
  ["apply", "Apply the penalty"],
  ["waive", "Waive (grace)"],
  ["extension", "Extension to…"],
  ["discuss", "Hold — discuss with student"],
];

function lateBlock(u) {
  const L = u.late;
  if (!L || !L.submitted) return "";
  const due = S.late && S.late.due;
  let h = `<div class="latebar${L.is_late ? " islate" : ""}" data-slug="${esc(u.slug)}">
    <span>Submitted <b>${esc(L.submitted_text)}</b></span>`;
  if (L.resubmitted) h += `<span>· re-uploaded ${esc(L.resubmitted_text)}</span>`;
  if (!due) h += `<span class="muted">· no due date set (rubric.yml
    <code>due:</code>)</span>`;
  else if (L.is_late)
    h += `<span>· <b class="lt">${esc(L.late_text)} late</b></span>
      <span class="verdict ${esc(L.action)}">${esc(L.label)}</span>`;
  else h += `<span class="muted">· on time</span>`;
  if (L.is_late && !CFG.grader) {
    const d = L.decision || {action: "auto"};
    const opts = LATE_ACTIONS.map(([v, t]) =>
      `<option value="${v}"${d.action === v ? " selected" : ""}>${t}</option>`).join("");
    h += `<span class="sp"></span>
      <select class="lateact" title="Instructor decision">${opts}</select>
      <input class="lateext" type="text" placeholder="2026-09-07 23:59"
        title="New deadline (course time)" value="${esc((d.extension || "").replace("T", " ").slice(0, 16))}"
        ${d.action === "extension" ? "" : "hidden"}>
      <input class="latenote" type="text" placeholder="note (optional)"
        value="${esc(d.note || "")}">
      <button class="latesave ghost">Save</button>`;
  } else if (L.is_late && L.decision && L.decision.note) {
    h += `<span class="muted">· ${esc(L.decision.note)}</span>`;
  }
  if (L.notes && L.notes.length)
    h += `<div class="latenotes">${L.notes.map(esc).join("<br>")}</div>`;
  return h + "</div>";
}

document.addEventListener("change", e => {
  if (!e.target.classList.contains("lateact")) return;
  const bar = e.target.closest(".latebar");
  bar.querySelector(".lateext").hidden = e.target.value !== "extension";
});

document.addEventListener("click", async e => {
  if (!e.target.classList.contains("latesave")) return;
  const bar = e.target.closest(".latebar");
  const slug = bar.dataset.slug;
  const body = {slug, action: bar.querySelector(".lateact").value,
                note: bar.querySelector(".latenote").value,
                extension: bar.querySelector(".lateext").value || null};
  try {
    const r = await api("/api/late", body);
    unit(slug).late = r.late;
    if (view === "student" && curSlug === slug) showStudent(slug);
    else renderSidebarStudents();
    notice(`Late decision saved for ${slug}: ${r.late.label}`);
  } catch (err) { notice("Could not save: " + err.message); }
});

// -------------------------------------------------------- by-student view --

function renderSidebarStudents() {
  const sb = $("#sidebar");
  sb.innerHTML = S.units.map(u => {
    const done = gradedCount(u);
    const star = u.tex_source === "reconstructed" ? "*" :
                 (!u.tex ? "†" : "");
    const late = u.late && u.late.is_late
      ? ` <span class="ltdot${u.late.hold ? " hold" : ""}" title="${esc(u.late.late_text)} late — ${esc(u.late.label)}">L</span>` : "";
    return `<div class="stu${u.slug === curSlug ? " active" : ""}"
      data-slug="${esc(u.slug)}">
      <span class="nm">${esc(u.slug)}${star}${late}</span>
      <span class="ct${done === S.n_parts ? " done" : ""}">${done}/${S.n_parts}</span>
    </div>`;
  }).join("") + `<div style="padding:.5rem .7rem;font-size:.72rem;
    color:var(--muted)">* reconstructed tex &nbsp; † no tex &nbsp;
    <span class="ltdot">L</span> late</div>`;
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
  // keep the grader's place: land on the part they were just grading
  // (part 1 = the top, so the student's header stays in view)
  scrollWhenReady(main, activePart > 1
    ? pane.querySelector(`.part[data-part="${activePart}"]`) : null, "start");
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
    const done = partGradedCount(n);
    return `<div class="stu${n === curPart ? " active" : ""}" data-n="${n}">
      <span class="nm">${esc(rp.label)}${rp.ec ? " (EC)" : ""}</span>
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
         ${esc(rp.label)}${rp.ec ? " (EC)" : ""}</option>`).join("")}</select>
      <button class="ghost" id="pnext"
        ${n >= S.n_parts ? "disabled" : ""}>→</button>
      <span class="ptext">${mx === null ? "" : "out of " + mx + " points"}
        — Enter moves to the next ungraded</span>
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
  // land on the first answer still to grade (the top if that's the first)
  const open = [...cards.querySelectorAll(".part")].find(p =>
    pdata(p.dataset.slug, n).status !== "graded");
  setActiveCard(open || cards.querySelector(".part"), false);
  updatePanelBtns();
  scrollWhenReady(main, open && open !== cards.firstElementChild ? open : null,
                  "start");
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
  location.href = "/grading?pick=1";
});

$("#home").addEventListener("click", async () => {
  await settleSaves();
  if (saveState() !== "clean") { updateSaveStat(); return; }
  location.href = "/";
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
      (s.extra_credit != null ?
        `; extra-credit CSV with ${s.extra_credit} rows` : "") +
      (s.late ? `; ${s.late.late} late` +
        (s.late.penalized ? `, ${s.late.penalized} penalized` : "") +
        (s.late.free ? `, ${s.late.free} used a free late` : "") +
        (s.late.held && s.late.held.length ?
          `, ${s.late.held.length} HELD (${s.late.held.join(", ")})` : "") +
        `; course gradebook updated` : "") +
      `. Files are in ${s.out}` +
      (s.warnings && s.warnings.length ? ` — ${s.warnings.join("; ")}` : "") +
      ". Click to dismiss.");
  }, 1000);
});

// ------------------------------------------------------------------ init --

(async function init() {
  $("#gradebook").addEventListener("click", () => {
    location.href = "/gradebook?folder=" + encodeURIComponent(CFG.folder);
  });
  $("#overview").addEventListener("click", () => {
    location.href = "/overview?folder=" + encodeURIComponent(CFG.folder);
  });
  if (CFG.grader) {
    $("#export").style.display = "none";
    $("#gradebook").style.display = "none";
    $("#overview").style.display = "none";
    $("#home").style.display = "none";
    updateWhoami();
    promptName(false);
  }
  S = await api("/api/state");
  setProgress(...S.progress);
  $("#foldname").textContent = S.folder.split("/").filter(Boolean).slice(-2).join("/");
  if (S.late && S.late.due_text)
    $("#foldname").title = `Due ${S.late.due_text} (${S.late.timezone})`;
  if (S.late && S.late.error) notice("Late policy: " + S.late.error);
  document.title = `hwGenie — ${S.folder.split("/").pop()}`;
  updateSaveStat();
  // ?student=<slug> deep-links to one submission (overview highlights)
  const want = new URLSearchParams(location.search).get("student");
  if (want && S.units.some(u => u.slug === want)) curSlug = want;
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
  .sub a { color: var(--accent); text-decoration: none;
           margin-left: .6rem; font-size: .88rem; }
  .sub a:hover { background: var(--hover-bg); }
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
  h2.sechead {
    font-size: 1rem; letter-spacing: .06em; color: var(--fg);
    border-top: 1px solid var(--border);
    margin: 2.2rem 0 .2rem; padding-top: 1.4rem;
  }
  h2.sechead:first-of-type { border-top: none; margin-top: .4rem;
                             padding-top: 0; }
  .remhead { display: flex; align-items: baseline; gap: .8rem;
             margin: .8rem 0 .6rem; }
  .remhead .grow { flex: 1; }
  .remhead a { color: var(--accent); text-decoration: none;
               font-size: .85rem; white-space: nowrap; }
  .remhead a:hover { background: var(--hover-bg); }
  button.ghost {
    font: inherit; font-size: .8rem; padding: .25rem .7rem;
    cursor: pointer; color: var(--fg); background: transparent;
    border: 1px solid var(--border);
  }
  button.ghost:hover { background: var(--hover-bg); }
  button.ghost:disabled { opacity: .45; cursor: default; }
  .remrow {
    display: flex; align-items: baseline; gap: .8rem;
    background: var(--card-bg); padding: .55rem .8rem; margin: 0 0 .45rem;
  }
  .remrow .path { flex: 1; overflow: hidden; text-overflow: ellipsis;
                  white-space: nowrap; }
  .remrow .meta { color: var(--muted); font-size: .8rem;
                  white-space: nowrap; }
  .remrow .done { color: var(--sol-accent); font-weight: 600; }
  #rempush select {
    flex: 1; font: inherit; padding: .45rem .6rem; color: var(--fg);
    background: var(--card-bg); border: 1px solid var(--border);
  }
  #remlog { white-space: pre-wrap; font-family: ui-monospace, monospace;
            font-size: .72rem; margin-top: .6rem; }
  #clog { white-space: pre-wrap; font-family: ui-monospace, monospace;
          font-size: .72rem; margin-top: .6rem; }
  .row .recollect { margin-left: .5rem; padding: 0 .4rem; font-size: .9rem; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill,
           minmax(15rem, 1fr)); gap: .9rem; margin-top: .5rem; }
  .card { background: var(--card-bg); padding: 1rem 1.1rem 1rem;
          border-top: 3px solid var(--accent); }
  .card .cardlink { display: block; font-weight: 700; font-size: 1.05rem;
          color: var(--accent); text-decoration: none; margin-bottom: .3rem; }
  .card a.cardlink:hover { text-decoration: underline; }
  .card p { margin: 0 0 .5rem; color: var(--muted); font-size: .85rem; }
  .card .mini a { display: block; font-size: .85rem; color: var(--fg);
          text-decoration: none; padding: .15rem 0; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; }
  .card .mini a:hover { color: var(--accent); }
  .card .mini.muted { font-size: .8rem; color: var(--muted); }
  #back a { color: var(--accent); text-decoration: none; }
  .filebtn { padding: .45rem .9rem; cursor: pointer; background: var(--accent);
             color: var(--bg); font: inherit; }
  #nlist .f { display: block; } #nlist .f b { font-weight: 600; }
  #ncourse { flex: 0 0 12rem; font: inherit; padding: .45rem .6rem;
             color: var(--fg); background: var(--card-bg);
             border: 1px solid var(--border); }
</style>
</head>
<body>
__NAV__
<main>
  <div id="hub" hidden>
    <p class="sub">Grading &mdash; pick a task.</p>
    <div class="cards">
      <div class="card">
        <a class="cardlink" href="/grading?pick=1&view=grade">Grade &rarr;</a>
        <p>Open a collected assignment in hwGrader.</p>
        <div id="hubrecent" class="mini"></div>
      </div>
      <div class="card">
        <a class="cardlink" href="/grading?pick=1&view=collect">Collect from
          Moodle &rarr;</a>
        <p>Turn a Moodle download into a grading folder, or pull in late
          work with &#x21bb; on an assignment.</p>
      </div>
      <div class="card">
        <span class="cardlink">Gradebook</span>
        <p>Every student &times; every assignment: progress, totals after
          the late policy, free lates used.</p>
        <div id="hubcourses" class="mini"></div>
      </div>
      <div class="card">
        <a class="cardlink" href="/grading?pick=1&view=remote">External
          grading &rarr;</a>
        <p>Push assignments to the graders&rsquo; server and pull their
          grades back.</p>
        <div id="hubremote" class="mini muted"></div>
      </div>
      <div class="card">
        <a class="cardlink" href="/grading/howto">How-to &rarr;</a>
        <p>The Moodle round trip, rubrics, extra credit, late work.</p>
      </div>
    </div>
  </div>
  <p class="sub" id="back" hidden><a href="/grading?pick=1">&larr; Grading</a>
    <span id="viewtitle"></span></p>
  <div data-view="grade">
  <div id="sec-recents">
    <h2>Recent</h2>
    <div id="recents"><span class="none">nothing yet</span></div>
  </div>
  <h2>Found in <span id="root"></span></h2>
  <div id="found"><span class="none">scanning…</span></div>
  </div>
  <div id="sec-manual" data-view="grade">
    <h2>Somewhere else</h2>
    <div class="manual">
      <input id="path" spellcheck="false"
        placeholder="/path/to/grading-folder or moodle-download.zip">
      <button id="open">Open</button>
    </div>
    <p class="hint">Paste a grading folder (made by <code>hwgenie
    collect</code>) or a Moodle &ldquo;Download all submissions&rdquo; .zip
    &mdash; a zip is collected into a folder next to it first.</p>
  </div>
  <div id="sec-new" data-view="collect">
    <h2 class="sechead">New assignment</h2>
    <div class="manual">
      <select id="ncourse" title="Course folder in the grading lab"></select>
      <input id="ncoursenew" spellcheck="false" style="flex:0 0 12rem"
        placeholder="new course folder, e.g. math301" hidden>
      <input id="nname" spellcheck="false" style="flex:0 0 9rem"
        placeholder="ps01">
      <button id="ncreate">Create</button>
    </div>
    <p class="hint">Makes <code>&lt;lab&gt;/&lt;course&gt;/&lt;ps&gt;/</code>
      with <code>moodle-raw/</code> and <code>build/</code>. Picking an
      existing assignment just reopens it so you can add files.</p>
    <div id="nfiles" hidden>
      <p class="hint" id="npath"></p>
      <div class="manual">
        <label class="filebtn">Add files…
          <input type="file" id="nupload" multiple accept=".zip,.csv,.tex" hidden>
        </label>
        <input id="ndue" spellcheck="false" style="flex:0 0 11rem"
          placeholder="due: 2026-09-11 23:59" title="Deadline in course time">
        <button id="ncollect" disabled>Collect now</button>
      </div>
      <p class="hint">From Moodle: the <b>Download all submissions</b> zip and
        the <b>grading worksheet</b> csv; from the course repo: the
        assignment&rsquo;s <b>.tex source</b> (solutions show to graders).
        Zips and csvs land in <code>moodle-raw/</code>, tex in
        <code>build/</code>.</p>
      <div id="nlist" class="hint"></div>
    </div>
  </div>
  <div id="sec-collect" data-view="collect">
    <h2 class="sechead">Collect from Moodle</h2>
    <div class="manual">
      <input id="czip" spellcheck="false"
        placeholder="/path/to/<assignment>/moodle-raw/<download>.zip">
      <input id="cdue" spellcheck="false" style="flex:0 0 11rem"
        placeholder="due: 2026-09-04 23:59" title="Deadline in course time
(optional; kept from a previous collect if blank)">
      <button id="collect">Collect</button>
    </div>
    <p class="hint">Put the zip and the <code>Grades-&hellip;.csv</code>
    worksheet in <code>&lt;assignment&gt;/moodle-raw/</code> and the
    submission template in <code>&lt;assignment&gt;/build/</code>; the
    grading folder becomes <code>&lt;assignment&gt;/grading/</code>.
    Re-collecting an existing folder (or the &#x21bb; on a row above)
    adds late students and re-uploads without touching graded work.</p>
    <pre id="clog" class="hint" style="display:none"></pre>
  </div>
  <div id="err"></div>
  <div id="sec-remote" data-view="remote" style="display:none">
    <h2 class="sechead">External Grading</h2>
    <div class="remhead">
      <span id="remstat" class="none">checking the grading server…</span>
      <span class="grow"></span>
      <a id="remurl" target="_blank" style="display:none">Open grading
        site ↗</a>
      <button class="ghost" id="remrefresh">Refresh</button>
    </div>
    <div id="remrows"></div>
    <div class="manual" id="rempush" style="display:none">
      <select id="pushsel"></select>
      <button id="pushbtn">Push to server</button>
    </div>
    <p class="hint" id="remhint" style="display:none">Push mirrors a local
    grading folder to the server, grades included &mdash; pull first if
    the graders have work there you haven&rsquo;t fetched. Pull copies
    the graders&rsquo; grades into the matching local folder (it never
    deletes anything local).</p>
    <div id="remlog" class="hint" style="display:none"></div>
  </div>
</main>
<script>
"use strict";
const CFG = __CFG__;
const $ = s => document.querySelector(s);

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function gotoFolder(p) {
  location.href = "/grading?folder=" + encodeURIComponent(p);
}

async function openPath(p) {
  $("#err").style.display = "none";
  if (!p.toLowerCase().endsWith(".zip")) {
    gotoFolder(p);
    return;
  }
  try {
    // a zip must be collected server-side first
    const r = await fetch("/api/open",
      {method: "POST", body: JSON.stringify({path: p})});
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "could not open");
    gotoFolder(data.folder);
  } catch (e) {
    $("#err").textContent = e.message;
    $("#err").style.display = "block";
  }
}

// "…/grading-lab/math221/ps01/grading" -> "math221 / ps01"
function label(p) {
  const parts = p.split("/").filter(Boolean);
  const i = parts[parts.length - 1] === "grading" ? parts.length - 2
                                                   : parts.length - 1;
  return (i > 0 ? parts[i - 1] + " / " : "") + (parts[i] || p);
}
// the course folder above an assignment ("…/grading-lab/math221")
function courseOf(p) {
  const parts = p.split("/").filter(Boolean);
  const i = parts[parts.length - 1] === "grading" ? parts.length - 2
                                                   : parts.length - 1;
  return i > 0 ? "/" + parts.slice(0, i).join("/") : null;
}

function rows(el, items, rootPrefix) {
  if (!items.length) return;
  el.innerHTML = items.map(f => {
    const rel = label(f.path);
    const meta = [f.units !== null && f.units !== undefined ?
                  f.units + " submissions" : "", f.created]
                 .filter(Boolean).join(" · ");
    const rc = !CFG.grader && f.units !== null && f.units !== undefined
      ? `<button class="ghost recollect" title="Collect again from the newest
zip in this assignment's moodle-raw/ — adds late students and re-uploads,
never touches graded work">&#x21bb;</button>` : "";
    return `<div class="row" data-p="${esc(f.path)}" title="${esc(f.path)}">
      <span class="path">${esc(rel || f.path)}</span>
      <span class="meta">${esc(meta)}</span>${rc}</div>`;
  }).join("");
  el.querySelectorAll(".row").forEach(r =>
    r.addEventListener("click", () => gotoFolder(r.dataset.p)));
  el.querySelectorAll(".recollect").forEach(b =>
    b.addEventListener("click", e => {
      e.stopPropagation();
      runCollect({folder: b.closest(".row").dataset.p});
    }));
}

async function runCollect(body) {
  const log = $("#clog");
  log.style.display = "block";
  log.textContent = "Collecting…";
  $("#err").style.display = "none";
  try {
    const r = await fetch("/api/collect",
      {method: "POST", body: JSON.stringify(body)});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "collect failed");
    const head = (d.update ? "Updated" : "Collected") +
      ` ${d.units} submissions in ${d.folder}` +
      (d.update ? ` — ${d.added.length} added, ${d.replaced.length} replaced, ` +
        `${d.resubmitted.length} re-uploaded after grading (kept), ` +
        `${d.unchanged} unchanged` : "") +
      `\n  zip: ${d.zip}` +
      `\n  template: ${d.template || "none — no problem pane / box check"}` +
      `\n  times from: ${d.worksheet || "zip file dates (no worksheet found)"}` +
      `\n  due: ${d.due || "not set — nothing will be flagged late"}` +
      (d.late.length ? `\n  late: ${d.late.join(", ")}` : "") +
      (d.skipped.length ? `\n  skipped: ${d.skipped.join("; ")}` : "");
    log.textContent = head + (d.lines.length ? "\n\n" + d.lines.join("\n") : "");
    const a = document.createElement("a");
    a.href = "/grading?folder=" + encodeURIComponent(d.folder);
    a.textContent = "\nOpen it →";
    log.appendChild(a);
    const s = await (await fetch("/api/scan")).json();
    rows($("#found"), s.folders, s.root);
    if (NEW) { const r = await (await fetch("/api/assignment/new",
      {method: "POST", body: JSON.stringify({course: NEW.course, name: NEW.name})})).json();
      if (r.ok) renderNewFiles(r.files); }
  } catch (e) {
    log.textContent = "";
    log.style.display = "none";
    $("#err").textContent = e.message;
    $("#err").style.display = "block";
  }
}

const VIEW_TITLES = {grade: "Grade", collect: "Collect from Moodle",
                     remote: "External grading"};

// ------------------------------------------------------- new assignment --

let LAB = null, NEW = null;   // lab layout; the assignment being set up

async function loadLab() {
  try { LAB = await (await fetch("/api/lab")).json(); }
  catch (e) { LAB = {root: "", courses: []}; }
  const sel = $("#ncourse");
  sel.innerHTML = LAB.courses.map(c =>
    `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("") +
    `<option value="__new__">new course folder…</option>`;
  if (!LAB.courses.length) sel.value = "__new__";
  $("#ncoursenew").hidden = sel.value !== "__new__";
}

function showNew(r) {
  NEW = r;
  $("#nfiles").hidden = false;
  $("#npath").innerHTML = (r.existed ? "Reopened " : "Created ") +
    `<code>${esc(r.path)}</code>`;
  renderNewFiles(r.files);
}

function renderNewFiles(files) {
  const raw = files["moodle-raw"] || [], build = files["build"] || [];
  const zip = raw.find(f => f.toLowerCase().endsWith(".zip"));
  const csv = raw.find(f => f.toLowerCase().endsWith(".csv"));
  const tex = build.find(f => f.toLowerCase().endsWith(".tex"));
  const li = (ok, what, name) =>
    `<span class="f">${ok ? "✓" : "○"} ${what}: ${name ? "<b>" + esc(name) + "</b>" : "<i>missing</i>"}</span>`;
  $("#nlist").innerHTML = li(!!zip, "Moodle zip", zip) + li(!!csv, "worksheet", csv) +
    li(!!tex, "assignment .tex", tex) +
    (files.collected ? `<span class="f">✓ already collected — “Collect now” updates it</span>` : "");
  $("#ncollect").disabled = !zip;
  NEW.zip = zip ? NEW.path + "/moodle-raw/" + zip : null;
}

async function uploadFiles(list) {
  for (const f of list) {
    $("#npath").innerHTML += ` <span class="muted">uploading ${esc(f.name)}…</span>`;
    const q = `course=${encodeURIComponent(NEW.course)}&name=${encodeURIComponent(NEW.name)}` +
              `&filename=${encodeURIComponent(f.name)}`;
    try {
      const r = await fetch("/api/assignment/upload?" + q, {method: "POST", body: f});
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "upload failed");
      renderNewFiles(d.files);
    } catch (e) {
      $("#err").textContent = e.message; $("#err").style.display = "block";
    }
  }
  $("#npath").innerHTML = `<code>${esc(NEW.path)}</code>`;
}

function wireNew() {
  $("#ncourse").addEventListener("change", () => {
    $("#ncoursenew").hidden = $("#ncourse").value !== "__new__";
    if (!$("#ncoursenew").hidden) $("#ncoursenew").focus();
  });
  $("#ncreate").addEventListener("click", async () => {
    const course = $("#ncourse").value === "__new__"
      ? $("#ncoursenew").value.trim() : $("#ncourse").value;
    const name = $("#nname").value.trim();
    if (!course) { $("#ncoursenew").focus(); return; }
    if (!name) { $("#nname").focus(); return; }
    $("#err").style.display = "none";
    try {
      const r = await fetch("/api/assignment/new",
        {method: "POST", body: JSON.stringify({course, name})});
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "could not create");
      showNew(d);
      await loadLab();
      $("#ncourse").value = course;
      $("#ncoursenew").hidden = true;
    } catch (e) {
      $("#err").textContent = e.message; $("#err").style.display = "block";
    }
  });
  $("#nname").addEventListener("keydown", e => {
    if (e.key === "Enter") $("#ncreate").click();
  });
  $("#nupload").addEventListener("change", e => {
    uploadFiles([...e.target.files]); e.target.value = "";
  });
  $("#ncollect").addEventListener("click", () => {
    if (NEW && NEW.zip) runCollect({zip: NEW.zip, due: $("#ndue").value.trim()});
  });
}

function showView(view) {
  // grader-only servers: just the list; instructors: hub or one view
  const v = CFG.grader ? "grade" : (VIEW_TITLES[view] ? view : null);
  document.querySelectorAll("[data-view]").forEach(el => {
    el.hidden = (v !== el.dataset.view);
    if (el.id === "sec-remote") el.style.display = v === "remote" ? "" : "none";
  });
  $("#hub").hidden = !!v || CFG.grader;
  $("#back").hidden = !v || CFG.grader;
  $("#viewtitle").textContent = v ? "· " + VIEW_TITLES[v] : "";
  document.title = "hwGenie — " + (v ? VIEW_TITLES[v] : "Grading");
}

(async function init() {
  const params = new URLSearchParams(location.search);
  showView(params.get("view"));
  if (CFG.grader) {
    $("#sec-manual").style.display = "none";
    $("#sec-collect").style.display = "none";
    $("#sec-recents").style.display = "none";
  }
  $("#collect").addEventListener("click", () => {
    const zip = $("#czip").value.trim();
    if (!zip) { $("#czip").focus(); return; }
    runCollect({zip, due: $("#cdue").value.trim()});
  });
  $("#czip").addEventListener("keydown", e => {
    if (e.key === "Enter") $("#collect").click();
  });
  if (!CFG.grader) { wireNew(); loadLab(); }
  const err = params.get("err");
  if (err) {
    $("#err").textContent = err;
    $("#err").style.display = "block";
  }
  const s = await (await fetch("/api/scan")).json();
  $("#root").textContent = s.root;
  rows($("#found"), s.folders, s.root);
  if (!s.folders.length)
    $("#found").innerHTML = '<span class="none">no grading folders ' +
      'found' + (CFG.grader ? '' : ' — collect a Moodle zip below') +
      '</span>';
  if (!CFG.grader) {
    rows($("#recents"), s.recents.map(p => ({path: p})), null);
    SCAN = s;
    // hub: the most recent assignments, and one gradebook per course
    const recent = (s.recents.length ? s.recents : s.folders.map(f => f.path))
      .slice(0, 5);
    $("#hubrecent").innerHTML = recent.map(p =>
      `<a href="/grading?folder=${encodeURIComponent(p)}" title="${esc(p)}">
        ${esc(label(p))}</a>`).join("") ||
      '<span class="none">nothing collected yet</span>';
    const courses = [...new Set(s.folders.map(f => courseOf(f.path))
      .filter(Boolean))].sort();
    $("#hubcourses").innerHTML = courses.map(c =>
      `<a href="/gradebook?course=${encodeURIComponent(c)}" title="${esc(c)}">
        ${esc(c.split("/").pop())} &rarr;</a>`).join("") ||
      '<span class="none">no courses found under ' + esc(s.root) + '</span>';
    fillPushSel();
    loadRemote(true);
  }
})();

// ------------------------------------------------------ external grading --

let SCAN = null, remTimer = null;

function srvName(p) {
  const parts = p.split("/").filter(Boolean);
  let b = parts[parts.length - 1] || p;
  if (b === "grading" && parts.length > 1) b = parts[parts.length - 2];
  return b;
}

function fillPushSel() {
  const folders = (SCAN && SCAN.folders) || [];
  $("#pushsel").innerHTML = folders.map(f => {
    const rel = f.path.startsWith(SCAN.root)
      ? f.path.slice(SCAN.root.length).replace(/^\//, "") : f.path;
    return `<option value="${esc(f.path)}">${esc(srvName(f.path))}` +
           `  —  ${esc(rel)}</option>`;
  }).join("");
  $("#rempush").style.display = folders.length ? "" : "none";
}

function renderRemote(st) {
  setTimeout(() => {
    const h = $("#hubremote"), r = $("#remstat"), u = $("#remurl");
    if (h && r) h.innerHTML = esc(r.textContent) + (u && u.href && u.style.display !== "none"
      ? ` · <a href="${esc(u.href)}" target="_blank">open grading site ↗</a>` : "");
  }, 0);
  const stat = $("#remstat");
  $("#remurl").style.display = st.url ? "" : "none";
  if (st.url) $("#remurl").href = st.url;
  $("#remhint").style.display = st.configured ? "" : "none";
  const busy = !!st.running;
  $("#remrefresh").disabled = busy;
  $("#pushbtn").disabled = busy;
  document.querySelectorAll("#remrows button").forEach(
    b => b.disabled = busy);
  if (!st.configured) {
    stat.textContent = "No grading server configured — see " +
      "~/.hwgenie/remote.json";
    return;
  }
  stat.style.color = st.error && !st.running ? "var(--alert)" : "";
  if (st.running)
    stat.textContent = `${st.running} running… (server: ${st.host})`;
  else if (st.error)
    stat.textContent = st.error;
  else
    stat.textContent = `server: ${st.host}` + (st.age == null ? "" :
      " · updated " + (st.age < 90 ? st.age + "s"
                            : Math.round(st.age / 60) + " min") + " ago");
  const log = $("#remlog");
  const showLog = (st.running || st.error) && st.log.length;
  log.style.display = showLog ? "" : "none";
  log.textContent = st.log.slice(-6).join("\n");
  const rows = $("#remrows");
  if (st.assignments == null) { rows.innerHTML = ""; return; }
  if (!st.assignments.length) {
    rows.innerHTML = '<span class="none">no assignments on the server ' +
      'yet — push one below</span>';
    return;
  }
  rows.innerHTML = st.assignments.map(a => {
    if (a.error)
      return `<div class="remrow"><span class="path">${esc(a.name)}</span>
        <span class="meta">${esc(a.error)}</span></div>`;
    const done = a.total && a.graded === a.total;
    return `<div class="remrow"><span class="path">${esc(a.name)}</span>
      <span class="meta">${a.units} submissions · <span class="${
        done ? "done" : ""}">${a.graded}/${a.total} graded</span>${
        a.created ? " · " + esc(a.created) : ""}</span>
      <button class="ghost rpull" data-name="${esc(a.name)}">Pull
        grades</button>
      <button class="ghost rview" data-name="${esc(a.name)}"
        title="How the assignment went (from the grades pulled so far)">
        Overview</button></div>`;
  }).join("");
  rows.querySelectorAll(".rpull").forEach(b =>
    b.addEventListener("click", () => pullRemote(b.dataset.name)));
  rows.querySelectorAll(".rview").forEach(b =>
    b.addEventListener("click", () => {
      const match = localMatch(b.dataset.name);
      if (match)
        location.href = "/overview?folder=" + encodeURIComponent(match.path);
    }));
}

// the local grading folder a server assignment name corresponds to
function localMatch(name) {
  const folders = (SCAN && SCAN.folders) || [];
  const match = folders.find(f => srvName(f.path) === name);
  if (!match) {
    $("#err").textContent = `No local grading folder matches "${name}" ` +
      "— collect the assignment locally first.";
    $("#err").style.display = "block";
  }
  return match || null;
}

async function loadRemote(kick) {
  let st;
  try { st = await (await fetch("/api/remote")).json(); }
  catch (e) { return; }
  if (st.configured && kick && !st.running && st.assignments == null) {
    remotePost("/api/remote/scan", {});
    return;
  }
  renderRemote(st);
  if (st.running && !remTimer)
    remTimer = setInterval(async () => {
      let s2;
      try { s2 = await (await fetch("/api/remote")).json(); }
      catch (e) { return; }
      renderRemote(s2);
      if (!s2.running) { clearInterval(remTimer); remTimer = null; }
    }, 1000);
}

async function remotePost(path, body) {
  $("#err").style.display = "none";
  try {
    const r = await fetch(path,
      {method: "POST", body: JSON.stringify(body)});
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "request failed");
  } catch (e) {
    $("#err").textContent = e.message;
    $("#err").style.display = "block";
    return;
  }
  loadRemote(false);
}

function pullRemote(name) {
  const match = localMatch(name);
  if (!match) return;
  if (!confirm(`Pull the graders' grades for "${name}" into\n` +
      `${match.path}?\n\nServer grade files overwrite local ones with ` +
      "the same name (nothing local is deleted)."))
    return;
  remotePost("/api/remote/pull", {path: match.path});
}

$("#remrefresh").addEventListener("click",
  () => remotePost("/api/remote/scan", {}));
$("#pushbtn").addEventListener("click", () => {
  const p = $("#pushsel").value;
  if (!p) return;
  if (!confirm(`Push "${srvName(p)}" to the grading server?\n\nThis ` +
      "mirrors the local folder — grades included — over the " +
      "server copy. If the graders have entered work you haven't " +
      "pulled yet, pull first."))
    return;
  remotePost("/api/remote/push", {path: p});
});

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
