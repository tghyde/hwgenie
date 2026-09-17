"""The assignment rubric file, read and written as a whole.

``<grading folder>/rubric.yml`` holds the assignment's settings (``due:``,
``timezone:``) and one line per solution box (``parts:``).  ``grade.py``
parses the parts and ``late.py`` the settings; this module is the editor
behind the app's Rubric & deadline panel: it turns the file into a JSON
payload, validates an edited payload and writes the file back, and it
seeds a fresh grading folder's parts from the template's problem/box
structure so labels read "2.3" rather than "Part 7".
"""

from __future__ import annotations

import re
from pathlib import Path

from . import late as late_mod
from .grade import (DEFAULT_MAX, RUBRIC_NAME, GradeError, RubricPart,
                    _parse_rubric, _strip_comment, infer_n_parts,
                    load_manifest)

HEADER = [
    "# Rubric for {name} — one line per solution box, in template order.",
    '# Format: "- <label>: <max points>"; add a trailing "ec" to mark an',
    '# extra-credit part (e.g. "- 2.4: 3 ec").  Edit here or in the app',
    "# (Grading → Collect from Moodle → Rubric & deadline).",
]


# ---------------------------------------------------------------- defaults --

def template_box_labels(template: Path | None) -> list[str]:
    """One label per solution box in template order, ``<problem>.<k>``
    (box k of that problem); boxes outside any problem environment get
    their global ordinal.  Empty when the template is missing."""
    if template is None:
        return []
    try:
        text = Path(template).read_text(errors="replace")
    except OSError:
        return []
    from .grade_gui import template_problem_blocks
    labels: dict[int, str] = {}
    for blk in template_problem_blocks(text):
        for k, box in enumerate(blk["boxes"], start=1):
            labels[box] = f"{blk['num']}.{k}"
    n = _count_boxes(text)
    return [labels.get(i, str(i)) for i in range(1, n + 1)]


def _count_boxes(text: str) -> int:
    return sum(_strip_comment(ln).count(r"\begin{solution}")
               for ln in text.splitlines())


def default_parts(template: Path | None, n_parts: int) -> list[RubricPart]:
    labels = template_box_labels(template)
    if len(labels) != n_parts:
        labels = [str(i) for i in range(1, n_parts + 1)]
    return [RubricPart(label=lb) for lb in labels]


# ------------------------------------------------------------ read/write --

def _header_lines(text: str) -> list[str]:
    """Leading comment lines of an existing file (kept on rewrite)."""
    out = []
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            out.append(raw.rstrip())
        elif raw.strip():
            break
    return out


def write_rubric(folder: Path, parts: list[RubricPart], *,
                 due: str | None = None, timezone: str | None = None,
                 name: str | None = None) -> Path:
    """Rewrite rubric.yml from scratch: header comment, settings, parts."""
    folder = Path(folder)
    path = folder / RUBRIC_NAME
    header = _header_lines(path.read_text()) if path.is_file() else []
    if not header:
        header = [ln.format(name=name or late_mod.assignment_key(folder))
                  for ln in HEADER]
    lines = list(header)
    if due:
        lines.append(f"due: {due}")
    if timezone:
        lines.append(f"timezone: {timezone}")
    lines.append("parts:")
    for rp in parts:
        mx = int(rp.max) if float(rp.max).is_integer() else rp.max
        lines.append(f"- {rp.label}: {mx}" + (" ec" if rp.ec else ""))
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def seed_parts(folder: Path, template: Path | None, n_parts: int) -> bool:
    """Give a grading folder a parts block if it has none yet (settings
    already in the file are kept).  Returns True when the file changed."""
    folder = Path(folder)
    path = folder / RUBRIC_NAME
    text = path.read_text() if path.is_file() else ""
    if _parse_rubric(text) or n_parts <= 0:
        return False
    st = late_mod.read_settings(folder)
    write_rubric(folder, default_parts(template, n_parts),
                 due=st.get("due"), timezone=st.get("timezone"))
    return True


