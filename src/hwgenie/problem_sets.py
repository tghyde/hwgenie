r"""Problem set manager for the hwGenie app (``/problem-sets``).

The semester's courses are *pinned* (remembered in
``~/.hwgenie/problem-sets.json``); each pinned course gets a table of the
problem sets in its local clone, with the two publishing switches that
live in the source ``.tex`` — ``\hwrelease`` (is the assignment on the
site at all) and ``\hwsolutions`` (are the solutions posted) — plus the
``\hwdue`` line.

Edits are held in the browser and written to disk only when Push runs:
one job per course that pulls, rewrites the sources, commits and pushes,
so the site rebuild is what actually publishes the change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .course_admin import _run, find_local_clones, parse_course_yml
from .metadata import MetadataError, parse_metadata

PINNED_PATH = Path.home() / ".hwgenie" / "problem-sets.json"

# The \hw... metadata commands, in the order they are written into a
# source file when one has to be inserted.
FIELD_ORDER = ("type", "number", "title", "due", "release", "solutions")



def _run_stdin(cmd: list[str], cwd: Path, text: str
               ) -> subprocess.CompletedProcess:
    """``_run`` for the one command that reads paths on stdin."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(cmd, cwd=cwd, input=text, capture_output=True,
                          text=True, timeout=30, env=env)


# ------------------------------------------------- editing a source --

def _cmd_re(field: str) -> re.Pattern:
    return re.compile(r"\\hw" + field + r"\s*\{([^{}]*)\}")


def _v2_block_span(text: str) -> tuple[int, int] | None:
    """(start, end) char offsets of the ``%===hwgenie===`` block body."""
    from .metadata import V2_END, V2_START
    lines = text.splitlines(keepends=True)
    pos = 0
    start = None
    for line in lines:
        bare = line.rstrip("\n")
        if start is None:
            if V2_START.match(bare):
                start = pos + len(line)
        elif V2_END.match(bare):
            return (start, pos)
        pos += len(line)
    return None


def set_hw_field(text: str, field: str, value: str) -> str:
    """Return ``text`` with metadata ``field`` set to ``value``.

    Prefers the ``\\hwfield{...}`` command (the current source format,
    trailing comments preserved); falls back to the ``%===hwgenie===``
    block when a file uses that instead; otherwise inserts a new command
    line near the other metadata.  An empty ``value`` deletes the setting
    rather than leaving an empty pair of braces behind.
    """
    m = _cmd_re(field).search(text)
    if m:
        if not value:
            return _delete_line_at(text, m.start())
        return text[:m.start(1)] + value + text[m.end(1):]

    span = _v2_block_span(text)
    if span is not None:
        start, end = span
        body = text[start:end]
        key = re.compile(r"(?mi)^(%\s*" + field + r"\s*=[ \t]*)(.*)$")
        km = key.search(body)
        if km:
            if not value:
                body = _delete_line_at(body, km.start())
            else:
                body = body[:km.start(2)] + value + body[km.end(2):]
        elif value:
            body = body + f"% {field:<9} = {value}\n"
        return text[:start] + body + text[end:]

    if not value:
        return text
    return _insert_command(text, field, value)


