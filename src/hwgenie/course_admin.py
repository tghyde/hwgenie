"""Course management page for the hwGenie app (``/courses``).

Scans the user's GitHub for course repos (any repo whose
``.github/workflows/build.yml`` installs hwgenie), shows the hwGenie
version each is pegged to, whether the shared template files are in
sync, and whether the local clone is ahead of / behind GitHub.  Buttons
run ``hwgenie sync-template`` per course or for every outdated course.

All GitHub/git work runs in a background thread; the page polls
``/courses/api/state``.  Scan results are cached in memory with a
timestamp so the page can say how fresh they are.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .sync_template import (DEFAULT_TEMPLATE, MANIFEST_NAME, classify,
                            parse_manifest)

HWGENIE_REPO = "tghyde/hwgenie"
_GH_TIMEOUT = 45


# ------------------------------------------------------ pure helpers --

def parse_pin(build_yml: str) -> str | None:
    """The hwgenie version a course's build workflow is pegged to."""
    m = re.search(r"hwgenie(?:\.git)?@v?([0-9][0-9A-Za-z.\-]*)", build_yml)
    return m.group(1) if m else None


def replace_pin(build_yml: str, version: str) -> str:
    """The same workflow text, re-pegged to ``version``."""
    return re.sub(r"(hwgenie(?:\.git)?@)v?[0-9][0-9A-Za-z.\-]*",
                  rf"\g<1>v{version}", build_yml)


def ci_from_runs(runs: dict | None) -> dict | None:
    """status/conclusion/url of the most recent Actions run, or None."""
    if not runs or not runs.get("workflow_runs"):
        return None
    r = runs["workflow_runs"][0]
    return {"status": r.get("status"), "conclusion": r.get("conclusion"),
            "url": r.get("html_url")}


def parse_course_yml(text: str) -> dict:
    """course / title / semester from a course.yml (flat ``key: value``)."""
    out: dict = {}
    for line in text.splitlines():
        m = re.match(r"^(course|title|semester)\s*:\s*(.+?)\s*$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip("\"'")
    return out


def version_key(v: str) -> tuple:
    """Sort key for version strings: numeric fields compare numerically."""
    return tuple(int(p) if p.isdigit() else -1
                 for p in re.split(r"[.\-+]", v.lstrip("v")))


def latest_version(tags: list[str]) -> str | None:
    versions = [t.lstrip("v") for t in tags
                if re.fullmatch(r"v?\d+(\.\d+)*", t)]
    return max(versions, key=version_key) if versions else None


def repo_from_remote(url: str) -> str | None:
    """``owner/name`` from a github remote URL (ssh or https)."""
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
                  url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ------------------------------------------------------- subprocesses --

def _run(cmd: list[str], cwd: Path | None = None,
         timeout: int = _GH_TIMEOUT) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def _gh_raw(repo: str, path: str) -> str | None:
    try:
        p = _run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                  f"repos/{repo}/contents/{path}"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _gh_json(args: list[str]):
    try:
        p = _run(["gh"] + args)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


# ------------------------------------------------------- local clones --

