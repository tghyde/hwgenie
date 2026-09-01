"""External grading server integration for the hwGenie app.

The instructor's local app can push grading folders to an always-on
"grading server" (a small VPS reached over Tailscale, running
``hwgenie grade --gui --grader-only``), watch the graders' progress,
and pull their grades/<slug>.json files back down for export.  All
transport is plain ssh/rsync using the user's own ssh config.

Configuration lives in ``~/.hwgenie/remote.json``::

    {
      "host": "hwgrader",                      # ssh destination (alias ok)
      "root": "/home/hwgrader/grading-lab",    # assignments dir on server
      "url": "https://.../grading",            # grader-facing site (link)
      "owner": "hwgrader:hwgrader",            # chown after push ("" = skip)
      "python": "/opt/hwgenie/bin/python"      # server python w/ hwgenie
    }

No file -> the External Grading section shows a "not configured" hint.

The push mirrors hwgrader-push.command: stage a copy without return/,
bundle the assignment's template.tex into the folder and relativize the
manifest's template path (an absolute local path means nothing on the
server), then ``rsync --delete`` — so the server copy is an exact
mirror.  Pull copies only grades/*.json down, never deleting local
files.  Server-side assignment names: a folder literally named
``grading`` is listed under its parent's name (ps01/grading -> ps01).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .grade import GradeError, MANIFEST_NAME

CONFIG_PATH = Path.home() / ".hwgenie" / "remote.json"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]

# runs on the server (its hwgenie venv) to describe each assignment
_LIST_SCRIPT = r"""
import json, sys
from pathlib import Path
from hwgenie.grade import GradeStore, infer_n_parts, load_manifest, load_rubric
out = []
root = Path(sys.argv[1])
for d in sorted(p for p in root.iterdir() if p.is_dir()):
    if not (d / "manifest.json").is_file():
        continue
    try:
        m = load_manifest(d)
        units = m.get("units", [])
        n = infer_n_parts(m)
        store = GradeStore(d, load_rubric(d, n))
        g, t = store.progress([u["slug"] for u in units])
        out.append({"name": d.name, "units": len(units),
                    "created": (m.get("created") or "")[:10],
                    "graded": g, "total": t})
    except Exception as e:
        out.append({"name": d.name, "error": str(e)})
print(json.dumps(out))
"""


def load_config() -> dict | None:
    """The remote-server config, or None when not set up."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not cfg.get("host"):
        return None
    cfg.setdefault("root", "/home/hwgrader/grading-lab")
    cfg.setdefault("url", "")
    cfg.setdefault("owner", "hwgrader:hwgrader")
    cfg.setdefault("python", "/opt/hwgenie/bin/python")
    return cfg


def server_name(folder: Path) -> str:
    """The assignment's name on the server (ps01/grading -> "ps01")."""
    folder = Path(folder)
    return (folder.parent.name
            if folder.name == "grading" and folder.parent.name
            else folder.name)


def _run(cmd: list[str], log, input_text: str | None = None,
         timeout: int = 600) -> str:
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              input=input_text, timeout=timeout)
    except FileNotFoundError:
        raise GradeError(f"{cmd[0]} not found on this machine")
    except subprocess.TimeoutExpired:
        raise GradeError(f"{cmd[0]} timed out")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GradeError(msg.splitlines()[-1] if msg
                         else f"{cmd[0]} failed ({proc.returncode})")
    return proc.stdout


def remote_list(cfg: dict, log=lambda s: None) -> list[dict]:
    """The assignments on the server, with grading progress."""
    out = _run(["ssh", *SSH_OPTS, cfg["host"], cfg["python"], "-",
                cfg["root"]], log, input_text=_LIST_SCRIPT, timeout=60)
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise GradeError("could not parse the server's assignment list")
    if not isinstance(data, list):
        raise GradeError("could not parse the server's assignment list")
    return data


def _bundle_template(stage: Path, log) -> None:
    """Copy an absolute-path template into the staged folder and point
    the manifest at the copy, so the server can render the problem
    statements."""
    mf = stage / MANIFEST_NAME
    try:
        m = json.loads(mf.read_text())
    except (OSError, ValueError):
        return
    tmpl = (m.get("template") or {}).get("path")
    if not tmpl:
        return
    p = Path(tmpl)
    if not p.is_absolute():
        return                       # already folder-relative: fine as-is
    if p.is_file():
        shutil.copy2(p, stage / "template.tex")
        m["template"]["path"] = "template.tex"
        mf.write_text(json.dumps(m, indent=2) + "\n")
        log("bundled the submission template into the push")
    else:
        log(f"note: template {p} not found — the problem-statement "
            "pane will be empty on the server")