def _delete_line_at(text: str, pos: int) -> str:
    """Drop the whole physical line containing offset ``pos``."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    end = len(text) if end < 0 else end + 1
    return text[:start] + text[end:]


def _insert_command(text: str, field: str, value: str) -> str:
    """Add ``\\hwfield{value}`` on its own line, in canonical order."""
    line = f"\\hw{field}{{{value}}}\n"
    try:
        rank = FIELD_ORDER.index(field)
    except ValueError:
        rank = len(FIELD_ORDER)

    after = None    # insert after the last earlier-ranked command…
    before = None   # …or before the first later-ranked one
    for other in FIELD_ORDER:
        m = _cmd_re(other).search(text)
        if not m:
            continue
        eol = text.find("\n", m.end())
        eol = len(text) if eol < 0 else eol + 1
        if FIELD_ORDER.index(other) < rank:
            after = eol
        elif before is None:
            before = text.rfind("\n", 0, m.start()) + 1
    at = after if after is not None else before
    if at is None:
        # no metadata commands at all: sit below the preamble's hook
        anchor = re.search(r"^\\usepackage\{hwgenie\}.*\n", text, re.M) \
            or re.search(r"^\\documentclass.*\n", text, re.M)
        at = anchor.end() if anchor else 0
        line = "\n" + line if anchor else line
    return text[:at] + line + text[at:]


def _yesno(on: bool) -> str:
    return "yes" if on else "no"


def apply_edits(text: str, edit: dict) -> str:
    """Apply one row's pending edit dict to a source file's text."""
    if "released" in edit:
        text = set_hw_field(text, "release", _yesno(bool(edit["released"])))
    if "solutions" in edit:
        text = set_hw_field(text, "solutions",
                            _yesno(bool(edit["solutions"])))
    if "due" in edit:
        text = set_hw_field(text, "due", str(edit["due"]).strip())
    return text


# ------------------------------------------------------ git per file --

def classify_sync(rel: str, ours: set[str], theirs: set[str],
                  dirty: set[str]) -> str:
    """How one file compares to GitHub: insync/ahead/behind/diverged."""
    local = rel in ours or rel in dirty
    remote = rel in theirs
    if local and remote:
        return "diverged"
    if remote:
        return "behind"
    if local:
        return "ahead"
    return "insync"


def _names(p) -> set[str]:
    """Paths from a ``git ... -z --name-only`` run."""
    if p.returncode != 0:
        return set()
    return {n for n in p.stdout.split("\0") if n}


def file_sync_sets(clone: Path) -> tuple[set, set, set, str | None]:
    """(ours, theirs, dirty, error) path sets for the whole clone.

    ``ours``/``theirs`` are what each side changed since the merge base,
    so a file is only "diverged" when both histories really touched it.
    """
    fetch = _run(["git", "fetch", "-q", "origin"], cwd=clone, timeout=120)
    err = "fetch failed — showing the last known state" \
        if fetch.returncode != 0 else None
    dirty = _names(_run(["git", "diff", "-z", "--name-only", "HEAD"],
                        cwd=clone, timeout=30))
    base = _run(["git", "merge-base", "HEAD", "@{upstream}"], cwd=clone,
                timeout=15)
    if base.returncode != 0:
        return set(), set(), dirty, err or "no upstream branch"
    b = base.stdout.strip()
    ours = _names(_run(["git", "diff", "-z", "--name-only", b, "HEAD"],
                       cwd=clone, timeout=30))
    theirs = _names(_run(["git", "diff", "-z", "--name-only", b,
                          "@{upstream}"], cwd=clone, timeout=30))
    return ours, theirs, dirty, err


# ----------------------------------------------------------- scanning --

def _number_key(n: str) -> tuple:
    m = re.match(r"\s*(\d+)", n or "")
    return (int(m.group(1)) if m else 10**6, n or "")


# Names hwgenie itself generates.  The site builder only ever sees a
# clean checkout, but this page reads the user's working tree, where
# local ``build/`` output sits right beside the sources.
GENERATED = ("[submission]", "[template]", "[source]", "[solutions]")


def problem_set_sources(clone: Path) -> list[Path]:
    """Every hand-written problem-set source in a course clone."""
    root = clone / "source" / "problem-sets"
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.rglob("*.tex")):
        parts = p.relative_to(root).parts
        if any(d.startswith("_") or d == "build" for d in parts[:-1]):
            continue
        if any(g in p.name.lower() for g in GENERATED):
            continue
        out.append(p)
    return drop_ignored(clone, out)


def drop_ignored(clone: Path, paths: list[Path]) -> list[Path]:
    """Filter out anything .gitignore excludes — a file GitHub never
    sees cannot be published, so it has no business in the table."""
    if not paths:
        return paths
    rels = [p.relative_to(clone).as_posix() for p in paths]
    try:
        proc = _run_stdin(["git", "check-ignore", "--stdin", "-z"], clone,
                          "\0".join(rels))
    except (OSError, subprocess.SubprocessError):
        return paths
    if proc.returncode not in (0, 1):   # 1 = nothing was ignored
        return paths
    ignored = {n for n in proc.stdout.split("\0") if n}
    return [p for p, rel in zip(paths, rels) if rel not in ignored]


def scan_course(repo: str, clone: Path | None) -> dict:
    """One pinned course: its problem sets and how each compares to
    GitHub."""
    name = repo.split("/")[-1]
    out = {"repo": repo, "name": name, "course": "", "semester": "",
           "path": str(clone) if clone else None, "error": None, "sets": []}
    if clone is None:
        out["error"] = "no local clone found"
        return out
    cfg = clone / "course.yml"
    if cfg.is_file():
        try:
            meta = parse_course_yml(cfg.read_text(encoding="utf-8"))
            out["course"] = meta.get("course", "")
            out["semester"] = meta.get("semester", "")
        except OSError:
            pass

    ours, theirs, dirty, err = file_sync_sets(clone)
    out["error"] = err

    sets = []
    for src in problem_set_sources(clone):
        rel = src.relative_to(clone).as_posix()
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            from .texscan import mask_verbatim
            m = parse_metadata(mask_verbatim(text))
        except MetadataError:
            continue
        if m.doc_type != "problemset":
            continue
        from .site import is_released
        sets.append({
            "file": rel,
            "number": m.number,
            "title": m.title or "",
            # an absent \hwrelease means "live" — that is how the site
            # builder reads it, so the checkbox must agree
            "released": True if m.release is None
                        else bool(is_released(m.release)),
            "solutions": bool(is_released(m.solutions_release)),
            "due": m.due or "",
            "sync": classify_sync(rel, ours, theirs, dirty),
        })
    sets.sort(key=lambda s: _number_key(s["number"]))
    out["sets"] = sets
    return out


def candidate_courses(clones: dict[str, Path]) -> list[dict]:
    """Local clones that actually hold problem sets — the pick list."""
    from .sync_template import DEFAULT_TEMPLATE
    out = []
    for repo, path in clones.items():
        # course-template is scaffolding, not a course to teach from
        if repo == DEFAULT_TEMPLATE:
            continue
        if not (path / "source" / "problem-sets").is_dir():
            continue
        cfg = path / "course.yml"
        meta = parse_course_yml(cfg.read_text(encoding="utf-8")) \
            if cfg.is_file() else {}
        out.append({"repo": repo, "name": repo.split("/")[-1],
                    "course": meta.get("course", ""),
                    "semester": meta.get("semester", "")})
    out.sort(key=lambda c: c["name"])
    return out


# ------------------------------------------------------ pinned store --

def load_pinned() -> list[str]:
    try:
        data = json.loads(PINNED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pinned = data.get("pinned", [])
    return [r for r in pinned if isinstance(r, str)]


def save_pinned(repos: list[str]) -> None:
    PINNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PINNED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"pinned": repos}, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(PINNED_PATH)


def scan(roots: list[Path], log=lambda s: None) -> dict:
    """One full refresh of every pinned course."""
    from .new_course import _extend_path
    _extend_path()   # git/gh live outside the launchd PATH

    clones = find_local_clones(roots)
    pinned = load_pinned()
    courses = []
    for repo in pinned:
        log(f"Checking {repo.split('/')[-1]}…")
        courses.append(scan_course(repo, clones.get(repo)))
    return {"scanned_at": time.time(), "pinned": pinned,
            "candidates": candidate_courses(clones), "courses": courses}


# ------------------------------------------------------ server state --

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "idle"          # idle | scanning | working
        self.lines: list[str] = []
        self.data: dict | None = None
        self.roots: list[Path] = []

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "lines": list(self.lines),
                    "data": self.data}

    def _begin(self, phase: str) -> bool:
        with self.lock:
            if self.phase != "idle":
                return False
            self.phase = phase
            self.lines.clear()
            return True

    def _finish(self, data: dict | None = None) -> None:
        with self.lock:
            if data is not None:
                self.data = data
            self.phase = "idle"


SETS = _State()


def _scan_worker() -> None:
    try:
        data = scan(SETS.roots, SETS.log)
    except Exception as e:  # noqa: BLE001 — a dead worker must never
        # leave the page saying "refreshing…" forever
        data = {"scanned_at": time.time(), "pinned": load_pinned(),
                "candidates": [], "courses": [],
                "error": f"scan failed: {e!r}"}
    SETS._finish(data)


def start_refresh(roots: list[Path]) -> dict:
    if not SETS._begin("scanning"):
        return {"ok": False, "error": "already running"}
    SETS.roots = roots
    threading.Thread(target=_scan_worker, daemon=True).start()
    return {"ok": True}


def _job_worker(fn) -> None:
    from .new_course import _extend_path
    _extend_path()
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — a dead worker must never
        # leave the page saying "working…" forever
        SETS.log(f"unexpected error: {e!r}")
    SETS.log("Refreshing…")
    with SETS.lock:
        SETS.phase = "scanning"
    try:
        SETS._finish(scan(SETS.roots, lambda s: None))
    except Exception as e:  # noqa: BLE001
        SETS.log(f"refresh failed: {e!r}")
        SETS._finish()


def _start_job(fn, roots: list[Path]) -> dict:
    if not SETS._begin("working"):
        return {"ok": False, "error": "already running"}
    SETS.roots = roots
    threading.Thread(target=_job_worker, args=(fn,), daemon=True).start()
    return {"ok": True}


def _clone_of(repo: str) -> Path | None:
    for c in (SETS.data or {}).get("courses", []):
        if c["repo"] == repo and c.get("path"):
            return Path(c["path"])
    clone = find_local_clones(SETS.roots).get(repo)
    return clone


def _log_proc(p) -> None:
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            SETS.log(line)


# ----------------------------------------------------------- the push --

def describe_edit(before: dict, edit: dict) -> str:
    """A one-line summary of what a row's edit changes, for the commit."""
    bits = []
    for key, label in (("released", "assignment"), ("solutions",
                                                    "solutions")):
        if key in edit and bool(edit[key]) != bool(before.get(key)):
            bits.append(f"{label} {'published' if edit[key] else 'hidden'}")
    if "due" in edit and str(edit["due"]).strip() != (before.get("due") or ""):
        due = str(edit["due"]).strip()
        bits.append(f"due {due}" if due else "due date cleared")
    return ", ".join(bits)