# ------------------------------------------------------------- app payload --

def _problems(template: Path | None) -> list[dict]:
    if template is None:
        return []
    try:
        text = Path(template).read_text(errors="replace")
    except OSError:
        return []
    from .grade_gui import template_problem_blocks
    return [{"num": b["num"], "boxes": b["boxes"]}
            for b in template_problem_blocks(text)]


def payload(folder: Path) -> dict:
    """What the editor shows for a grading folder: the file's settings and
    parts (padded/defaulted like the grader sees them), the template's
    problem → boxes map and the default labels, plus the file's own text."""
    folder = Path(folder)
    manifest = load_manifest(folder)
    n_parts = infer_n_parts(manifest)
    tmpl = (manifest.get("template") or {}).get("path")
    template = Path(tmpl) if tmpl else None
    path = folder / RUBRIC_NAME
    text = path.read_text() if path.is_file() else ""
    parts = _parse_rubric(text)
    if len(parts) > n_parts:
        raise GradeError(
            f"{RUBRIC_NAME} lists {len(parts)} parts but the assignment has "
            f"{n_parts} solution boxes")
    defaults = default_parts(template, n_parts)
    if not parts:
        parts = defaults
    else:
        parts = parts + defaults[len(parts):]
    st = late_mod.read_settings(folder)
    return {
        "ok": True, "folder": str(folder), "path": str(path),
        "exists": path.is_file(), "name": late_mod.assignment_key(folder),
        "due": st.get("due"), "timezone": st.get("timezone"),
        "n_parts": n_parts,
        "parts": [{"label": rp.label, "max": rp.max, "ec": rp.ec}
                  for rp in parts],
        "default_labels": [rp.label for rp in defaults],
        "problems": _problems(template),
        "template": str(template) if template else None,
        "text": text,
    }


_LABEL_RE = re.compile(r"^[^\s:#\"'][^:#\"']*$")


def save(folder: Path, data: dict) -> dict:
    """Validate an edited payload and write rubric.yml; returns the fresh
    payload.  ``data``: {due, timezone, parts: [{label, max, ec}]}."""
    folder = Path(folder)
    manifest = load_manifest(folder)
    n_parts = infer_n_parts(manifest)
    due = str(data.get("due") or "").strip()
    tz = str(data.get("timezone") or "").strip()
    if tz:
        try:
            late_mod.course_tz(tz)
        except late_mod.LateError as e:
            raise GradeError(str(e))
    if due:
        try:
            late_mod.parse_due(due, late_mod.course_tz(tz or None))
        except late_mod.LateError as e:
            raise GradeError(str(e))
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or len(raw_parts) != n_parts:
        raise GradeError(f"the rubric needs exactly {n_parts} parts (one "
                         "per solution box in the template)")
    parts: list[RubricPart] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_parts, start=1):
        if not isinstance(item, dict):
            raise GradeError(f"part {i}: bad entry")
        label = str(item.get("label") or "").strip()
        if not label or not _LABEL_RE.match(label):
            raise GradeError(f"part {i}: label {label!r} may not be empty "
                             "or contain : # or quotes")
        if label in seen:
            raise GradeError(f"part {i}: label {label!r} is used twice")
        seen.add(label)
        mx = item.get("max", DEFAULT_MAX)
        try:
            mxf = float(DEFAULT_MAX if mx in (None, "") else mx)
        except (TypeError, ValueError):
            raise GradeError(f"part {label}: bad max points {mx!r}")
        if not (mxf >= 0) or mxf != mxf or mxf == float("inf"):
            raise GradeError(f"part {label}: bad max points {mx!r}")
        parts.append(RubricPart(label=label, max=mxf, ec=bool(item.get("ec"))))
    write_rubric(folder, parts, due=due or None, timezone=tz or None)
    return payload(folder)
