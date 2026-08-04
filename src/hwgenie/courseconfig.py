"""Per-repo course configuration (course.yml).

A deliberately tiny flat key: value format (a YAML subset) — no dependency
needed.  Recognized keys: course, title, semester, instructor, plus anything
else the templates may want later.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

LINE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$")


def parse_course_config(text: str) -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        # strip full-line trailing comments and symmetric quotes
        value = re.sub(r"\s+#.*$", "", value)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            cfg[m.group(1).lower()] = value
    return cfg


def find_course_config(start_dir: Path, max_up: int = 4) -> Optional[Path]:
    """Look for course.yml in start_dir and up to `max_up` parent dirs."""
    d = Path(start_dir).resolve()
    for _ in range(max_up + 1):
        candidate = d / "course.yml"
        if candidate.exists():
            return candidate
        if d.parent == d:
            break
        d = d.parent
    return None


def load_course_config(path: Path) -> Dict[str, str]:
    return parse_course_config(Path(path).read_text(encoding="utf-8"))