def _do_push(repo: str, edits: dict) -> None:
    """Write one course's pending edits, then commit and push them."""
    label = repo.split("/")[-1]
    SETS.log(f"── {label}")
    clone = _clone_of(repo)
    if clone is None:
        SETS.log("no local clone — nothing pushed")
        return

    ours, theirs, dirty, err = file_sync_sets(clone)
    if err:
        SETS.log(err)
    conflicted = [f for f in edits
                  if classify_sync(f, ours, theirs, dirty) == "diverged"]
    if conflicted:
        SETS.log("changed on BOTH sides: " + ", ".join(conflicted))
        SETS.log("resolve those rows first — nothing pushed")
        return
    if theirs:
        # the Overleaf side moved: edit the current sources, not stale ones
        SETS.log("GitHub is ahead — pulling first…")
        pull = _run(["git", "pull", "--ff-only"], cwd=clone, timeout=120)
        if pull.returncode != 0:
            _log_proc(pull)
            SETS.log("pull failed — nothing pushed")
            return

    known = {s["file"]: s for c in (SETS.data or {}).get("courses", [])
             if c["repo"] == repo for s in c["sets"]}
    changed: list[str] = []
    summary: list[str] = []
    dirty_now = _names(_run(["git", "diff", "-z", "--name-only", "HEAD"],
                            cwd=clone, timeout=30))
    for rel, edit in edits.items():
        src = clone / rel
        if not src.is_file():
            SETS.log(f"{rel}: gone from the repo — skipped")
            continue
        stem = Path(rel).stem
        note = ""
        if edit:
            text = src.read_text(encoding="utf-8")
            new_text = apply_edits(text, edit)
            note = describe_edit(known.get(rel, {}), edit)
            if new_text != text:
                src.write_text(new_text, encoding="utf-8")
                dirty_now.add(rel)
            else:
                note = ""
                SETS.log(f"{rel}: already as requested")
        # a row with no pending edit (or one already applied) still needs
        # committing when the working tree has drifted from HEAD
        if rel in dirty_now:
            changed.append(rel)
            summary.append(f"{stem}: {note}" if note else f"{stem}: updated")
            SETS.log(f"{rel}: {note or 'local edits staged'}")

    if changed:
        add = _run(["git", "add", "--"] + changed, cwd=clone, timeout=60)
        if add.returncode != 0:
            _log_proc(add)
            return
        names = ", ".join(sorted(Path(f).stem for f in changed))
        message = (f"Update problem sets: {names}\n\n"
                   + "\n".join(summary) + "\n")
        commit = _run(["git", "commit", "-q", "-m", message], cwd=clone,
                      timeout=60)
        if commit.returncode != 0:
            _log_proc(commit)
            SETS.log("commit failed")
            return

    counts = _run(["git", "rev-list", "--count", "@{upstream}..HEAD"],
                  cwd=clone, timeout=15)
    ahead = int(counts.stdout.strip() or 0) if counts.returncode == 0 else 0
    if not ahead:
        SETS.log("nothing to push — GitHub already has these files")
        return
    push = _run(["git", "push", "-q"], cwd=clone, timeout=120)
    _log_proc(push)
    if push.returncode != 0:
        SETS.log("push failed — the commit is waiting in the local clone")
        return
    SETS.log(f"pushed {ahead} commit(s) — the site rebuilds on GitHub "
             "in a minute or two")


