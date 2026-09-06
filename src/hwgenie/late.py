"""Late-submission policy: timestamps, tiers, instructor decisions, and the
course-level gradebook that remembers who has used their free late.

Three files hold the three kinds of fact, so that graders' work, Moodle's
facts and the instructor's decisions never overwrite each other:

``manifest.json`` (facts from Moodle, written by ``hwgenie collect``)
    per unit: ``submitted`` (ISO time with offset, from the worksheet's
    "Last modified (submission)" column or the zip's file times),
    ``resubmitted`` (a later re-upload seen by ``collect --update``), and
    ``collected`` (when the unit entered the grading folder).

``rubric.yml`` (assignment settings, pushed to the grading server)
    ``due: 2026-09-04 23:59`` and optionally ``timezone: America/New_York``
    (default: the machine's local zone).  Without ``due`` nothing is late.

``late.json`` (instructor decisions; never pushed or pulled)::

    {"Doe-Jane": {"action": "waive", "note": "2 minutes late",
                  "extension": null, "decided": "2026-09-06T..."}}

    action: "auto" (follow the policy — the default when absent),
            "free" (spend the free late), "apply" (penalty even if a free
            late is available), "waive" (no penalty), "extension" (deadline
            moved to ``extension``, tiers apply from there), or "discuss"
            (hold the grade out of the Moodle upload).

``<course>/gradebook.json`` (course-level, instructor side)
    per student per assignment: raw and final totals, lateness, action;
    plus ``free_late_used`` = the assignment key that consumed it.  A CSV
    twin is rewritten next to it on every export.

Grades in ``grades/<slug>.json`` stay raw part scores; the penalty is an
assignment-level adjustment applied only at export time (worksheet,
gradebook, feedback sheets).
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LATE_FILE = "late.json"
GRADEBOOK_JSON = "gradebook.json"
GRADEBOOK_CSV = "gradebook.csv"

ACTIONS = ("auto", "free", "apply", "waive", "extension", "discuss")

DEFAULT_POLICY = {
    "free_lates": 1,          # free late assignments per student
    "free_within_hours": 72,  # ...usable only within this window
    "grace_minutes": 0,       # automatic grace (0 = every minute counts)
    "tiers": [                # (up to hours late, % of possible deducted)
        [24, 5],
        [72, 10],
    ],                        # beyond the last tier: hold for discussion
}

# Moodle grading-worksheet timestamps: "Friday, September 4, 2026, 10:36 PM"
_WS_FORMATS = ("%A, %B %d, %Y, %I:%M %p", "%A, %d %B %Y, %I:%M %p",
               "%A, %B %d, %Y, %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M")


class LateError(Exception):
    pass


# ---------------------------------------------------------------- times ----

def course_tz(name: str | None):
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise LateError(f"unknown timezone {name!r}")


def parse_worksheet_time(text: str, tz) -> datetime | None:
    """A Moodle worksheet timestamp -> aware datetime, or None if blank or
    unparseable.  Moodle prints wall-clock time in the downloading user's
    display timezone, which is the course timezone here."""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if not s or s == "-":
        return None
    for fmt in _WS_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def parse_due(text: str, tz) -> datetime:
    """``due:`` values: 'YYYY-MM-DD HH:MM', ISO 8601 (with or without an
    offset), or a Moodle-style worksheet timestamp."""
    s = (text or "").strip().strip("\"'")
    if not s:
        raise LateError("empty due date")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=tz)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    dt = parse_worksheet_time(s, tz)
    if dt is None:
        raise LateError(f"cannot parse due date {s!r} (use YYYY-MM-DD HH:MM)")
    return dt


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(timespec="minutes")


def from_iso(s: str | None, tz=None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt


def fmt_when(dt: datetime | None, tz=None) -> str:
    if dt is None:
        return ""
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%a %b %-d, %-I:%M %p")


def fmt_hours(h: float | None) -> str:
    if h is None or h <= 0:
        return ""
    mins = int(round(h * 60))
    d, rem = divmod(mins, 1440)
    hh, mm = divmod(rem, 60)
    out = []
    if d:
        out.append(f"{d} d")
    if hh:
        out.append(f"{hh} h")
    if mm or not out:
        out.append(f"{mm} min")
    return " ".join(out)


# --------------------------------------------------------------- settings --

def read_settings(folder: Path) -> dict:
    """``due``/``timezone`` lines from rubric.yml (a tiny YAML subset)."""
    out: dict = {"due": None, "timezone": None}
    path = Path(folder) / "rubric.yml"
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^(due|timezone)\s*:\s*(.*)$", line)
        if m:
            val = m.group(2).split(" #", 1)[0].strip().strip("\"'")
            out[m.group(1)] = val or None
    return out


def write_setting(folder: Path, key: str, value: str) -> None:
    """Set/replace a top-level ``key: value`` line in rubric.yml, creating
    the file if needed (the parts block is left untouched)."""
    path = Path(folder) / "rubric.yml"
    lines = path.read_text().splitlines() if path.is_file() else []
    pat = re.compile(rf"^\s*{key}\s*:")
    new = f"{key}: {value}"
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = new
            break
    else:
        # before "parts:" if present, so the block stays last
        for i, line in enumerate(lines):
            if re.match(r"^parts\s*:", line):
                lines.insert(i, new)
                break
        else:
            lines.append(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def assignment_key(folder: Path) -> str:
    """ps01/grading -> 'ps01'; anything else -> the folder's own name
    (the same rule the server-side push uses)."""
    folder = Path(folder).resolve()
    return folder.parent.name if folder.name == "grading" else folder.name


def course_dir(folder: Path) -> Path:
    """Where the course gradebook lives: above the assignment folder."""
    folder = Path(folder).resolve()
    return folder.parent.parent if folder.name == "grading" else folder.parent


# -------------------------------------------------------------- decisions --

def load_decisions(folder: Path) -> dict:
    path = Path(folder) / LATE_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise LateError(f"{path} is not valid JSON")
    return data if isinstance(data, dict) else {}


def save_decision(folder: Path, slug: str, action: str,
                  note: str = "", extension: str | None = None,
                  tz=None) -> dict:
    if action not in ACTIONS:
        raise LateError(f"unknown late action {action!r}")
    ext_iso = None
    if action == "extension":
        if not extension:
            raise LateError("an extension needs a new deadline")
        ext_iso = iso(parse_due(extension, tz or course_tz(None)))
    decisions = load_decisions(folder)
    if action == "auto" and not note:
        decisions.pop(slug, None)
    else:
        decisions[slug] = {
            "action": action, "note": (note or "")[:300],
            "extension": ext_iso,
            "decided": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        }
    path = Path(folder) / LATE_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(decisions, indent=2) + "\n")
    tmp.replace(path)
    return decisions.get(slug, {"action": "auto", "note": "",
                                "extension": None})


# -------------------------------------------------------------- gradebook --

class Gradebook:
    """The course-level record: ``<course>/gradebook.json`` (+ .csv)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"policy": dict(DEFAULT_POLICY), "students": {}}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                raise LateError(f"{self.path} is not valid JSON")
            if isinstance(loaded, dict):
                pol = dict(DEFAULT_POLICY)
                pol.update(loaded.get("policy") or {})
                self.data = {"policy": pol,
                             "students": loaded.get("students") or {}}

    @classmethod
    def for_folder(cls, folder: Path) -> "Gradebook":
        return cls(course_dir(folder) / GRADEBOOK_JSON)

    @property
    def policy(self) -> dict:
        return self.data["policy"]

    def student(self, moodle_id: str) -> dict:
        return self.data["students"].setdefault(
            str(moodle_id), {"name": "", "email": "", "assignments": {},
                             "free_late_used": None})

    def free_late_used_on(self, moodle_id: str) -> str | None:
        s = self.data["students"].get(str(moodle_id))
        return s.get("free_late_used") if s else None

    def record(self, moodle_id: str, key: str, entry: dict,
               name: str = "", email: str = "") -> None:
        s = self.student(moodle_id)
        if name:
            s["name"] = name
        if email:
            s["email"] = email
        s["assignments"][key] = entry
        if entry.get("action") == "free":
            s["free_late_used"] = key
        elif s.get("free_late_used") == key:
            s["free_late_used"] = None   # decision changed on re-export

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True)
                       + "\n")
        tmp.replace(self.path)
        self._write_csv()

    def assignment_keys(self) -> list[str]:
        keys = {k for s in self.data["students"].values()
                for k in s.get("assignments", {})}
        return sorted(keys)

    def _write_csv(self) -> None:
        keys = self.assignment_keys()
        rows = []
        for mid, s in sorted(self.data["students"].items(),
                             key=lambda kv: (kv[1].get("name") or "",
                                             kv[0])):
            row = [s.get("name", ""), s.get("email", ""), mid]
            lates = []
            for k in keys:
                a = s["assignments"].get(k)
                if not a:
                    row += ["", ""]
                    continue
                row += [_num(a.get("total")), _num(a.get("out_of"))]
                if a.get("hours_late"):
                    lates.append(
                        f"{k}: {fmt_hours(a['hours_late'])} late "
                        f"({a.get('action', 'auto')})")
            row += [s.get("free_late_used") or "", "; ".join(lates)]
            rows.append(row)
        header = ["student", "email", "moodle_id"]
        for k in keys:
            header += [k, f"{k} out of"]
        header += ["free late used on", "late submissions"]
        with self.path.with_name(GRADEBOOK_CSV).open(
                "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)


def _num(x) -> str:
    if x is None:
        return ""
    return str(int(x)) if float(x) == int(x) else f"{float(x):g}"


# ------------------------------------------------------------ resolution --

def hours_between(later: datetime | None, deadline: datetime | None
                  ) -> float | None:
    if later is None or deadline is None:
        return None
    return (later - deadline).total_seconds() / 3600.0


def tier_for(hours: float, policy: dict) -> tuple[float | None, bool]:
    """(penalty percent, hold) for a submission ``hours`` late under the
    policy's tiers; hold=True means past the last tier."""
    if hours <= policy.get("grace_minutes", 0) / 60.0:
        return 0.0, False
    for upto, pct in policy.get("tiers", DEFAULT_POLICY["tiers"]):
        if hours <= upto:
            return float(pct), False
    return None, True


class LateStatus:
    """Everything the UI and the export need to know about one unit."""

    def __init__(self):
        self.submitted: datetime | None = None
        self.resubmitted: datetime | None = None
        self.deadline: datetime | None = None
        self.hours_late: float | None = None      # vs the effective deadline
        self.hours_late_original: float | None = None  # vs the due date
        self.decision: dict | None = None
        self.action: str = "none"        # effective: none/free/apply/waive/
        self.penalty_pct: float = 0.0    #   extension/discuss
        self.penalty_pts: float = 0.0
        self.hold: bool = False
        self.free_available: bool | None = None  # None = unknown (no book)
        self.tier_pct: float | None = None   # what the policy alone says
        self.tier_hold: bool = False
        self.notes: list[str] = []
        self.label: str = ""

    @property
    def is_late(self) -> bool:
        return bool(self.hours_late_original and self.hours_late_original > 0)

    def to_json(self, tz=None) -> dict:
        return {
            "submitted": iso(self.submitted),
            "submitted_text": fmt_when(self.submitted, tz),
            "resubmitted": iso(self.resubmitted),
            "resubmitted_text": fmt_when(self.resubmitted, tz),
            "deadline": iso(self.deadline),
            "hours_late": (None if self.hours_late_original is None
                           else round(self.hours_late_original, 2)),
            "late_text": fmt_hours(self.hours_late_original),
            "is_late": self.is_late,
            "tier_pct": self.tier_pct,
            "tier_hold": self.tier_hold,
            "decision": self.decision or {"action": "auto", "note": "",
                                          "extension": None},
            "action": self.action,
            "penalty_pct": self.penalty_pct,
            "penalty_pts": self.penalty_pts,
            "hold": self.hold,
            "free_available": self.free_available,
            "label": self.label,
            "notes": self.notes,
        }


def resolve(unit: dict, *, due: datetime | None, decision: dict | None,
            policy: dict, out_of: float, key: str,
            free_used_on: str | None = None, have_book: bool = True,
            tz=None) -> LateStatus:
    """Apply the policy and the instructor's decision to one unit."""
    st = LateStatus()
    st.submitted = from_iso(unit.get("submitted"), tz)
    st.resubmitted = from_iso(unit.get("resubmitted"), tz)
    st.decision = decision or None
    action = (decision or {}).get("action", "auto")
    if action not in ACTIONS:
        action = "auto"
    st.deadline = due
    if due is None or st.submitted is None:
        st.label = ("no due date set" if due is None and st.submitted
                    else "")
        return st

    st.hours_late_original = hours_between(st.submitted, due)
    if action == "extension" and (decision or {}).get("extension"):
        st.deadline = from_iso(decision["extension"], tz) or due
    st.hours_late = hours_between(st.submitted, st.deadline)
    st.tier_pct, st.tier_hold = tier_for(max(0.0, st.hours_late), policy)

    if st.hours_late <= 0 or st.tier_pct == 0.0 and not st.tier_hold:
        if action == "extension":
            st.action = "extension"
            st.label = f"extension to {fmt_when(st.deadline, tz)}: on time"
        elif st.hours_late_original > 0:
            st.action = "waive"   # inside the automatic grace period
            st.label = "within grace period"
        return st

    # late relative to the effective deadline
    if have_book:
        used = free_used_on
        st.free_available = (used is None or used == key) and \
            (policy.get("free_lates", 1) or 0) > 0 and \
            st.hours_late <= policy.get("free_within_hours", 72)
        if used is not None and used != key and key < used:
            st.notes.append(
                f"free late already used on {used}, which comes after "
                f"{key} — reassign it by hand if this should be the free "
                "one")
    if action == "waive":
        st.action = "waive"
        st.label = "penalty waived"
    elif action == "discuss":
        st.action, st.hold = "discuss", True
        st.label = "held for discussion"
    elif action == "free" or (action == "auto" and st.free_available):
        if action == "free" and have_book and st.free_available is False:
            st.notes.append("free late already spent on "
                            f"{free_used_on}; using it here anyway")
        st.action = "free"
        st.label = "free late assignment"
    elif st.tier_hold:
        st.action, st.hold = "discuss", True
        st.label = (f"more than {policy['tiers'][-1][0]} h late — "
                    "held for discussion")
    else:
        st.action = "extension" if action == "extension" else "apply"
        st.penalty_pct = st.tier_pct or 0.0
        st.penalty_pts = round(st.penalty_pct / 100.0 * (out_of or 0), 2)
        st.label = f"{_num(st.penalty_pct)}% penalty"
        if not have_book and action == "auto":
            st.label += " unless a free late is available"
    if action == "auto" and not have_book:
        st.notes.append("free-late status unknown here (no course "
                        "gradebook) — the export decides")
    return st


def apply_penalty(raw_total: float, st: LateStatus) -> float:
    if st.penalty_pts:
        return max(0.0, round(raw_total - st.penalty_pts, 4))
    return raw_total


# ------------------------------------------------------------- app glue ----

class LateContext:
    """Per-grading-folder bundle: settings, decisions, policy, gradebook."""

    def __init__(self, folder: Path, out_of: float, with_book: bool = True):
        self.folder = Path(folder)
        self.out_of = out_of
        settings = read_settings(folder)
        self.tz = course_tz(settings.get("timezone"))
        self.due = (parse_due(settings["due"], self.tz)
                    if settings.get("due") else None)
        self.decisions = load_decisions(folder)
        self.key = assignment_key(folder)
        self.book: Gradebook | None = None
        if with_book:
            try:
                self.book = Gradebook.for_folder(folder)
            except LateError:
                self.book = None
        self.policy = self.book.policy if self.book else dict(DEFAULT_POLICY)

    def status(self, unit: dict) -> LateStatus:
        mid = str(unit.get("moodle_id", ""))
        return resolve(
            unit, due=self.due, decision=self.decisions.get(unit["slug"]),
            policy=self.policy, out_of=self.out_of, key=self.key,
            free_used_on=(self.book.free_late_used_on(mid)
                          if self.book else None),
            have_book=self.book is not None, tz=self.tz)

    def payload(self, unit: dict) -> dict:
        return self.status(unit).to_json(self.tz)

    def summary(self) -> dict:
        return {"due": iso(self.due), "due_text": fmt_when(self.due, self.tz),
                "timezone": str(self.tz), "policy": self.policy,
                "gradebook": str(self.book.path) if self.book else None,
                "key": self.key}

