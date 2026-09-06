"""Collect a Moodle "Download all submissions" export into a grading folder.

Moodle's bulk download is a zip with one folder per submitter named
``<Name>_<id>_assignsubmission_file_``. This module normalizes that into::

    <dest>/
      manifest.json                  # everything needed for the return trip
      submissions/<slug>/
        submission.pdf               # renamed from whatever the student called it
        submission.tex
        <extras kept under their original names>

The manifest records each unit's *exact* original Moodle folder name — the
feedback-files return zip must reuse those names verbatim — plus file
hashes, anomalies (missing/duplicate files), where each tex came from
(``original`` vs ``reconstructed`` via ``--tex-fallback``), and when the
student submitted (``submitted``: from the grading worksheet's "Last
modified (submission)" column when one is found next to the zip or in the
grading folder, else the zip's own file times — both to the minute).

Re-running against an existing grading folder is an *update* (late work):
students not seen before are added, unchanged students are left exactly as
they were (including hand edits to the manifest), and a student whose
files changed is flagged as resubmitted — replaced only if nothing of
theirs has been graded yet.  ``--fresh`` forces the old overwrite-all
behaviour.

Group submissions: Moodle may duplicate one file across every member's
folder; identical content hashes in the manifest make that visible. Group
mapping (group -> members) is handled at grading time, not here.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import late as late_mod

FOLDER_RE = re.compile(r"^(?P<name>.+)_(?P<mid>\d+)_assignsubmission_(?P<kind>[a-z]+)_?$")
COLLAB_RE = re.compile(r"\\newcommand\{\\yourcollaborators\}\{(?P<val>[^{}]*)\}")
TEMPLATE_MARKER = "%Write your solution here"
MANIFEST_NAME = "manifest.json"
GRADES_DIR = "grades"


class CollectError(Exception):
    pass


@dataclasses.dataclass
class Unit:
    slug: str
    moodle_folder: str
    moodle_id: str
    pdf: str | None = None            # original filename, or None if missing
    tex: str | None = None
    tex_source: str | None = None     # "original" | "reconstructed" | None
    extras: list[str] = dataclasses.field(default_factory=list)
    anomalies: list[str] = dataclasses.field(default_factory=list)
    sha256: dict[str, str] = dataclasses.field(default_factory=dict)
    parts_found: int | None = None    # \begin{solution} count in the tex
    collaborators: str | None = None  # \yourcollaborators value, if the tex has it
    submitted: str | None = None      # ISO time Moodle last saw an upload
    resubmitted: str | None = None    # a later upload seen by an update
    collected: str | None = None      # when this unit entered the folder
    source_sha256: dict[str, str] = dataclasses.field(default_factory=dict)
    #                                 # {original filename: sha} of what
    #                                 # Moodle handed us — change detection

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Unit":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclasses.dataclass
class CollectResult:
    dest: Path
    units: list[Unit]
    skipped: list[str]
    template_parts: int | None
    added: list[str] = dataclasses.field(default_factory=list)
    resubmitted: list[str] = dataclasses.field(default_factory=list)
    replaced: list[str] = dataclasses.field(default_factory=list)
    unchanged: list[str] = dataclasses.field(default_factory=list)
    update: bool = False
    worksheet: Path | None = None
    late: dict[str, dict] = dataclasses.field(default_factory=dict)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _slugify(name: str) -> str:
    # Moodle names are "Lastname-Firstname" (possibly with spaces/hyphens
    # inside the last name); keep them readable, drop path-hostile chars.
    return re.sub(r"[/\\:\0]", "-", name).strip()


def _count_parts(tex_path: Path) -> int:
    # Count only outside TeX comments — the submission preamble mentions
    # \begin{solution} in its instructions to students.
    n = 0
    for line in tex_path.read_text(errors="replace").splitlines():
        code = re.split(r"(?<!\\)%", line, maxsplit=1)[0]
        n += code.count(r"\begin{solution}")
    return n


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------- timestamps ----

def _zip_times(zpath: Path) -> dict[str, datetime]:
    """{top-level folder: latest file time} from a zip's entries (DOS
    times: wall clock in Moodle's zone, minute resolution)."""
    out: dict[str, datetime] = {}
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir() or "/" not in info.filename:
                continue
            top = info.filename.split("/", 1)[0]
            try:
                dt = datetime(*info.date_time)
            except ValueError:
                continue
            if top not in out or dt > out[top]:
                out[top] = dt
    return out


def _dir_times(root: Path) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        times = [p.stat().st_mtime for p in folder.rglob("*") if p.is_file()]
        if times:
            out[folder.name] = datetime.fromtimestamp(max(times))
    return out