def _do_pull(repo: str) -> None:
    clone = _clone_of(repo)
    SETS.log(f"── {repo.split('/')[-1]}: git pull")
    if clone is None:
        SETS.log("no local clone")
        return
    _log_proc(_run(["git", "pull", "--ff-only"], cwd=clone, timeout=120))


def _do_resolve(repo: str) -> None:
    """Diverged clone: the same rebase-and-push the Courses page runs."""
    from .course_admin import resolve_clone
    resolve_clone(_clone_of(repo), repo.split("/")[-1], SETS.log)


# --------------------------------------------------------- API + page --

def _clean_edits(raw) -> dict:
    """Whitelist a pushed edit payload: {relpath: {released,solutions,due}}."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for rel, edit in raw.items():
        if not isinstance(rel, str) or not isinstance(edit, dict):
            continue
        if rel.startswith("/") or ".." in Path(rel).parts:
            continue
        clean = {}
        for key in ("released", "solutions"):
            if key in edit:
                clean[key] = bool(edit[key])
        if "due" in edit:
            clean["due"] = str(edit["due"])[:200]
        # an empty patch is meaningful: "commit this file as it stands"
        out[rel] = clean
    return out


def api_get(path: str):
    if path == "/problem-sets/api/state":
        return SETS.snapshot(), 200
    return None


def api_post(path: str, data: dict, roots: list[Path]):
    if path == "/problem-sets/api/refresh":
        return start_refresh(roots), 200
    if path == "/problem-sets/api/pin":
        repos = [r for r in data.get("repos", []) if isinstance(r, str)]
        save_pinned(repos)
        return start_refresh(roots), 200
    if path == "/problem-sets/api/push":
        repo = data.get("repo")
        edits = _clean_edits(data.get("edits"))
        if not isinstance(repo, str) or not repo:
            return {"ok": False, "error": "missing repo"}, 400
        if not edits:
            return {"ok": False, "error": "no changes to push"}, 400
        return _start_job(lambda: _do_push(repo, edits), roots), 200
    if path in ("/problem-sets/api/pull", "/problem-sets/api/resolve"):
        repo = data.get("repo")
        if not isinstance(repo, str) or not repo:
            return {"ok": False, "error": "missing repo"}, 400
        fn = _do_pull if path.endswith("pull") else _do_resolve
        return _start_job(lambda: fn(repo), roots), 200
    return None


def render_problem_sets() -> str:
    from .appicon import LAMP_SVG
    from .webstyle import BASE_CSS, nav_header
    return (PAGE.replace("__BASE__", BASE_CSS)
                .replace("__NAV__", nav_header("problemsets"))
                .replace("__LAMP__", LAMP_SVG))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hwGenie &mdash; Problem Sets</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<style>
__BASE__
  /* height:auto so the body box spans the full content: the sticky
     .appnav sticks for the whole scroll, not just the first viewport */
  html, body { height: auto; min-height: 100%; }
  body { overflow: auto; display: block; }
  main { max-width: 980px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }
  .topbar { display: flex; align-items: baseline; gap: 1rem;
            flex-wrap: wrap; margin-bottom: 1rem; }
  h1 { font-size: 1.35rem; margin: 0; }
  .muted { color: var(--muted); }
  .stamp { color: var(--muted); font-size: .85rem; }
  .none { color: var(--muted); font-style: italic; }
  button.primary {
    padding: .45rem 1.2rem; cursor: pointer; border: none;
    background: var(--accent); color: var(--bg); font: inherit;
  }
  button.primary:disabled { opacity: .45; cursor: default; }

  /* course picker */
  #picker { background: var(--card-bg); padding: 1rem 1.1rem;
            margin-bottom: 1.5rem; }
  #picker h2 { font-size: .8rem; letter-spacing: .04em; margin: 0 0 .6rem;
               text-transform: uppercase; color: var(--muted); }
  #picker ul { list-style: none; margin: 0 0 .9rem; padding: 0;
               display: grid; gap: .3rem;
               grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); }
  #picker label { display: flex; gap: .5rem; align-items: baseline;
                  cursor: pointer; }

  /* one course block */
  .course { margin-bottom: 2.2rem; }
  .chead { display: flex; align-items: baseline; gap: .8rem;
           flex-wrap: wrap; background: var(--bar-bg);
           padding: .5rem .8rem; }
  .chead .cname { font-weight: 600; }
  .chead .links { font-size: .8rem; }
  .chead .links a { margin-left: .7rem; }
  table { border-collapse: collapse; width: 100%; }
  th { text-align: left; font-size: .8rem; letter-spacing: .04em;
       text-transform: uppercase; color: var(--muted);
       padding: .35rem .7rem; font-weight: 600; }
  th.mid, td.mid { text-align: center; }
  td { background: var(--card-bg); padding: .5rem .7rem;
       border-bottom: 2px solid var(--bg); vertical-align: middle; }
  td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
  /* due dates are written out longhand ("Friday, September 4th at
     11:59pm"), so give that column room rather than clipping it */
  th.duecol, td.duecol { width: 19rem; }
  td .file { color: var(--muted); font-size: .78rem; }
  input[type=checkbox] { width: 1.05rem; height: 1.05rem; cursor: pointer;
                         accent-color: var(--accent); }
  /* the due box reads as text until it is clicked into */
  input.due { font: inherit; color: inherit; background: transparent;
              border: 1px solid transparent; padding: .2rem .35rem;
              width: 100%; min-width: 10rem; border-radius: 0; }
  input.due::placeholder { color: var(--muted); font-style: italic; }
  input.due:hover { border-color: var(--border); }
  input.due:focus { outline: none; border-color: var(--accent);
                    background: var(--bg); }
  /* pending edits: not on GitHub until Push runs, so mark them loudly */
  tr.pending td { background: var(--draft-bg);
                  box-shadow: inset 3px 0 0 var(--draft-accent); }
  tr.pending td ~ td { box-shadow: none; }
  .changed { box-shadow: inset 0 -2px 0 var(--draft-accent); }
  .pendbar { display: flex; align-items: center; gap: .8rem;
             flex-wrap: wrap; padding: .6rem .8rem; margin-top: .15rem;
             background: var(--draft-bg); color: var(--draft-accent); }
  .pendbar.clean { background: var(--card-bg); color: var(--muted); }
  .pendbar button.primary { background: var(--draft-accent); }
  .ok { color: var(--sol-accent); }
  .bad { color: var(--alert); }
  .stale { color: var(--draft-accent); }
  .sync { font-size: .82rem; white-space: nowrap; }
  td button.fix { font-size: .8rem; padding: .2rem .6rem; cursor: pointer;
                  border: none; background: var(--accent); color: var(--bg);
                  margin-left: .4rem; }
  td button.fix:disabled { opacity: .45; cursor: default; }
  .cerr { color: var(--alert); padding: .5rem .8rem;
          background: var(--card-bg); }
  #log { background: var(--code-bg); padding: .7rem .9rem;
         margin-top: 1.2rem; font: .82rem/1.45 ui-monospace, Menlo,
         monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
  #err { color: var(--alert); margin-top: .8rem; }
</style>
</head>
<body>
__NAV__
<main>
  <div class="topbar">
    <h1>Problem Sets</h1>
    <span class="sp"></span>
    <span class="stamp" id="stamp"></span>
    <button id="editpins" class="ghost" hidden>Change courses</button>
    <button id="refresh" class="ghost">&#8635; Refresh</button>
  </div>
  <div id="picker" hidden>
    <h2>Courses for this semester</h2>
    <ul id="cands"></ul>
    <button id="savepins" class="primary">Pin selected courses</button>
    <button id="cancelpins" class="ghost" hidden>Cancel</button>
    <p class="none" id="nocands" hidden>No local course clones with a
      <code>source/problem-sets</code> folder were found.</p>
  </div>
  <div id="courses"></div>
  <div id="err" hidden></div>
  <div id="log" hidden></div>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

let state = null;
let timer = null;
let scannedOnLoad = false;   // each page load re-reads the clones once
let picking = false;         // the pin picker is open
// pending edits, never written to disk until Push: repo -> file -> patch
const edits = {};

function rel(epoch) {
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return new Date(epoch * 1000).toLocaleString();
}

function patch(repo, file, key, value, original) {
  const forRepo = edits[repo] || (edits[repo] = {});
  const row = forRepo[file] || (forRepo[file] = {});
  if (value === original) delete row[key]; else row[key] = value;
  if (!Object.keys(row).length) delete forRepo[file];
  if (!Object.keys(forRepo).length) delete edits[repo];
}

function pendingOf(repo, file) {
  return (edits[repo] || {})[file] || {};
}

// Drop pending values a fresh scan says are now on disk.  That is how a
// push clears its edits — a push the server refused (diverged sources)
// leaves the files alone, so those edits survive and stay highlighted
// instead of vanishing as if they had been applied.
function prune(d) {
  for (const c of d.courses) {
    const rows = edits[c.repo];
    if (!rows) continue;
    for (const s of c.sets) {
      const row = rows[s.file];
      if (!row) continue;
      for (const key of Object.keys(row))
        if (row[key] === s[key]) delete row[key];
      if (!Object.keys(row).length) delete rows[s.file];
    }
    if (!Object.keys(rows).length) delete edits[c.repo];
  }
}
function pendingCount(repo) {
  return Object.keys(edits[repo] || {}).length;
}

const SYNC = {
  insync: ['<span class="ok">up to date</span>', null, null],
  ahead: ['<span class="stale">local edits not on GitHub</span>',
          "pushfile", "Push"],
  behind: ['<span class="bad">GitHub is newer</span>', "pull", "Pull"],
  diverged: ['<span class="bad">changed on both sides</span>',
             "resolve", "Resolve"],
};

function syncCell(c, s, busy) {
  const [text, act, label] =
    SYNC[s.sync] || ['<span class="muted">?</span>', null, null];
  const btn = act
    ? ` <button class="fix" data-act="${act}" data-repo="${esc(c.repo)}"
        data-file="${esc(s.file)}" ${busy ? "disabled" : ""}>${label}</button>`
    : "";
  return `<span class="sync">${text}</span>${btn}`;
}

function rowHTML(c, s, busy) {
  const p = pendingOf(c.repo, s.file);
  const released = "released" in p ? p.released : s.released;
  const solutions = "solutions" in p ? p.solutions : s.solutions;
  const due = "due" in p ? p.due : s.due;
  const mark = k => (k in p) ? " changed" : "";
  const box = (key, on) => `<input type="checkbox" data-key="${key}"
      data-file="${esc(s.file)}" class="${mark(key).trim()}"
      ${on ? "checked" : ""} ${busy ? "disabled" : ""}>`;
  return `<tr class="${Object.keys(p).length ? "pending" : ""}">
    <td class="num"><b>${esc(s.number)}</b>
      <div class="file">${esc(s.file.split("/").pop())}</div></td>
    <td>${esc(s.title) || '<span class="muted">—</span>'}</td>
    <td class="duecol"><input class="due${mark("due")}" data-key="due"
      data-file="${esc(s.file)}" value="${esc(due)}"
      placeholder="no due date" ${busy ? "disabled" : ""}></td>
    <td class="mid">${box("released", released)}</td>
    <td class="mid">${box("solutions", solutions)}</td>
    <td>${syncCell(c, s, busy)}</td>
  </tr>`;
}

function courseHTML(c, busy) {
  const [owner, name] = c.repo.split("/");
  const n = pendingCount(c.repo);
  const head = `<div class="chead">
      <span class="cname">${esc(c.course || c.name)}</span>
      <span class="muted">${esc(c.semester || "")}</span>
      <span class="sp"></span>
      <span class="links">
        <a href="https://github.com/${esc(c.repo)}" target="_blank">repo</a>
        <a href="https://${esc(owner)}.github.io/${esc(name)}/"
           target="_blank">site</a></span>
    </div>`;
  if (c.error && !c.sets.length)
    return `<div class="course" data-course="${esc(c.repo)}">${head}
      <div class="cerr">${esc(c.error)}</div></div>`;
  const warn = c.error ? `<div class="cerr">${esc(c.error)}</div>` : "";
  const body = c.sets.length ? `<table>
      <thead><tr>
        <th>Set</th><th>Title</th><th class="duecol">Due</th>
        <th class="mid">Released</th><th class="mid">Solutions</th>
        <th>Local vs GitHub</th>
      </tr></thead>
      <tbody>${c.sets.map(s => rowHTML(c, s, busy)).join("")}</tbody>
    </table>` : `<div class="cerr none">No problem sets found in
      <code>source/problem-sets</code>.</div>`;
  const bar = `<div class="pendbar ${n ? "" : "clean"}">
      <span>${n ? `${n} problem set${n > 1 ? "s" : ""} changed —
        nothing is on the site until you push`
        : "No unpushed changes"}</span>
      <span class="sp"></span>
      ${n ? `<button class="ghost" data-act="revert"
        data-repo="${esc(c.repo)}">Discard</button>` : ""}
      <button class="primary" data-act="push" data-repo="${esc(c.repo)}"
        ${n && !busy ? "" : "disabled"}>Push changes</button>
    </div>`;
  return `<div class="course" data-course="${esc(c.repo)}">${head}${
    warn}${body}${bar}</div>`;
}

function renderPicker(d, busy) {
  const open = picking || !d.pinned.length;
  $("#picker").hidden = !open;
  $("#editpins").hidden = open || !d.candidates.length;
  $("#cancelpins").hidden = !d.pinned.length;
  if (!open) return;
  $("#nocands").hidden = !!d.candidates.length;
  $("#savepins").disabled = busy || !d.candidates.length;
  $("#cands").innerHTML = d.candidates.map(c => `<li><label>
      <input type="checkbox" value="${esc(c.repo)}"
        ${d.pinned.includes(c.repo) ? "checked" : ""}>
      <span><b>${esc(c.course || c.name)}</b>
        <span class="muted">${esc(c.semester || c.name)}</span></span>
    </label></li>`).join("");
}

function render() {
  const busy = state.phase !== "idle";
  $("#refresh").disabled = busy;
  $("#log").hidden = !state.lines.length;
  $("#log").textContent = state.lines.join("\n");
  if (busy && !$("#log").hidden)
    $("#log").scrollTop = $("#log").scrollHeight;
  const d = state.data;
  if (!d) { $("#stamp").textContent = busy ? "reading clones…" : ""; return; }
  $("#stamp").textContent = "data from " + rel(d.scanned_at) +
                            (busy ? " · working…" : "");
  $("#err").hidden = !d.error;
  $("#err").textContent = d.error || "";
  prune(d);
  renderPicker(d, busy);
  $("#courses").innerHTML = d.courses.map(c => courseHTML(c, busy)).join("");

  $("#courses").querySelectorAll("input[data-key]").forEach(el => {
    const c = d.courses.find(
      x => x.repo === el.closest(".course").dataset.course);
    const s = c.sets.find(x => x.file === el.dataset.file);
    const key = el.dataset.key;
    const ev = key === "due" ? "input" : "change";
    el.addEventListener(ev, () => {
      const value = key === "due" ? el.value : el.checked;
      patch(c.repo, s.file, key, value, s[key]);
      // re-render on the next tick so a half-typed due date keeps focus
      if (key === "due") { markRow(el, c.repo, s.file); } else render();
    });
  });
  $("#courses").querySelectorAll("button[data-act]").forEach(b =>
    b.addEventListener("click", () => {
      const repo = b.dataset.repo;
      const act = b.dataset.act;
      if (act === "revert") { delete edits[repo]; render(); return; }
      if (act === "push" || act === "pushfile") {
        const payload = Object.assign({}, edits[repo] || {});
        if (act === "pushfile") payload[b.dataset.file] =
          payload[b.dataset.file] || {};
        start("/problem-sets/api/push", {repo, edits: payload});
      } else start("/problem-sets/api/" + b.dataset.act, {repo});
    }));
}

// live feedback for the due box without stealing focus mid-typing
function markRow(el, repo, file) {
  const p = pendingOf(repo, file);
  el.classList.toggle("changed", "due" in p);
  el.closest("tr").classList.toggle("pending", !!Object.keys(p).length);
  const bar = el.closest(".course").querySelector(".pendbar");
  const n = pendingCount(repo);
  bar.classList.toggle("clean", !n);
  bar.firstElementChild.textContent = n
    ? `${n} problem set${n > 1 ? "s" : ""} changed — nothing is on the ` +
      "site until you push"
    : "No unpushed changes";
  bar.querySelector('button[data-act="push"]').disabled =
    !n || state.phase !== "idle";
}

async function poll() {
  try {
    state = await (await fetch("/problem-sets/api/state")).json();
  } catch (e) { return; }
  render();
  clearTimeout(timer);
  if (state.phase !== "idle") {
    scannedOnLoad = true; timer = setTimeout(poll, 1000);
  } else if (!state.data || !scannedOnLoad) {
    scannedOnLoad = true; start("/problem-sets/api/refresh");
  } else timer = setTimeout(poll, 30000);   // keep the stamp honest
}

async function start(url, body) {
  const r = await fetch(url, {method: "POST",
                              body: JSON.stringify(body || {})});
  const res = await r.json();
  if (!res.ok && res.error) {
    $("#err").hidden = false; $("#err").textContent = res.error;
  }
  poll();
}

$("#refresh").addEventListener("click", () =>
  start("/problem-sets/api/refresh"));
$("#editpins").addEventListener("click", () => { picking = true; render(); });
$("#cancelpins").addEventListener("click",
  () => { picking = false; render(); });
$("#savepins").addEventListener("click", () => {
  const repos = [...$("#cands").querySelectorAll("input:checked")]
    .map(el => el.value);
  picking = false;
  for (const k of Object.keys(edits)) delete edits[k];
  start("/problem-sets/api/pin", {repos});
});
poll();
setInterval(() => {
  if (state && state.data) $("#stamp").textContent =
    "data from " + rel(state.data.scanned_at) +
    (state.phase !== "idle" ? " · working…" : "");
}, 30000);
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
