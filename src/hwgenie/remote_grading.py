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
    cfg.setdefault("service", "hwgrader")
    cfg.setdefault("port", 8461)
    return cfg


# ------------------------------------------------------ server version --

RELEASE_TARBALL = "https://github.com/tghyde/hwgenie/archive/refs/tags/v{v}.tar.gz"


def _version_cmd(cfg: dict) -> str:
    return (f"{cfg['python']} -c \"import importlib.metadata as m; "
            f"print(m.version('hwgenie'))\" 2>/dev/null || echo unknown; "
            f"systemctl is-active {cfg['service']} 2>/dev/null || echo unknown")


def server_version(cfg: dict, log=lambda s: None) -> dict:
    """{"version", "active", "error"} for the hwgenie install on the
    grading server (one ssh round trip)."""
    try:
        out = _run(["ssh", *SSH_OPTS, cfg["host"], _version_cmd(cfg)], log,
                   timeout=30)
    except GradeError as e:
        return {"version": None, "active": None, "error": str(e)}
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    version = lines[0] if lines and lines[0] != "unknown" else None
    active = lines[1] if len(lines) > 1 else "unknown"
    return {"version": version, "active": active, "error": None}


def upgrade_server(cfg: dict, version: str, log=lambda s: None) -> dict:
    """pip-install the tagged release into the server's venv and restart
    the service — the whole server-upgrade procedure — then re-read the
    version so the caller can confirm."""
    url = RELEASE_TARBALL.format(v=version)
    log(f"── grading server: upgrade hwgenie to v{version}")
    _run(["ssh", *SSH_OPTS, cfg["host"],
          f"{cfg['python']} -m pip install -q --upgrade '{url}'"], log,
         timeout=300)
    _run(["ssh", *SSH_OPTS, cfg["host"],
          f"systemctl restart {cfg['service']} && sleep 2 && "
          f"curl -s -o /dev/null -w 'grading page: %{{http_code}}\\n' "
          f"http://127.0.0.1:{cfg['port']}/grading"], log, timeout=120)
    info = server_version(cfg, log)
    log(f"server now runs hwgenie v{info.get('version') or '?'} "
        f"({info.get('active') or '?'})")
    return info


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


def find_template(folder: Path, tmpl: str | None) -> Path | None:
    """The submission template for a grading folder: the manifest's path
    (absolute, or relative to the folder), else the newest
    ``<assignment>/build/*submission*.tex`` beside it."""
    folder = Path(folder)
    if tmpl:
        p = Path(tmpl).expanduser()
        for cand in ([p] if p.is_absolute() else [folder / p]):
            if cand.is_file():
                return cand
    from .collect import newest_tex
    return newest_tex(folder.parent / "build")


def _bundle_template(stage: Path, log, folder: Path | None = None) -> None:
    """Copy the submission template into the staged folder and point the
    manifest at the copy, so the server can render the problem
    statements.  ``folder`` is the real grading folder (relative manifest
    paths and the ../build fallback resolve against it, not the stage)."""
    mf = stage / MANIFEST_NAME
    try:
        m = json.loads(mf.read_text())
    except (OSError, ValueError):
        return
    tmpl = (m.get("template") or {}).get("path")
    if tmpl and not Path(tmpl).is_absolute() and (stage / tmpl).is_file():
        return                       # already bundled in the folder itself
    found = find_template(folder or stage, tmpl)
    if found is not None:
        shutil.copy2(found, stage / "template.tex")
        m.setdefault("template", {})["path"] = "template.tex"
        mf.write_text(json.dumps(m, indent=2) + "\n")
        log(f"bundled the submission template ({found.name}) into the push")
    else:
        log(f"note: template {tmpl or '(none in manifest)'} not found and "
            "nothing in ../build — the problem-statement pane will be empty "
            "on the server")


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
        _bundle_template(stage, log, folder)
        # Once graders have started, the server's grades/ is the source
        # of truth: never overwrite it on a re-push (late additions,
        # rubric tweaks).  The first push seeds it; late.json holds the
        # instructor's late-work decisions and stays local.
        remote = f"{cfg['host']}:{cfg['root']}/{name}/"
        has_grades = _remote_has_grades(cfg, name, log)
        excl = ["--exclude", "late.json", "--exclude", "gradebook.*"]
        if has_grades:
            excl += ["--exclude", "grades/"]
            log("server already has grades/ — leaving it untouched")
        _run(["rsync", "-rlt", "--delete", *excl, f"{stage}/", remote], log)
    # touching the manifest makes a running grader server rebuild its
    # cached view of the assignment even when only submissions changed
    post = f"touch '{cfg['root']}/{name}/{MANIFEST_NAME}'"
    if cfg.get("owner"):
        post += f" && chown -R {cfg['owner']} '{cfg['root']}/{name}'"
    _run(["ssh", *SSH_OPTS, cfg["host"], post], log)
    log(f"pushed '{name}'")
    return name


def _remote_has_grades(cfg: dict, name: str, log) -> bool:
    try:
        _run(["ssh", *SSH_OPTS, cfg["host"],
              f"test -d '{cfg['root']}/{name}/grades'"], log)
        return True
    except GradeError:
        return False


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