def find_local_clones(roots: list[Path]) -> dict[str, Path]:
    """Map ``owner/name`` -> path for course repos one level below roots."""
    clones: dict[str, Path] = {}
    seen: set[Path] = set()
    for root in roots:
        try:
            subs = [p for p in Path(root).iterdir() if p.is_dir()]
        except OSError:
            continue
        for d in subs:
            d = d.resolve()
            if d in seen or not ((d / ".git").exists()
                                 and (d / "course.yml").is_file()):
                continue
            seen.add(d)
            try:
                p = _run(["git", "config", "--get", "remote.origin.url"],
                         cwd=d, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            repo = repo_from_remote(p.stdout) if p.returncode == 0 else None
            if repo:
                clones.setdefault(repo, d)
    return clones


def local_git_status(path: Path) -> dict:
    """Fetch origin, then report ahead/behind/dirty for the clone."""
    out = {"path": str(path), "ahead": None, "behind": None,
           "dirty": False, "error": None}
    try:
        fetch = _run(["git", "fetch", "-q", "origin"], cwd=path, timeout=60)
        if fetch.returncode != 0:
            out["error"] = "fetch failed"
        p = _run(["git", "rev-list", "--left-right", "--count",
                  "HEAD...@{upstream}"], cwd=path, timeout=15)
        if p.returncode == 0:
            ahead, behind = p.stdout.split()
            out["ahead"], out["behind"] = int(ahead), int(behind)
        else:
            out["error"] = out["error"] or "no upstream"
        st = _run(["git", "status", "--porcelain"], cwd=path, timeout=15)
        out["dirty"] = bool(st.stdout.strip()) if st.returncode == 0 else False
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        out["error"] = out["error"] or str(e)
    return out


# -------------------------------------------------------------- scan --

def scan(roots: list[Path], log=lambda s: None) -> dict:
    """One full refresh: GitHub repo list, versions, local git state."""
    from .new_course import _extend_path
    _extend_path()   # gh lives in /opt/homebrew/bin, absent from the
    # launchd PATH the app inherits

    errors: list[str] = []
    log("Listing GitHub repos…")
    repos = _gh_json(["repo", "list", "--limit", "200",
                      "--json", "nameWithOwner"])
    if repos is None:
        errors.append("could not list GitHub repos — is `gh` installed "
                      "and authenticated?")
        repos = []
    names = [r["nameWithOwner"] for r in repos]

    log("Reading hwgenie tags and template pin…")
    tags = _gh_json(["api", f"repos/{HWGENIE_REPO}/tags", "--paginate",
                     "--jq", "[.[].name]"]) or []
    latest = latest_version(tags)
    if latest is None:
        errors.append("could not read hwgenie release tags")

    template_build = _gh_raw(DEFAULT_TEMPLATE, ".github/workflows/build.yml")
    template_pin = parse_pin(template_build) if template_build else None
    if template_pin is None:
        errors.append("could not read the course-template build workflow")

    # The template files a sync would install, fetched once.
    manifest_text = _gh_raw(DEFAULT_TEMPLATE, MANIFEST_NAME)
    template_files: dict[str, str] = {}
    if manifest_text:
        for path in parse_manifest(manifest_text):
            content = _gh_raw(DEFAULT_TEMPLATE, path)
            if content is not None:
                template_files[path] = content
    else:
        errors.append("could not read the template sync manifest")

    log("Checking course repos…")

    def probe(name: str) -> dict | None:
        if name in (DEFAULT_TEMPLATE, HWGENIE_REPO):
            return None
        build = _gh_raw(name, ".github/workflows/build.yml")
        if not build or "hwgenie" not in build:
            return None
        pin = parse_pin(build)
        if pin is None:
            return None
        meta = parse_course_yml(_gh_raw(name, "course.yml") or "")
        ci = ci_from_runs(_gh_json(
            ["api", f"repos/{name}/actions/runs?per_page=1"]))
        return {"repo": name, "name": name.split("/")[-1], "pin": pin,
                "ci": ci,
                **{k: meta.get(k, "") for k in
                   ("course", "title", "semester")}}

    with ThreadPoolExecutor(max_workers=8) as pool:
        courses = [c for c in pool.map(probe, names) if c]
    courses.sort(key=lambda c: c["name"])

    clones = find_local_clones(roots)
    for c in courses:
        clone = clones.get(c["repo"])
        c["local"] = local_git_status(clone) if clone else None
        if clone and template_files:
            differs = []
            for path, remote in template_files.items():
                f = clone / path
                local = f.read_text(encoding="utf-8") if f.is_file() else None
                if classify(local, remote) != "unchanged":
                    differs.append(path)
            c["differs"] = differs
        else:
            c["differs"] = None
        if c["local"]:
            log(f"{c['name']}: v{c['pin']}")

    return {"scanned_at": time.time(), "latest": latest,
            "template_pin": template_pin, "courses": courses,
            "errors": errors}


# ------------------------------------------------------ server state --

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "idle"          # idle | scanning | syncing
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


COURSES = _State()


def _scan_worker() -> None:
    try:
        data = scan(COURSES.roots, COURSES.log)
    except Exception as e:  # noqa: BLE001 — a dead worker must never
        # leave the page saying "refreshing…" forever
        data = {"scanned_at": time.time(), "latest": None,
                "template_pin": None, "courses": [],
                "errors": [f"scan failed: {e!r}"]}
    COURSES._finish(data)


def start_refresh(roots: list[Path]) -> dict:
    if not COURSES._begin("scanning"):
        return {"ok": False, "error": "already running"}
    COURSES.roots = roots
    threading.Thread(target=_scan_worker, daemon=True).start()
    return {"ok": True}


def _job_worker(fn) -> None:
    """Run one mutating job (sync/pull/push/bump), then rescan."""
    from .new_course import _extend_path
    _extend_path()
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — a dead worker must never
        # leave the page saying "working…" forever
        COURSES.log(f"unexpected error: {e!r}")
    COURSES.log("Refreshing…")
    with COURSES.lock:
        COURSES.phase = "scanning"
    try:
        data = scan(COURSES.roots, lambda s: None)
        COURSES._finish(data)
    except Exception as e:  # noqa: BLE001
        COURSES.log(f"refresh failed: {e!r}")
        COURSES._finish()


def _start_job(fn, roots: list[Path]) -> dict:
    if not COURSES._begin("working"):
        return {"ok": False, "error": "already running"}
    COURSES.roots = roots
    threading.Thread(target=_job_worker, args=(fn,), daemon=True).start()
    return {"ok": True}


def _known_clones() -> dict[str, str]:
    return {c["repo"]: c["local"]["path"]
            for c in (COURSES.data or {}).get("courses", [])
            if c.get("local")}


def _log_proc(p: subprocess.CompletedProcess) -> None:
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            COURSES.log(line)


def _do_sync(repos: list[str]) -> None:
    from .sync_template import run_sync_template
    clones = _known_clones()
    failed = []
    for repo in repos:
        clone = clones.get(repo)
        COURSES.log(f"── {repo.split('/')[-1]}")
        if not clone:
            COURSES.log("no local clone — skipped")
            failed.append(repo)
            continue
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(buf):
                rc = run_sync_template(Namespace(
                    dir=clone, template=DEFAULT_TEMPLATE,
                    dry_run=False, diff=False, no_push=False))
        except Exception as e:  # noqa: BLE001
            buf.write(f"unexpected error: {e!r}\n")
            rc = 1
        for line in buf.getvalue().splitlines():
            COURSES.log(line)
        if rc != 0:
            failed.append(repo)
    if failed:
        COURSES.log(f"⚠ {len(failed)} course(s) failed — see above")


def _do_pull(repo: str) -> None:
    clone = _known_clones().get(repo)
    COURSES.log(f"── {repo.split('/')[-1]}: git pull")
    if not clone:
        COURSES.log("no local clone")
        return
    _log_proc(_run(["git", "pull", "--ff-only"], cwd=Path(clone),
                   timeout=120))


def _do_push(repo: str) -> None:
    clone = _known_clones().get(repo)
    COURSES.log(f"── {repo.split('/')[-1]}: git push")
    if not clone:
        COURSES.log("no local clone")
        return
    p = _run(["git", "push"], cwd=Path(clone), timeout=120)
    _log_proc(p)
    if p.returncode == 0:
        COURSES.log("pushed")


def find_clone_of(repo: str, roots: list[Path]) -> Path | None:
    """A local clone of ``repo`` one level below the roots (no
    course.yml requirement — course-template isn't a course)."""
    for root in roots:
        try:
            subs = [p for p in Path(root).iterdir() if p.is_dir()]
        except OSError:
            continue
        for d in subs:
            if not (d / ".git").exists():
                continue
            try:
                p = _run(["git", "config", "--get", "remote.origin.url"],
                         cwd=d, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if p.returncode == 0 and repo_from_remote(p.stdout) == repo:
                return d.resolve()
    return None


def _do_bump_template() -> None:
    """Re-peg course-template's build workflow to the latest hwgenie."""
    latest = (COURSES.data or {}).get("latest")
    COURSES.log("── course-template: bump hwgenie pin")
    if not latest:
        COURSES.log("latest hwgenie version unknown — refresh first")
        return
    clone = find_clone_of(DEFAULT_TEMPLATE, COURSES.roots)
    if clone is None:
        COURSES.log(f"no local clone of {DEFAULT_TEMPLATE} found")
        return
    _log_proc(pull := _run(["git", "pull", "--ff-only"], cwd=clone,
                           timeout=120))
    if pull.returncode != 0:
        COURSES.log("pull failed — not bumping")
        return
    wf = clone / ".github" / "workflows" / "build.yml"
    text = wf.read_text(encoding="utf-8")
    if parse_pin(text) == latest:
        COURSES.log(f"template already pins v{latest}")
        return
    wf.write_text(replace_pin(text, latest), encoding="utf-8")
    for cmd in (["git", "add", "--", str(wf.relative_to(clone))],
                ["git", "commit", "-q", "-m", f"Pin hwgenie v{latest}"],
                ["git", "push", "-q"]):
        p = _run(cmd, cwd=clone, timeout=120)
        if p.returncode != 0:
            _log_proc(p)
            COURSES.log(f"{cmd[1]} failed")
            return
    COURSES.log(f"template now pins v{latest} — courses show as behind "
                "until you Update All")


def start_sync(repos: list[str], roots: list[Path]) -> dict:
    if not repos:
        return {"ok": False, "error": "nothing to sync"}
    return _start_job(lambda: _do_sync(repos), roots)


# --------------------------------------------------------- API + page --

def api_get(path: str):
    if path == "/courses/api/state":
        return COURSES.snapshot(), 200
    return None


def api_post(path: str, data: dict, roots: list[Path]):
    if path == "/courses/api/refresh":
        return start_refresh(roots), 200
    if path == "/courses/api/sync":
        repos = [r for r in data.get("repos", []) if isinstance(r, str)]
        return start_sync(repos, roots), 200
    if path in ("/courses/api/pull", "/courses/api/push"):
        repo = data.get("repo")
        if not isinstance(repo, str) or not repo:
            return {"ok": False, "error": "missing repo"}, 400
        fn = _do_pull if path.endswith("pull") else _do_push
        return _start_job(lambda: fn(repo), roots), 200
    if path == "/courses/api/bump-template":
        return _start_job(_do_bump_template, roots), 200
    return None


def render_courses() -> str:
    from .appicon import LAMP_SVG
    from .webstyle import BASE_CSS, nav_header
    return (PAGE.replace("__BASE__", BASE_CSS)
                .replace("__NAV__", nav_header("courses"))
                .replace("__LAMP__", LAMP_SVG))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hwGenie — Courses</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#24589f">
<style>
__BASE__
  /* height:auto so the body box spans the full content: the sticky
     .appnav sticks for the whole scroll, not just the first viewport */
  html, body { height: auto; min-height: 100%; }
  body { overflow: auto; display: block; }
  main { max-width: 880px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }
  .topbar { display: flex; align-items: baseline; gap: 1rem;
            flex-wrap: wrap; margin-bottom: .35rem; }
  .muted { color: var(--muted); }
  .stamp { color: var(--muted); font-size: .85rem; }
  .warn { background: color-mix(in srgb, var(--mark-bg) 40%, var(--bg));
          padding: .5rem .8rem; margin: .6rem 0; font-size: .9rem; }
  .warn button { font-size: .85rem; padding: .3rem .8rem; cursor: pointer;
                 border: none; background: var(--accent);
                 color: var(--bg); margin-left: .4rem; }
  .warn button:disabled { opacity: .45; cursor: default; }
  .actions { display: flex; gap: .6rem; align-items: center;
             margin: 1rem 0 1.2rem; }
  button.primary, a.btn {
    padding: .45rem 1.2rem; cursor: pointer; border: none;
    background: var(--accent); color: var(--bg); font: inherit;
    text-decoration: none; display: inline-block;
  }
  button.primary:disabled { opacity: .45; cursor: default; }
  table { border-collapse: collapse; width: 100%; }
  th { text-align: left; font-size: .8rem; letter-spacing: .04em;
       text-transform: uppercase; color: var(--muted);
       padding: .35rem .7rem; }
  td { background: var(--card-bg); padding: .55rem .7rem;
       border-bottom: 2px solid var(--bg); vertical-align: baseline; }
  td .links { font-size: .8rem; }
  td .links a { margin-right: .6rem; }
  .cname { font-weight: 600; }
  .csem { color: var(--muted); font-size: .85rem; }
  .ok { color: var(--sol-accent); }
  a.ci { text-decoration: none; font-size: 1.05rem; }
  .bad { color: var(--alert); }
  .stale { color: var(--draft-accent); }
  .vercell { white-space: nowrap; }
  td button { font-size: .85rem; padding: .3rem .8rem; cursor: pointer;
              border: none; background: var(--accent); color: var(--bg); }
  td button:disabled { opacity: .45; cursor: default; }
  #log { background: var(--code-bg); padding: .7rem .9rem;
         margin-top: 1.2rem; font: .82rem/1.45 ui-monospace, Menlo,
         monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
  #err { color: var(--alert); margin-top: .8rem; }
  .none { color: var(--muted); font-style: italic; }
</style>
</head>
<body>
__NAV__
<main>
  <div class="topbar">
    <span>hwGenie <b id="latest">…</b> <span class="muted">is the latest
      release</span> · <span class="muted">template pins</span>
      <b id="tpin">…</b></span>
    <span class="sp"></span>
    <span class="stamp" id="stamp"></span>
    <button id="refresh" class="ghost">↻ Refresh</button>
  </div>
  <div id="pinwarn" class="warn" hidden></div>
  <div class="actions">
    <button id="syncall" class="primary" disabled>Update All Courses</button>
    <a class="btn" href="/new-course">＋ New Course</a>
    <span id="busy" class="muted" hidden>working…</span>
  </div>
  <table id="tbl" hidden>
    <thead><tr>
      <th>Course</th><th>hwGenie</th><th>Shared Template Files</th>
      <th>Build</th><th>Local repo</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <p id="empty" class="none" hidden>No course repos found on GitHub.</p>
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

function rel(epoch) {
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return new Date(epoch * 1000).toLocaleString();
}

function outdated(c) { return !!(c.differs && c.differs.length); }

function localCell(c) {
  if (!c.local) return '<span class="muted">not cloned</span>';
  const l = c.local;
  if (l.error) return `<span class="bad">${esc(l.error)}</span>`;
  const bits = [];
  if (l.behind) bits.push(`<span class="bad">${l.behind} behind GitHub</span>`);
  if (l.ahead) bits.push(`<span class="stale">${l.ahead} ahead (push?)</span>`);
  if (!bits.length) bits.push('<span class="ok">up to date</span>');
  if (l.dirty) bits.push('<span class="stale">+ local edits</span>');
  return bits.join(", ");
}

function verCell(c, tpin) {
  if (!tpin || c.pin === tpin)
    return `<span class="ok">v${esc(c.pin)}</span>`;
  return `<span class="bad">v${esc(c.pin)}</span>` +
         ` <span class="muted">→ v${esc(tpin)}</span>`;
}

function ciCell(c) {
  if (!c.ci) return '<span class="muted">—</span>';
  const url = esc(c.ci.url || "");
  const a = (cls, icon, tip) => `<a class="ci ${cls}" href="${url}"
    target="_blank" title="${tip}">${icon}</a>`;
  if (c.ci.status !== "completed") return a("stale", "●", "running");
  if (c.ci.conclusion === "success") return a("ok", "✓", "passed");
  return a("bad", "✗", esc(c.ci.conclusion || "failed"));
}

function filesCell(c) {
  if (c.differs === null || c.differs === undefined)
    return '<span class="muted">—</span>';
  if (!c.differs.length) return '<span class="ok">in sync</span>';
  const n = c.differs.length;
  return `<span class="bad" title="${esc(c.differs.join(", "))}">` +
         `${n} file${n > 1 ? "s" : ""} behind template</span>`;
}

function render() {
  const busy = state.phase !== "idle";
  $("#busy").hidden = !busy;
  $("#refresh").disabled = busy;
  const d = state.data;
  $("#log").hidden = !(state.lines.length && (busy ||
    state.phase === "idle" && state.lines.some(l => l.includes("──"))));
  $("#log").textContent = state.lines.join("\n");
  if (busy && !$("#log").hidden)
    $("#log").scrollTop = $("#log").scrollHeight;
  if (!d) { $("#stamp").textContent = busy ? "refreshing…" : ""; return; }

  $("#latest").textContent = d.latest ? "v" + d.latest : "?";
  $("#tpin").textContent = d.template_pin ? "v" + d.template_pin : "?";
  $("#stamp").textContent = "data from " + rel(d.scanned_at) +
                            (busy ? " · refreshing…" : "");
  const warn = $("#pinwarn");
  if (d.latest && d.template_pin && d.latest !== d.template_pin) {
    warn.hidden = false;
    warn.innerHTML = "course-template pins v" + esc(d.template_pin) +
      " but the latest hwGenie is v" + esc(d.latest) + " — syncing " +
      "brings courses to the template's pin, so bump the template " +
      "first, then Update All. " +
      `<button id="bump" ${busy ? "disabled" : ""}>Bump template to v` +
      esc(d.latest) + "</button>";
    $("#bump").addEventListener("click", () =>
      start("/courses/api/bump-template"));
  } else warn.hidden = true;

  const errs = (d.errors || []);
  $("#err").hidden = !errs.length;
  $("#err").textContent = errs.join(" · ");

  const tb = $("#tbl tbody");
  $("#tbl").hidden = !d.courses.length;
  $("#empty").hidden = !!d.courses.length;
  tb.innerHTML = d.courses.map(c => {
    const [owner, name] = c.repo.split("/");
    const label = c.course ? esc(c.course) : esc(c.name);
    const sem = c.semester ? esc(c.semester) : "";
    const canSync = !!c.local;
    const l = c.local;
    let gitBtn = "";
    if (l && !l.error) {
      if (l.behind && !l.ahead) gitBtn = "pull";
      else if (l.ahead && !l.behind) gitBtn = "push";
    }
    return `<tr>
      <td><div class="cname">${label}</div>
        <div class="csem">${sem}</div>
        <div class="links">
          <a href="https://github.com/${esc(c.repo)}" target="_blank">repo</a>
          <a href="https://${esc(owner)}.github.io/${esc(name)}/"
             target="_blank">site</a></div></td>
      <td class="vercell">${verCell(c, d.template_pin)}${
        outdated(c) && canSync ? ` <button data-act="sync"
          data-repo="${esc(c.repo)}" ${busy ? "disabled" : ""}>Sync` +
          "</button>" : ""}</td>
      <td>${filesCell(c)}</td>
      <td>${ciCell(c)}</td>
      <td>${localCell(c)}${gitBtn ? ` <button data-act="${gitBtn}"
        data-repo="${esc(c.repo)}" ${busy ? "disabled" : ""}>` +
        (gitBtn === "pull" ? "Pull" : "Push") + "</button>" : ""}</td>
      </tr>`;
  }).join("");
  tb.querySelectorAll("button[data-act]").forEach(b =>
    b.addEventListener("click", () => {
      if (b.dataset.act === "sync") sync([b.dataset.repo]);
      else start("/courses/api/" + b.dataset.act, {repo: b.dataset.repo});
    }));

  const todo = d.courses.filter(c => outdated(c) && c.local);
  const all = $("#syncall");
  all.disabled = busy || !todo.length;
  all.title = todo.length ? "" : "All courses are in sync with the template";
  all.onclick = () => sync(todo.map(c => c.repo));
}

async function poll() {
  try {
    state = await (await fetch("/courses/api/state")).json();
  } catch (e) { return; }
  render();
  clearTimeout(timer);
  if (state.phase !== "idle") timer = setTimeout(poll, 1000);
  else if (!state.data) start("/courses/api/refresh");
  else timer = setTimeout(poll, 30000);   // keep the stamp honest
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

function sync(repos) { start("/courses/api/sync", {repos}); }
$("#refresh").addEventListener("click", () => start("/courses/api/refresh"));
poll();
setInterval(() => {
  if (state && state.data) $("#stamp").textContent =
    "data from " + rel(state.data.scanned_at) +
    (state.phase !== "idle" ? " · refreshing…" : "");
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