def push(folder: Path, cfg: dict, log=lambda s: None) -> str:
    """Mirror a grading folder (minus return/) to the server; returns
    the server-side assignment name."""
    folder = Path(folder).resolve()
    if not (folder / MANIFEST_NAME).is_file():
        raise GradeError(f"{folder} is not a grading folder")
    name = server_name(folder)
    with tempfile.TemporaryDirectory(prefix="hwgenie-push-") as tmp:
        stage = Path(tmp) / "stage"
        shutil.copytree(folder, stage,
                        ignore=shutil.ignore_patterns("return"))
        _bundle_template(stage, log)
        _run(["rsync", "-rlt", "--delete", f"{stage}/",
              f"{cfg['host']}:{cfg['root']}/{name}/"], log)
    if cfg.get("owner"):
        _run(["ssh", *SSH_OPTS, cfg["host"],
              f"chown -R {cfg['owner']} '{cfg['root']}/{name}'"], log)
    log(f"pushed '{name}'")
    return name


def pull(folder: Path, cfg: dict, log=lambda s: None) -> str:
    """Copy the server's grades/*.json for this assignment down into the
    local grading folder (overwrites same-name files, deletes nothing)."""
    folder = Path(folder).resolve()
    if not (folder / MANIFEST_NAME).is_file():
        raise GradeError(f"{folder} is not a grading folder")
    name = server_name(folder)
    (folder / "grades").mkdir(exist_ok=True)
    _run(["rsync", "-rlt", f"{cfg['host']}:{cfg['root']}/{name}/grades/",
          f"{folder / 'grades'}/"], log)
    log(f"pulled grades for '{name}'")
    return name


# ------------------------------------------------------------- app state --

class _State:
    """One background job at a time + the last server scan."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running: str | None = None    # "scan" | "push" | "pull"
        self.log: list[str] = []
        self.error: str | None = None
        self.assignments: list[dict] | None = None
        self.scanned_at: float | None = None

    def snapshot(self) -> dict:
        cfg = load_config()
        with self.lock:
            return {
                "configured": cfg is not None,
                "host": (cfg or {}).get("host"),
                "url": (cfg or {}).get("url"),
                "running": self.running,
                "log": list(self.log),
                "error": self.error,
                "assignments": self.assignments,
                "age": (None if self.scanned_at is None
                        else round(time.monotonic() - self.scanned_at)),
            }

    def start(self, action: str, work) -> tuple[dict, int]:
        cfg = load_config()
        if cfg is None:
            return {"ok": False, "error": "no grading server configured "
                    f"(create {CONFIG_PATH})"}, 400
        with self.lock:
            if self.running:
                return {"ok": False,
                        "error": f"{self.running} already running"}, 409
            self.running = action
            self.log = []
            self.error = None

        def logline(s: str) -> None:
            with self.lock:
                self.log.append(str(s))

        def worker():
            try:
                work(cfg, logline)
                err = None
            except (GradeError, OSError) as e:
                err = str(e)
            except Exception as e:      # never leave the job stuck
                err = f"{type(e).__name__}: {e}"
            # a push/pull changes server state: rescan while we're at it
            if err is None and action != "scan":
                try:
                    assignments = remote_list(cfg, lambda s: None)
                    with self.lock:
                        self.assignments = assignments
                        self.scanned_at = time.monotonic()
                except (GradeError, OSError):
                    pass
            with self.lock:
                self.error = err
                self.running = None

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}, 200


REMOTE = _State()


def _do_scan(cfg: dict, log) -> None:
    assignments = remote_list(cfg, log)
    with REMOTE.lock:
        REMOTE.assignments = assignments
        REMOTE.scanned_at = time.monotonic()
    log(f"found {len(assignments)} assignment(s) on {cfg['host']}")


def api_get(path: str):
    if path == "/api/remote":
        return REMOTE.snapshot(), 200
    return None


def api_post(path: str, data: dict):
    if path == "/api/remote/scan":
        return REMOTE.start("scan", _do_scan)
    if path == "/api/remote/push":
        folder = Path(str(data.get("path", "")))
        return REMOTE.start(
            "push", lambda cfg, log: push(folder, cfg, log))
    if path == "/api/remote/pull":
        folder = Path(str(data.get("path", "")))
        return REMOTE.start(
            "pull", lambda cfg, log: pull(folder, cfg, log))
    return None
