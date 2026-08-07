"""Create a new course repository from the course template.

Steps automated here (see SETUP.md in the template repo):
  1. gh repo create <owner>/<repo> --private --template <template>
  2. clone locally, fill @@...@@ placeholders, commit, push
  3. enable GitHub Pages (build_type=workflow)
  4. optionally set repo variable DEPLOY_PAGES=true
  5. optionally wait for the first Actions build

Run headless via ``hwgenie new-course ...`` or as a local browser wizard
via ``hwgenie new-course --gui``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

DEFAULT_TEMPLATE = "tghyde/course-template"

# Placeholder tokens in the template -> form field names.
TOKEN_FIELDS = {
    "@@COURSE@@": "course",          # "Math 301"
    "@@COURSETITLE@@": "title",      # "Real Analysis"
    "@@SEMESTER@@": "semester",      # "Spring 2026"
    "@@INSTRUCTOR@@": "instructor",  # "Prof. Trevor Hyde"
    "@@OFFICE@@": "office",
    "@@EMAIL@@": "email",
}

# File types eligible for placeholder replacement.
TEXT_SUFFIXES = {".tex", ".yml", ".yaml", ".md", ".html", ".css", ".sty"}

DEFAULTS_PATH = Path.home() / ".config" / "hwgenie" / "defaults.json"


def derive_repo_name(course: str, semester: str) -> str:
    """"Math 301", "Spring 2026" -> "math301-spring2026"."""
    a = re.sub(r"[^a-z0-9]", "", course.lower())
    b = re.sub(r"[^a-z0-9]", "", semester.lower())
    name = f"{a}-{b}".strip("-")
    return name or "new-course"


def fill_placeholders(root: Path, values: Dict[str, str]) -> List[Path]:
    """Replace @@TOKEN@@ placeholders in text files under root.

    Returns the list of files that changed.
    """
    changed: List[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = text
        for token, key in TOKEN_FIELDS.items():
            if key in values and values[key]:
                new = new.replace(token, values[key])
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path)
    return changed


def load_defaults() -> Dict[str, str]:
    try:
        return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_defaults(values: Dict[str, str]) -> None:
    keep = {k: values[k] for k in
            ("instructor", "office", "email", "parent_dir") if values.get(k)}
    try:
        DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULTS_PATH.write_text(json.dumps(keep, indent=2), encoding="utf-8")
    except OSError:
        pass  # defaults are a convenience; never fail the run over them


class StepError(Exception):
    pass


@dataclass
class CreateRequest:
    course: str
    title: str
    semester: str
    instructor: str = ""
    office: str = ""
    email: str = ""
    theme: str = "slate"
    repo: str = ""                 # derived from course/semester if empty
    parent_dir: str = ""           # local folder to clone into
    deploy: bool = True            # set DEPLOY_PAGES=true
    wait_for_build: bool = True    # poll the first Actions run
    template: str = DEFAULT_TEMPLATE

    def resolved_repo(self) -> str:
        return self.repo.strip() or derive_repo_name(self.course, self.semester)


@dataclass
class CreateResult:
    ok: bool
    repo_url: str = ""
    site_url: str = ""
    local_path: str = ""
    build_state: str = ""   # "success" | "failure" | "running" | "skipped"
    error: str = ""
    next_steps: List[str] = field(default_factory=list)


def _run(cmd: List[str], log: Callable[[str], None], *, cwd: Optional[Path] = None,
         check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise StepError(f"command failed: {' '.join(cmd)}\n{detail}")
    return proc


def create_course(req: CreateRequest, log: Callable[[str], None]) -> CreateResult:
    """Run the whole pipeline. Raises nothing; errors land in the result."""
    try:
        return _create_course(req, log)
    except StepError as e:
        log(f"ERROR: {e}")
        return CreateResult(ok=False, error=str(e))


def _create_course(req: CreateRequest, log) -> CreateResult:
    for fld in ("course", "title", "semester"):
        if not getattr(req, fld).strip():
            raise StepError(f"missing required field: {fld}")

    repo = req.resolved_repo()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        raise StepError(f"invalid repo name: {repo!r}")

    log("Checking GitHub CLI authentication...")
    _run(["gh", "auth", "status"], log, quiet=True)
    owner = _run(["gh", "api", "user", "--jq", ".login"], log,
                 quiet=True).stdout.strip()
    full = f"{owner}/{repo}"
    repo_url = f"https://github.com/{full}"
    site_url = f"https://{owner}.github.io/{repo}/"

    if subprocess.run(["gh", "repo", "view", full], capture_output=True).returncode == 0:
        raise StepError(f"repo {full} already exists — pick a different name "
                        f"or delete the old repo first")

    parent = Path(req.parent_dir).expanduser() if req.parent_dir else Path.cwd()
    local = parent / repo
    if local.exists():
        raise StepError(f"local folder already exists: {local}")
    parent.mkdir(parents=True, exist_ok=True)

    log(f"Creating private repo {full} from template {req.template}...")
    _run(["gh", "repo", "create", full, "--private",
          "--template", req.template], log)

    # The template copy happens asynchronously on GitHub's side.
    log("Waiting for GitHub to finish copying the template...")
    for _ in range(30):
        proc = _run(["gh", "api", f"repos/{full}/commits?per_page=1",
                     "--jq", "length"], log, check=False, quiet=True)
        if proc.returncode == 0 and proc.stdout.strip() not in ("", "0"):
            break
        time.sleep(2)
    else:
        raise StepError("template copy never completed on GitHub's side")

    log(f"Cloning into {local}...")
    _run(["gh", "repo", "clone", full, str(local)], log)

    log("Filling in course data (placeholders)...")
    values = {key: getattr(req, key).strip() for key in
              ("course", "title", "semester", "instructor", "office", "email")}
    changed = fill_placeholders(local, values)
    for p in changed:
        log(f"  updated {p.relative_to(local)}")
    if req.theme and req.theme != "slate":
        yml = local / "course.yml"
        yml.write_text(yml.read_text(encoding="utf-8").replace(
            "theme: slate", f"theme: {req.theme}"), encoding="utf-8")
        log(f"  updated course.yml (theme: {req.theme})")

    log("Committing and pushing course data...")
    _run(["git", "add", "-A"], log, cwd=local, quiet=True)
    _run(["git", "commit", "-q", "-m",
          f"Fill in course data for {req.course}, {req.semester}"],
         log, cwd=local, quiet=True)
    _run(["git", "push", "-q"], log, cwd=local, quiet=True)

    log("Enabling GitHub Pages (source: GitHub Actions)...")
    proc = _run(["gh", "api", "-X", "POST", f"repos/{full}/pages",
                 "-f", "build_type=workflow"], log, check=False)
    if proc.returncode != 0:
        if "409" in proc.stderr or "already exists" in proc.stderr.lower():
            _run(["gh", "api", "-X", "PUT", f"repos/{full}/pages",
                  "-f", "build_type=workflow"], log, check=False)
        else:
            raise StepError(f"could not enable Pages:\n{proc.stderr.strip()}")

    if req.deploy:
        log("Setting repository variable DEPLOY_PAGES=true...")
        _run(["gh", "variable", "set", "DEPLOY_PAGES", "-b", "true",
              "-R", full], log)
    else:
        log("Skipping DEPLOY_PAGES (site stays offline until you set it).")

    build_state = "skipped"
    if req.wait_for_build:
        build_state = _wait_for_build(full, log)
    else:
        log("Not waiting for the build — check the repo's Actions tab.")

    result = CreateResult(
        ok=build_state in ("success", "skipped", "running"),
        repo_url=repo_url, site_url=site_url if req.deploy else "",
        local_path=str(local), build_state=build_state,
    )
    result.next_steps = [
        "Overleaf: New Project -> Import from GitHub -> pick "
        f"{repo} (https://www.overleaf.com/project)",
        "In Overleaf, set Menu -> Settings -> Main document to the file "
        "you're writing (e.g. source/problem-sets/ps01/ps01.tex).",
        "Edit the placeholder content: static/intro.html, "
        "source/handouts/syllabus.tex, instructions.tex, and the skeleton "
        "ps01/lesson1 (they ship unreleased: hwrelease is set to no).",
    ]
    if not req.deploy:
        result.next_steps.append(
            "When ready to go live: create repo variable DEPLOY_PAGES=true "
            "(Settings -> Secrets and variables -> Actions -> Variables) "
            "and re-run the workflow.")
    if build_state == "failure":
        result.error = ("The first site build failed — open the repo's "
                        "Actions tab to see the log.")
    save_defaults({**values, "parent_dir": str(parent)})
    return result


def _wait_for_build(full: str, log, timeout_s: int = 25 * 60) -> str:
    log("Waiting for the first site build (TeX Live setup makes the first "
        "run slow — typically 5-10 minutes)...")
    start = time.time()
    last = ""
    while time.time() - start < timeout_s:
        proc = _run(["gh", "run", "list", "-R", full, "--limit", "1",
                     "--json", "status,conclusion"], log, check=False,
                    quiet=True)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                runs = json.loads(proc.stdout)
            except ValueError:
                runs = []
            if runs:
                status = runs[0].get("status", "")
                if status != last:
                    log(f"  build status: {status}")
                    last = status
                if status == "completed":
                    conclusion = runs[0].get("conclusion", "")
                    log(f"  build finished: {conclusion}")
                    return "success" if conclusion == "success" else "failure"
        time.sleep(15)
    log("  gave up waiting; the build is still running.")
    return "running"


# ----------------------------------------------------------------- CLI --

def run_new_course(args) -> int:
    if args.gui:
        from .new_course_gui import serve_wizard
        return serve_wizard(port=args.port)

    defaults = load_defaults()
    req = CreateRequest(
        course=args.course or "",
        title=args.title or "",
        semester=args.semester or "",
        instructor=args.instructor or defaults.get("instructor", ""),
        office=args.office or defaults.get("office", ""),
        email=args.email or defaults.get("email", ""),
        theme=args.theme,
        repo=args.repo or "",
        parent_dir=args.dir or defaults.get("parent_dir", ""),
        deploy=not args.no_deploy,
        wait_for_build=not args.no_wait,
        template=args.template,
    )
    result = create_course(req, print)
    print()
    if result.ok:
        print(f"Repo:  {result.repo_url}")
        if result.site_url:
            print(f"Site:  {result.site_url}")
        print(f"Local: {result.local_path}")
        print("\nNext steps:")
        for i, step in enumerate(result.next_steps, 1):
            print(f"  {i}. {step}")
        return 0
    print(f"error: {result.error}")
    return 1


def add_parser(sub) -> None:
    p = sub.add_parser(
        "new-course",
        help="Create a new course repo from the course template "
             "(use --gui for the browser wizard).",
    )
    p.add_argument("--gui", action="store_true",
                   help="Open the browser wizard instead of running headless.")
    p.add_argument("--port", type=int, default=0,
                   help="Port for the wizard (default: random).")
    p.add_argument("--course", help='Course code, e.g. "Math 301".')
    p.add_argument("--title", help='Course title, e.g. "Real Analysis".')
    p.add_argument("--semester", help='Semester, e.g. "Spring 2026".')
    p.add_argument("--instructor", help="Instructor name for the syllabus.")
    p.add_argument("--office", help="Office for the syllabus.")
    p.add_argument("--email", help="Email for the syllabus.")
    p.add_argument("--theme", default="slate", help="Site theme.")
    p.add_argument("--repo", help="Repo name (default: derived, e.g. "
                                  "math301-spring2026).")
    p.add_argument("--dir", help="Parent folder for the local clone "
                                 "(default: last used, else cwd).")
    p.add_argument("--no-deploy", action="store_true",
                   help="Do not set DEPLOY_PAGES (site stays offline).")
    p.add_argument("--no-wait", action="store_true",
                   help="Do not wait for the first Actions build.")
    p.add_argument("--template", default=DEFAULT_TEMPLATE,
                   help=f"Template repo (default: {DEFAULT_TEMPLATE}).")
