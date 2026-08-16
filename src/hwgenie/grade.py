"""Grading data model + ``hwgenie grade`` command.

Operates on a grading folder produced by ``hwgenie collect``.  Adds:

    rubric.yml            # optional: per-part labels and max points
    groups.yml            # optional: group slug -> member names
    grades/<slug>.json    # one file per submission — the single source of
                          # truth shared by the grading app, the AI-review
                          # skill (which writes ai_draft only), and export.

Schema of ``grades/<slug>.json``::

    {
      "slug": "Doe-Jane",
      "updated": "2026-08-13T00:00:00+00:00",
      "parts": {
        "1": {                       # keys are solution-box numbers, 1-based
          "score": 3.5,              # null until graded
          "max": 4,                  # from rubric.yml (default 5)
          "status": "graded",        # derived: "graded" iff score is set
          "comments": [              # anchored inline comments
            {"anchor": "exact tex substring or null", "text": "..."}
          ],
          "ai_draft": {              # written ONLY by the AI-review skill;
            "suggested_score": 3,    # the app displays it as a draft and
            "feedback": "...",       # never auto-copies it into the fields
            "issues": ["..."],       # above; issues are grader-facing
            "comments": [            # suggested anchored comments, each
              {"anchor": "...",      # accepted individually in the app
               "text": "..."}
            ]
          }
        }, ...
      }
    }

``rubric.yml`` (a tiny YAML subset, one line per solution box in template
order; max points optional)::

    parts:
    - 1.1: 4
    - 1.2: 4
    - 2.1a: 2

``groups.yml`` (group slug -> member names; a submission slug matching a
group key fans its grade out to every member at export time)::

    group-alpha:
    - Doe-Jane
    - Roe-Rick
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SOLUTION_BEGIN = r"\begin{solution}"
SOLUTION_END = r"\end{solution}"
MANIFEST_NAME = "manifest.json"
RUBRIC_NAME = "rubric.yml"
GROUPS_NAME = "groups.yml"
GRADES_DIR = "grades"


class GradeError(Exception):
    pass


# ------------------------------------------------------------ tex parsing --

def _strip_comment(line: str) -> str:
    return re.split(r"(?<!\\)%", line, maxsplit=1)[0]


def extract_solution_bodies(text: str) -> list[str]:
    """Bodies of ``\\begin{solution}...\\end{solution}``, in order.

    Delimiters are matched comment-aware (the submission preamble mentions
    ``\\begin{solution}`` inside comments); body text keeps its comments so
    anchors quote the student's tex verbatim.
    """
    bodies: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        code = _strip_comment(line)
        pos = 0
        while True:
            if current is None:
                i = code.find(SOLUTION_BEGIN, pos)
                if i == -1:
                    break
                start = i + len(SOLUTION_BEGIN)
                j = code.find(SOLUTION_END, start)
                if j != -1:
                    bodies.append(line[start:j])
                    pos = j + len(SOLUTION_END)
                    continue
                current = [line[start:]]
                break
            j = code.find(SOLUTION_END, pos)
            if j == -1:
                current.append(line[pos:] if pos else line)
                break
            current.append(line[pos:j])
            bodies.append("\n".join(current))
            current = None
            pos = j + len(SOLUTION_END)
    return bodies


def body_is_empty(body: str) -> bool:
    """True if the box holds nothing but whitespace and comments (e.g. the
    template's untouched '%Write your solution here')."""
    return not any(_strip_comment(ln).strip() for ln in body.splitlines())


def split_preamble(text: str) -> str:
    """Everything before \\begin{document} (for theorem defs + KaTeX macros)."""
    i = text.find(r"\begin{document}")
    return text[:i] if i != -1 else text


# ----------------------------------------------------------- rubric/groups --

DEFAULT_MAX = 5


@dataclasses.dataclass
class RubricPart:
    label: str
    max: float = DEFAULT_MAX


def _parse_rubric(text: str) -> list[RubricPart]:
    parts: list[RubricPart] = []
    in_parts = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^parts\s*:\s*$", line):
            in_parts = True
            continue
        if in_parts and line.startswith("-"):
            item = line[1:].strip()
            label, _, maxval = item.rpartition(":")
            if not label:  # "- 1.1" with no max
                label, maxval = maxval, ""
            label = label.strip().strip("\"'")
            maxval = maxval.split("#", 1)[0].strip()
            mx: float = DEFAULT_MAX
            if maxval:
                try:
                    mx = float(maxval)
                except ValueError:
                    raise GradeError(
                        f"{RUBRIC_NAME}: bad max points {maxval!r} for part "
                        f"{label!r}")
            parts.append(RubricPart(label=label, max=mx))
        else:
            in_parts = False
    return parts


def load_rubric(folder: Path, n_parts: int) -> list[RubricPart]:
    """Rubric padded to n_parts; default labels 'Part k', default max 5."""
    path = folder / RUBRIC_NAME
    parts = _parse_rubric(path.read_text()) if path.is_file() else []
    if len(parts) > n_parts > 0:
        raise GradeError(
            f"{RUBRIC_NAME} lists {len(parts)} parts but the assignment has "
            f"{n_parts} solution boxes")
    while len(parts) < n_parts:
        parts.append(RubricPart(label=f"Part {len(parts) + 1}"))
    return parts


def load_groups(folder: Path) -> dict[str, list[str]]:
    path = folder / GROUPS_NAME
    groups: dict[str, list[str]] = {}
    if not path.is_file():
        return groups
    current: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            if current is None:
                raise GradeError(f"{GROUPS_NAME}: member line before any group")
            member = line[1:].strip().strip("\"'")
            if member:
                groups[current].append(member)
        elif line.endswith(":"):
            current = line[:-1].strip().strip("\"'")
            groups[current] = []
        else:
            raise GradeError(f"{GROUPS_NAME}: cannot parse line {line!r}")
    return groups


# ---------------------------------------------------------------- manifest --

def load_manifest(folder: Path) -> dict:
    path = folder / MANIFEST_NAME
    if not path.is_file():
        raise GradeError(
            f"{path} not found — is {folder} a grading folder made by "
            "`hwgenie collect`?")
    return json.loads(path.read_text())


def infer_n_parts(manifest: dict) -> int:
    tmpl = manifest.get("template")
    if tmpl and tmpl.get("parts"):
        return int(tmpl["parts"])
    counts = [u.get("parts_found") for u in manifest.get("units", [])
              if u.get("parts_found")]
    if counts:
        return max(counts)
    raise GradeError(
        "cannot determine the number of parts: manifest has no template "
        "and no unit has a parsed tex (re-run collect with --template)")


# ------------------------------------------------------------ grades store --

def _default_part(rp: RubricPart) -> dict:
    return {"score": None, "max": rp.max, "status": "ungraded",
            "comments": [], "ai_draft": None}


class GradeStore:
    """Reads/writes grades/<slug>.json.

    Only ``score`` and ``comments`` are writable here; ``max`` mirrors the
    rubric, ``status`` is derived, and ``ai_draft`` (plus any fields another
    tool adds) is preserved verbatim so the AI-review skill and this app can
    share the files safely.
    """

    def __init__(self, folder: Path, rubric: list[RubricPart]):
        self.dir = Path(folder) / GRADES_DIR
        self.rubric = rubric

    def path(self, slug: str) -> Path:
        return self.dir / f"{slug}.json"

    def load(self, slug: str) -> dict:
        data: dict = {}
        p = self.path(slug)
        if p.is_file():
            data = json.loads(p.read_text())
        parts = data.setdefault("parts", {})
        for k, rp in enumerate(self.rubric, start=1):
            part = parts.setdefault(str(k), _default_part(rp))
            for key, val in _default_part(rp).items():
                part.setdefault(key, val)
            part["max"] = rp.max
            part["status"] = ("graded" if part.get("score") is not None
                              else "ungraded")
        data["slug"] = slug
        return data

    def update(self, slug: str, part: int | str, fields: dict) -> dict:
        data = self.load(slug)
        p = data["parts"].get(str(part))
        if p is None:
            raise GradeError(f"no part {part!r} (parts are 1..{len(self.rubric)})")
        if "score" in fields:
            score = fields["score"]
            if score is not None:
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    raise GradeError(f"bad score {fields['score']!r}")
                if not math.isfinite(score) or score < 0:
                    raise GradeError(f"bad score {fields['score']!r}")
                if score == int(score):
                    score = int(score)
            p["score"] = score
            p["status"] = "graded" if score is not None else "ungraded"
        if "comments" in fields:
            if not isinstance(fields["comments"], list):
                raise GradeError("comments must be a list")
            p["comments"] = [
                {"anchor": (c.get("anchor") or None),
                 "text": str(c.get("text", ""))}
                for c in fields["comments"] if isinstance(c, dict)
            ]
        data["updated"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        self.save(slug, data)
        return data

    def save(self, slug: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path(slug).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path(slug))

    def progress(self, slugs: list[str]) -> tuple[int, int]:
        graded = 0
        for slug in slugs:
            data = self.load(slug)
            graded += sum(1 for p in data["parts"].values()
                          if p["status"] == "graded")
        return graded, len(slugs) * len(self.rubric)


# --------------------------------------------------------------------- cli --

def add_parser(sub) -> None:
    p = sub.add_parser(
        "grade",
        help="Grade a collected submissions folder (web app with --gui).",
    )
    p.add_argument("folder", nargs="?", default=".",
                   help="Grading folder with manifest.json (default: cwd). "
                        "With --gui, anything else opens the assignment "
                        "picker rooted there.")
    p.add_argument("--gui", action="store_true",
                   help="Open the grading web app (hwGrader) in a browser.")
    p.add_argument("--port", type=int, default=0,
                   help="Port for --gui (default: an unused one).")
    p.add_argument("--no-browser", action="store_true",
                   help="With --gui: print the URL instead of opening it.")
    p.add_argument("--auto-exit", action="store_true",
                   help="With --gui: shut down when the browser tab closes "
                        "(used by the hwGrader app launcher).")


def run_grade(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    try:
        if args.gui:
            from .grade_gui import serve_app
            return serve_app(folder, port=args.port,
                             open_browser=not args.no_browser,
                             auto_exit=args.auto_exit)
        return _print_summary(folder)
    except GradeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _print_summary(folder: Path) -> int:
    manifest = load_manifest(folder)
    n_parts = infer_n_parts(manifest)
    rubric = load_rubric(folder, n_parts)
    store = GradeStore(folder, rubric)
    units = manifest["units"]
    width = max(len(u["slug"]) for u in units)
    for u in units:
        data = store.load(u["slug"])
        done = sum(1 for p in data["parts"].values() if p["status"] == "graded")
        tex = {"original": "", "reconstructed": " [tex*]", None: " [no tex]"}[
            u.get("tex_source")]
        print(f"  {u['slug']:<{width}}  {done:>2}/{n_parts}{tex}")
    graded, total = store.progress([u["slug"] for u in units])
    print(f"Graded {graded}/{total} parts "
          f"({0 if not total else round(100 * graded / total)}%). "
          "Run with --gui to grade in the browser.")
    return 0
