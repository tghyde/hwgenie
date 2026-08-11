"""hwgenie sync-template — pull shared files from the course template.

Run inside a course repo. Fetches the files listed in the template repo's
``sync-manifest.txt`` (the manifest lives in the template, so the list has
one home), shows what changed, commits, and pushes. Course content —
sources, course.yml, instructions.tex, static/ — is never touched unless
the manifest names it.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_TEMPLATE = "tghyde/course-template"
MANIFEST_NAME = "sync-manifest.txt"


def parse_manifest(text: str) -> List[str]:
    """Manifest = one repo-relative path per line; # comments and blanks ok."""
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line.lstrip("/"))
    return paths


def classify(local: Optional[str], remote: str) -> str:
    """'unchanged' | 'new' | 'update' for one synced file."""
    if local is None:
        return "new"
    return "unchanged" if local == remote else "update"


def file_diff(path: str, local: Optional[str], remote: str,
              context: int = 3) -> str:
    return "".join(difflib.unified_diff(
        (local or "").splitlines(keepends=True),
        remote.splitlines(keepends=True),
        fromfile=f"{path} (this repo)",
        tofile=f"{path} (template)",
        n=context,
    ))


def _fetch_raw(template: str, path: str) -> Optional[str]:
    proc = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.raw",
         f"repos/{template}/contents/{path}"],
        capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def run_sync_template(args) -> int:
    repo = Path(args.dir or ".").resolve()
    if not (repo / "course.yml").is_file():
        print(f"error: {repo} does not look like a course repo "
              "(no course.yml)", file=sys.stderr)
        return 1
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 1

    template = args.template
    manifest_text = _fetch_raw(template, MANIFEST_NAME)
    if manifest_text is None:
        print(f"error: could not fetch {MANIFEST_NAME} from {template} "
              "(is gh authenticated?)", file=sys.stderr)
        return 1
    paths = parse_manifest(manifest_text)
    print(f"Syncing {len(paths)} file(s) from {template}:")

    changed: List[Tuple[str, str]] = []  # (path, remote content)
    for path in paths:
        remote = _fetch_raw(template, path)
        if remote is None:
            print(f"  {path}: MISSING in template — skipped")
            continue
        local_file = repo / path
        local = (local_file.read_text(encoding="utf-8")
                 if local_file.is_file() else None)
        state = classify(local, remote)
        print(f"  {path}: {state}")
        if state != "unchanged":
            changed.append((path, remote))
            if args.diff:
                print(file_diff(path, local, remote))

    if not changed:
        print("Everything already up to date.")
        return 0
    if args.dry_run:
        print(f"\nDry run: {len(changed)} file(s) would be updated. "
              "Re-run without --dry-run to apply.")
        return 0

    for path, remote in changed:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(remote, encoding="utf-8")

    files = [p for p, _ in changed]
    subprocess.run(["git", "add", "--"] + files, cwd=repo, check=True)
    msg = ("Sync shared files from course-template: "
           + ", ".join(files))
    commit = subprocess.run(["git", "commit", "-q", "-m", msg],
                            cwd=repo, capture_output=True, text=True)
    if commit.returncode != 0:
        print("error: git commit failed:\n" + commit.stderr, file=sys.stderr)
        return 1
    if args.no_push:
        print(f"Updated + committed {len(files)} file(s); not pushed "
              "(--no-push).")
    else:
        push = subprocess.run(["git", "push", "-q"], cwd=repo,
                              capture_output=True, text=True)
        if push.returncode != 0:
            print("error: git push failed (pull first if Overleaf pushed "
                  "recently):\n" + push.stderr, file=sys.stderr)
            return 1
        print(f"Updated, committed, and pushed {len(files)} file(s). "
              "The site will rebuild automatically.")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "sync-template",
        help="Update this course repo's shared files (style, workflow) "
             "from the course template.",
    )
    p.add_argument("--dir", help="Course repo root (default: cwd).")
    p.add_argument("--template", default=DEFAULT_TEMPLATE,
                   help=f"Template repo (default: {DEFAULT_TEMPLATE}).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing anything.")
    p.add_argument("--diff", action="store_true",
                   help="Print a unified diff for each changed file.")
    p.add_argument("--no-push", action="store_true",
                   help="Commit locally but do not push.")
