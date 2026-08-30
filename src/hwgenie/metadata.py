"""Parse the hwgenie metadata block from a source .tex file.

Two formats are supported:

New (v2) — flexible keys, any order::

    %===hwgenie===
    % type      = problemset
    % number    = 3
    % title     = Digits and Sage
    % course    = Math 261
    % semester  = Fall 2025
    % solutions = 2025-10-15
    %=============

Legacy (hw_gen.py) — recognized for backward compatibility::

    %Problem Set Data
    %number = 3
    %course = 261
    %semester = Fall 2025
    %path = /some/absolute/path
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Tuple

V2_START = re.compile(r"^%\s*={2,}\s*hwgenie\s*={2,}\s*$", re.IGNORECASE)
V2_END = re.compile(r"^%\s*={2,}\s*$")
KV_LINE = re.compile(r"^%\s*([A-Za-z][\w-]*)\s*=\s*(.*?)\s*$")
LEGACY_START = re.compile(r"^%\s*Problem Set Data\s*$", re.IGNORECASE)
# LaTeX-variable metadata (defined in hwgenie.sty): \hwnumber{3} etc.
HWCMD_RE = re.compile(r"\\hw(type|number|title|solutions|release|due)\s*\{([^{}]*)\}")


class MetadataError(ValueError):
    pass


@dataclass
class Metadata:
    number: str
    course: Optional[str] = None    # display form, e.g. "Math 261"
    semester: Optional[str] = None  # e.g. "Fall 2025"
    doc_type: str = "problemset"
    title: Optional[str] = None
    solutions_release: Optional[str] = None  # date string, "manual", or "released"
    release: Optional[str] = None  # gate for the whole assignment (None = live)
    due: Optional[str] = None      # display text, e.g. "Friday, Sep 4 at 11:59pm"
    legacy_path: Optional[str] = None
    fmt: str = "v2"      # "v2" or "legacy"
    span: Tuple[int, int] = (0, 0)  # char span of the block (for removal)
    raw: dict = field(default_factory=dict)


def _line_spans(text: str):
    """Yield (start, end_exclusive_incl_newline, line_without_newline)."""
    start = 0
    for m in re.finditer(r"\n", text):
        yield start, m.end(), text[start : m.start()]
        start = m.end()
    if start < len(text):
        yield start, len(text), text[start:]


def parse_metadata(text: str) -> Metadata:
    lines = list(_line_spans(text))
    # \hw... commands override comment-block values wherever both exist.
    cmd_raw = {
        m.group(1): m.group(2).strip()
        for m in HWCMD_RE.finditer(text)
        if m.group(2).strip()
    }

    # --- v2 format ---
    for i, (start, _end, line) in enumerate(lines):
        if V2_START.match(line):
            raw = {}
            span_end = lines[i][1]
            for j in range(i + 1, len(lines)):
                _s, e, l = lines[j]
                if V2_END.match(l):
                    span_end = e
                    break
                m = KV_LINE.match(l)
                if m:
                    raw[m.group(1).lower()] = m.group(2)
                    span_end = e
                elif l.strip() == "%" or not l.strip():
                    span_end = e
                else:
                    raise MetadataError(
                        f"Unrecognized line inside hwgenie metadata block: {l!r}"
                    )
            else:
                raise MetadataError(
                    "hwgenie metadata block is missing its closing '%====' line."
                )
            raw.update(cmd_raw)
            return _build(raw, fmt="v2", span=(start, span_end))

    # --- legacy format ---
    for i, (start, _end, line) in enumerate(lines):
        if LEGACY_START.match(line):
            raw = {}
            span_end = lines[i][1]
            for j in range(i + 1, len(lines)):
                _s, e, l = lines[j]
                m = KV_LINE.match(l)
                if not m:
                    break
                raw[m.group(1).lower()] = m.group(2)
                span_end = e
            raw.update(cmd_raw)
            return _build(raw, fmt="legacy", span=(start, span_end))

    if cmd_raw:
        return _build(cmd_raw, fmt="commands", span=(0, 0))

    raise MetadataError(
        "No metadata found. Use \\hwnumber{...}/\\hwtitle{...} (hwgenie.sty) "
        "or a '%===hwgenie===' comment block near the top of the document."
    )


def _build(raw: dict, fmt: str, span: Tuple[int, int]) -> Metadata:
    doc_type = raw.get("type", "problemset").lower()
    if not raw.get("number") and doc_type in ("problemset", "lesson"):
        raise MetadataError("Metadata block is missing the required 'number' key.")

    # course/semester may instead come from course.yml (merged at build time).
    course = raw.get("course")
    if course and (fmt == "legacy" or re.fullmatch(r"\d+[A-Za-z]?", course)):
        course = f"Math {course}"

    return Metadata(
        number=raw.get("number", ""),
        course=course,
        semester=raw.get("semester"),
        doc_type=doc_type,
        title=raw.get("title"),
        solutions_release=raw.get("solutions"),
        release=raw.get("release"),
        due=raw.get("due"),
        legacy_path=raw.get("path"),
        fmt=fmt,
        span=span,
        raw=raw,
    )


def latex_plain(s):
    """Down-convert simple LaTeX escapes in short text (titles) for HTML use."""
    if not s:
        return s
    for a, b in (("\\&", "&"), ("\\%", "%"), ("\\#", "#"),
                 ("\\_", "_"), ("~", " ")):
        s = s.replace(a, b)
    return s
