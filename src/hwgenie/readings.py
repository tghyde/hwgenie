"""Reading assignments: a repo-root readings.tex data file rendered as a
section on the course home page.

The file holds \\reading{<due date>}{<description>} entries; the description
is ordinary LaTeX (\\href, \\emph, math). Entries render in file order, so
the file is kept newest-first — the top entry is the current assignment and
is the only card unfolded by default on the site.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

READINGS_FILENAME = "readings.tex"


@dataclass
class Reading:
    due: str
    body: str


def _skip_comment(text: str, i: int) -> int:
    """i points at an unescaped %; return index just past the comment."""
    j = text.find("\n", i)
    return len(text) if j == -1 else j + 1


def _read_group(text: str, i: int) -> Optional[Tuple[str, int]]:
    """Parse one {balanced} group starting at or after i (skipping
    whitespace/comments). Returns (contents, index past group) or None.
    Braces are counted comment-aware, and backslash + next char is atomic
    so \\{ \\} \\% never miscount."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "%":
            i = _skip_comment(text, i)
        else:
            break
    if i >= n or text[i] != "{":
        return None
    depth = 0
    start = i + 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            i = _skip_comment(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None


def parse_readings(text: str) -> List[Reading]:
    """Scan text for \\reading{due}{description} entries, in file order.
    Comment-aware: a commented-out \\reading line is ignored."""
    out: List[Reading] = []
    i, n = 0, len(text)
    token = "\\reading"
    while i < n:
        c = text[i]
        if c == "%":
            i = _skip_comment(text, i)
            continue
        if c != "\\":
            i += 1
            continue
        if not text.startswith(token, i) or (
            i + len(token) < n and text[i + len(token)].isalpha()
        ):
            i += 2  # backslash + next char are atomic (handles \% \{ \\)
            continue
        got = _read_group(text, i + len(token))
        if not got:
            i += len(token)
            continue
        due, j = got
        got = _read_group(text, j)
        if not got:
            i = j
            continue
        body, j = got
        due, body = due.strip(), body.strip()
        if due or body:
            out.append(Reading(due=due, body=body))
        i = j
    return out


def load_readings(repo_root: Path) -> List[Reading]:
    path = Path(repo_root) / READINGS_FILENAME
    if not path.is_file():
        return []
    return parse_readings(path.read_text(encoding="utf-8"))