def find_worksheet_near(*dirs: Path) -> Path | None:
    """The single Grades-*.csv in the first directory that has exactly one."""
    for d in dirs:
        if d is None or not Path(d).is_dir():
            continue
        hits = sorted(Path(d).glob("Grades-*.csv"))
        if len(hits) == 1:
            return hits[0]
    return None


def worksheet_times(path: Path, tz) -> dict[str, datetime]:
    """{moodle_id: submitted} from a Moodle grading worksheet."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                return {}
            rows = list(reader)
    except OSError:
        return {}
    ci = header.index("Identifier") if "Identifier" in header else None
    ct = next((i for i, h in enumerate(header)
               if h.strip().lower().startswith("last modified (submission")),
              None)
    if ci is None or ct is None:
        return {}
    out: dict[str, datetime] = {}
    for row in rows:
        if len(row) <= max(ci, ct):
            continue
        m = re.search(r"\d+", row[ci])
        dt = late_mod.parse_worksheet_time(row[ct], tz)
        if m and dt is not None:
            out[m.group(0)] = dt
    return out


# ------------------------------------------------------------- one unit ----

def _source_hashes(folder: Path) -> dict[str, str]:
    return {p.name: _sha256(p)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _collect_unit(folder: Path, slug: str, mid: str, out_dir: Path,
                  tex_fallback: Path | None, template_parts: int | None
                  ) -> Unit:
    unit = Unit(slug=slug, moodle_folder=folder.name, moodle_id=mid,
                collected=_now())
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in folder.rglob("*") if p.is_file())
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    texs = [p for p in files if p.suffix.lower() == ".tex"]
    extras = [p for p in files if p not in pdfs and p not in texs]
    unit.source_sha256 = _source_hashes(folder)

    def _copy(src: Path, dest_name: str, key: str) -> None:
        dest = out_dir / dest_name
        shutil.copy2(src, dest)
        unit.sha256[key] = _sha256(dest)

    if len(pdfs) == 1:
        _copy(pdfs[0], "submission.pdf", "pdf")
        unit.pdf = pdfs[0].name
    elif not pdfs:
        unit.anomalies.append("no pdf submitted")
    else:
        unit.anomalies.append(f"multiple pdf files ({len(pdfs)}); none normalized")
        extras = pdfs + extras

    if len(texs) == 1:
        _copy(texs[0], "submission.tex", "tex")
        unit.tex, unit.tex_source = texs[0].name, "original"
    elif not texs:
        fallback = (tex_fallback / slug / "submission.tex") if tex_fallback else None
        if fallback and fallback.is_file():
            _copy(fallback, "submission.tex", "tex")
            unit.tex, unit.tex_source = str(fallback), "reconstructed"
        else:
            unit.anomalies.append("no tex submitted")
    else:
        unit.anomalies.append(f"multiple tex files ({len(texs)}); none normalized")
        extras = extras + texs

    for p in extras:
        _copy(p, p.name, f"extra:{p.name}")
        unit.extras.append(p.name)
    if unit.extras:
        unit.anomalies.append(f"extra files: {', '.join(unit.extras)}")

    if unit.tex:
        m = COLLAB_RE.search((out_dir / "submission.tex").read_text(errors="replace"))
        if m:
            unit.collaborators = m.group("val").strip()
        unit.parts_found = _count_parts(out_dir / "submission.tex")
        if template_parts is not None and unit.parts_found != template_parts:
            unit.anomalies.append(
                f"tex has {unit.parts_found} solution boxes; template has "
                f"{template_parts}")
    return unit


def _has_grades(dest: Path, slug: str) -> bool:
    p = dest / GRADES_DIR / f"{slug}.json"
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return True   # unreadable: assume it matters
    return any(part.get("score") is not None or part.get("comments")
               for part in (data.get("parts") or {}).values()
               if isinstance(part, dict))


def _unchanged(old: dict, new_hashes: dict, when: datetime | None,
               created: datetime | None) -> bool:
    """Did Moodle hand us the same files as last time?  Exact when the old
    manifest recorded source hashes; for older manifests only Moodle's
    clock can tell (a re-upload after our first collect), and when even
    that is unknown the existing unit — possibly hand-fixed — is kept."""
    old_src = old.get("source_sha256")
    if old_src:
        return old_src == new_hashes
    if when is not None and created is not None:
        return when <= created
    return True


# ------------------------------------------------------------- collect -----

def collect(src: Path, dest: Path, template: Path | None = None,
            tex_fallback: Path | None = None,
            worksheet: Path | None = None, due: str | None = None,
            fresh: bool = False, timezone_name: str | None = None
            ) -> CollectResult:
    template_parts = None
    if template is not None:
        template_parts = template.read_text(errors="replace").count(TEMPLATE_MARKER)
        if template_parts == 0:
            raise CollectError(
                f"{template} has no '{TEMPLATE_MARKER}' markers — is it the "
                "generated [submission] variant?")

    old_manifest: dict | None = None
    if not fresh and (dest / MANIFEST_NAME).is_file():
        try:
            old_manifest = json.loads((dest / MANIFEST_NAME).read_text())
        except json.JSONDecodeError:
            raise CollectError(f"{dest / MANIFEST_NAME} is not valid JSON "
                               "(use --fresh to start over)")
    update = old_manifest is not None
    old_by_id = {str(u.get("moodle_id")): u
                 for u in (old_manifest or {}).get("units", [])}
    old_created = late_mod.from_iso((old_manifest or {}).get("created"))

    if due:
        try:
            late_mod.parse_due(due, late_mod.course_tz(timezone_name))
        except late_mod.LateError as e:
            raise CollectError(str(e))
    settings = late_mod.read_settings(dest)
    tz_name = timezone_name or settings.get("timezone")
    try:
        tz = late_mod.course_tz(tz_name)
    except late_mod.LateError as e:
        raise CollectError(str(e))

    # Manifests from before v0.41 recorded no submission times, but the
    # worksheet copy already in the grading folder has the ORIGINAL ones —
    # read it before a fresh download replaces it, so a student who
    # re-uploads one file later keeps their on-time first submission.
    if update:
        old_ws = find_worksheet_near(dest)
        for mid, when in (worksheet_times(old_ws, tz) if old_ws else {}).items():
            old = old_by_id.get(mid)
            if old is not None and not old.get("submitted"):
                old["submitted"] = late_mod.iso(when)

    tmp = None
    try:
        if src.is_file() and src.suffix.lower() == ".zip":
            times = _zip_times(src)
            tmp = tempfile.TemporaryDirectory(prefix="hwgenie-collect-")
            with zipfile.ZipFile(src) as z:
                z.extractall(tmp.name)
            root = Path(tmp.name)
            ws = worksheet or find_worksheet_near(src.parent, dest)
        elif src.is_dir():
            times = _dir_times(src)
            root = src
            ws = worksheet or find_worksheet_near(src, src.parent, dest)
        else:
            raise CollectError(f"{src} is neither a zip file nor a directory")
        ws_times = worksheet_times(ws, tz) if ws else {}

        units: list[dict] = []
        skipped: list[str] = []
        added: list[str] = []
        resub: list[str] = []
        replaced: list[str] = []
        unchanged: list[str] = []
        seen_ids: set[str] = set()
        sub_root = dest / "submissions"
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            m = FOLDER_RE.match(folder.name)
            if not m:
                skipped.append(f"{folder.name} (not a Moodle submission folder)")
                continue
            if m.group("kind") != "file":
                skipped.append(f"{folder.name} (submission type "
                               f"'{m.group('kind')}' not collected)")
                continue
            mid = m.group("mid")
            seen_ids.add(mid)
            when = ws_times.get(mid)
            if when is None and folder.name in times:
                when = times[folder.name].replace(tzinfo=tz)
            when_iso = late_mod.iso(when)

            old = old_by_id.get(mid)
            if old is not None:
                slug = old["slug"]
                new_hashes = _source_hashes(folder)
                if _unchanged(old, new_hashes, when, old_created):
                    if not old.get("submitted") and when_iso:
                        old["submitted"] = when_iso
                    old.setdefault("source_sha256", new_hashes)
                    units.append(old)
                    unchanged.append(slug)
                    continue
                if _has_grades(dest, slug):
                    # graded already: keep what the graders saw, flag it
                    if old.get("resubmitted") != when_iso:
                        old["resubmitted"] = when_iso
                        note = ("resubmitted "
                                + (late_mod.fmt_when(when, tz) or "later")
                                + " after grading began — files NOT "
                                "replaced (copy them by hand from the zip "
                                "if the new version should be graded)")
                        old["anomalies"] = [a for a in old.get("anomalies", [])
                                            if not a.startswith("resubmitted ")]
                        old["anomalies"].append(note)
                    units.append(old)
                    resub.append(slug)
                    continue
                unit = _collect_unit(folder, slug, mid, sub_root / slug,
                                     tex_fallback, template_parts)
                unit.submitted = old.get("submitted") or when_iso
                # a legacy unit without a recorded time still counts as
                # re-uploaded: that is the only way we got here
                unit.resubmitted = (when_iso if when_iso and
                                    when_iso != old.get("submitted") else None)
                unit.collected = old.get("collected") or unit.collected
                if unit.resubmitted:
                    unit.anomalies.append(
                        "resubmitted " + (late_mod.fmt_when(when, tz)
                                          or "later") + "; files replaced")
                units.append(unit.to_json())
                replaced.append(slug)
                continue

            slug = _slugify(m.group("name"))
            unit = _collect_unit(folder, slug, mid, sub_root / slug,
                                 tex_fallback, template_parts)
            unit.submitted = when_iso
            if update:
                unit.anomalies.append("added by a later collect")
            units.append(unit.to_json())
            added.append(slug)

        # students in the old manifest but not in this download stay
        for mid, old in old_by_id.items():
            if mid not in seen_ids:
                units.append(old)
                unchanged.append(old["slug"])
    finally:
        if tmp is not None:
            tmp.cleanup()

    if not units:
        raise CollectError(f"no Moodle submission folders found in {src}")
    units.sort(key=lambda u: u["slug"].lower())

    dest.mkdir(parents=True, exist_ok=True)
    if due:
        late_mod.write_setting(dest, "due", due)
    if timezone_name:
        late_mod.write_setting(dest, "timezone", timezone_name)
    if ws is not None and ws.parent.resolve() != dest.resolve():
        # the worksheet also drives the return trip; keep the freshest
        # copy next to the manifest (Moodle reuses the filename, so a
        # re-download must replace the stale one)
        target = dest / ws.name
        if not target.exists() or _sha256(target) != _sha256(ws):
            shutil.copy2(ws, target)
    manifest = {
        "created": (old_manifest or {}).get("created") or _now(),
        "updated": _now() if update else None,
        "source": str(src),
        # absolute: the push bundlers and the GUI resolve it from anywhere
        "template": (None if template is None else
                     {"path": str(Path(template).expanduser().resolve()),
                      "parts": template_parts}),
        "units": units,
    }
    if template is None and old_manifest and old_manifest.get("template"):
        manifest["template"] = old_manifest["template"]
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")

    result = CollectResult(dest=dest, units=[Unit.from_dict(u) for u in units],
                           skipped=skipped,
                           template_parts=template_parts, added=added,
                           resubmitted=resub, replaced=replaced,
                           unchanged=unchanged, update=update, worksheet=ws)
    try:
        ctx = late_mod.LateContext(dest, out_of=0.0, with_book=True)
        if ctx.due is not None:
            result.late = {u["slug"]: ctx.payload(u) for u in units}
    except late_mod.LateError:
        pass
    return result


# ------------------------------------------------------- assignment layout --

def locate(zip_path: Path | None = None, folder: Path | None = None) -> dict:
    """Resolve the pieces of a collect from the assignment-folder layout
    the how-to describes (``<assignment>/moodle-raw/*.zip`` + worksheet,
    ``<assignment>/build/*submission*.tex``, ``<assignment>/grading/``)::

        {"zip", "dest", "template", "update"}

    Given a zip: dest is ``<assignment>/grading`` when the zip sits in a
    ``moodle-raw`` folder, else ``<stem>-grading`` beside the zip.  Given a
    grading folder (a re-collect): the newest zip in ``../moodle-raw``,
    the folder's parent, or the folder itself.  The template comes from
    the existing manifest when it still exists, else from ``../build``.
    """
    if zip_path is None and folder is None:
        raise CollectError("nothing to collect: give a zip or a folder")
    if folder is not None:
        dest = Path(folder).expanduser().resolve()
        assignment = dest.parent      # ps01/grading -> ps01; x-grading -> x's dir
    else:
        zp = Path(zip_path).expanduser().resolve()
        if not zp.is_file():
            raise CollectError(f"{zp} is not a file")
        if zp.parent.name == "moodle-raw":
            assignment = zp.parent.parent
            dest = assignment / "grading"
        else:
            assignment = zp.parent
            dest = zp.with_name(zp.stem + "-grading")
    if zip_path is None:
        cands: list[Path] = []
        for d in (assignment / "moodle-raw", assignment, dest):
            if d.is_dir():
                cands += [z for z in d.glob("*.zip") if z.is_file()]
        if not cands:
            raise CollectError(
                f"no Moodle zip found in {assignment / 'moodle-raw'} (or "
                f"{assignment}) — download it from Moodle first")
        zp = max(cands, key=lambda z: z.stat().st_mtime)
    template: Path | None = None
    mf = dest / MANIFEST_NAME
    if mf.is_file():
        try:
            t = (json.loads(mf.read_text()).get("template") or {}).get("path")
        except json.JSONDecodeError:
            t = None
        if t:
            tp = Path(t)
            if not tp.is_absolute():
                tp = dest / tp
            if tp.is_file():
                template = tp
    if template is None and (assignment / "build").is_dir():
        hits = sorted(assignment.glob("build/*submission*.tex"),
                      key=lambda x: x.stat().st_mtime)
        if hits:
            template = hits[-1]
    return {"zip": zp, "dest": dest, "template": template,
            "update": mf.is_file()}


def add_parser(sub) -> None:
    p = sub.add_parser(
        "collect",
        help="Normalize a Moodle 'Download all submissions' zip into a "
             "grading folder (re-run later to add late work).",
    )
    p.add_argument("src", help="Moodle zip (or an already-unpacked folder).")
    p.add_argument("--dest", required=True,
                   help="Grading folder to create/update.  Re-running adds "
                        "new students and leaves graded ones alone.")
    p.add_argument("--template",
                   help="The assignment's generated [submission] .tex; enables "
                        "the solution-box count check.")
    p.add_argument("--tex-fallback",
                   help="Folder holding <slug>/submission.tex reconstructions "
                        "to use when a student submitted no tex.")
    p.add_argument("--worksheet",
                   help="Moodle grading worksheet CSV (source of submission "
                        "times).  Default: a single Grades-*.csv next to the "
                        "zip or in the grading folder.")
    p.add_argument("--due",
                   help="Deadline, e.g. '2026-09-04 23:59' (course timezone); "
                        "written to rubric.yml so lateness can be computed.")
    p.add_argument("--timezone",
                   help="IANA zone for the due date and Moodle's timestamps "
                        "(default: this machine's zone); written to rubric.yml.")
    p.add_argument("--fresh", action="store_true",
                   help="Ignore an existing manifest and rebuild every unit "
                        "(the pre-update behaviour).")


def run_collect(args: argparse.Namespace) -> int:
    try:
        result = collect(
            Path(args.src), Path(args.dest),
            template=Path(args.template) if args.template else None,
            tex_fallback=Path(args.tex_fallback) if args.tex_fallback else None,
            worksheet=Path(args.worksheet) if args.worksheet else None,
            due=args.due, fresh=args.fresh, timezone_name=args.timezone,
        )
    except (CollectError, OSError, zipfile.BadZipFile) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    width = max(len(u.slug) for u in result.units)
    clean = 0
    for u in result.units:
        pdf = "pdf" if u.pdf else "---"
        tex = {"original": "tex", "reconstructed": "tex*", None: "---"}[
            u.tex_source]
        parts = "" if u.parts_found is None else f" [{u.parts_found} parts]"
        tag = ""
        if result.update:
            if u.slug in result.added:
                tag = " +new"
            elif u.slug in result.resubmitted:
                tag = " ~resubmitted"
            elif u.slug in result.replaced:
                tag = " ~replaced"
        lt = result.late.get(u.slug)
        late = (f"  LATE {lt['late_text']} ({lt['label']})"
                if lt and lt.get("is_late") else "")
        notes = "; ".join(a for a in u.anomalies)
        print(f"  {u.slug:<{width}}  {pdf} {tex:<4}{parts}{tag}{late}"
              + (f"  !! {notes}" if notes else ""))
        if not u.anomalies:
            clean += 1
    for s in result.skipped:
        print(f"  skipped: {s}", file=sys.stderr)
    n = len(result.units)
    recon = sum(1 for u in result.units if u.tex_source == "reconstructed")
    verb = "Updated" if result.update else "Collected"
    print(f"{verb} {n} submissions in {result.dest} "
          f"({clean} clean, {n - clean} flagged"
          + (f", {recon} tex reconstructed" if recon else "") + ")")
    if result.update:
        print(f"  {len(result.added)} added, {len(result.replaced)} replaced, "
              f"{len(result.resubmitted)} resubmitted after grading (kept), "
              f"{len(result.unchanged)} unchanged")
    if result.worksheet:
        print(f"  submission times from {result.worksheet.name}")
    else:
        print("  submission times from the zip's file dates (no Grades-*.csv "
              "worksheet found)")
    if not result.late:
        print("  no due date — set one with --due 'YYYY-MM-DD HH:MM' (or a "
              "due: line in rubric.yml) to flag late work")
    print("  (tex* = reconstructed from PDF, not the student's original)")
    return 0
