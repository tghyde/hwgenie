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
hashes, anomalies (missing/duplicate files), and where each tex came from
(``original`` vs ``reconstructed`` via ``--tex-fallback``).

Group submissions: Moodle may duplicate one file across every member's
folder; identical content hashes in the manifest make that visible. Group
mapping (group -> members) is handled at grading time, not here.
"""

from __future__ import annotations

import argparse
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

FOLDER_RE = re.compile(r"^(?P<name>.+)_(?P<mid>\d+)_assignsubmission_(?P<kind>[a-z]+)_?$")
COLLAB_RE = re.compile(r"\\newcommand\{\\yourcollaborators\}\{(?P<val>[^{}]*)\}")
TEMPLATE_MARKER = "%Write your solution here"
MANIFEST_NAME = "manifest.json"


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

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CollectResult:
    dest: Path
    units: list[Unit]
    skipped: list[str]
    template_parts: int | None


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


def _collect_unit(folder: Path, slug: str, mid: str, out_dir: Path,
                  tex_fallback: Path | None, template_parts: int | None) -> Unit:
    unit = Unit(slug=slug, moodle_folder=folder.name, moodle_id=mid)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in folder.rglob("*") if p.is_file())
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    texs = [p for p in files if p.suffix.lower() == ".tex"]
    extras = [p for p in files if p not in pdfs and p not in texs]

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


def collect(src: Path, dest: Path, template: Path | None = None,
            tex_fallback: Path | None = None) -> CollectResult:
    template_parts = None
    if template is not None:
        template_parts = template.read_text(errors="replace").count(TEMPLATE_MARKER)
        if template_parts == 0:
            raise CollectError(
                f"{template} has no '{TEMPLATE_MARKER}' markers — is it the "
                "generated [submission] variant?")

    tmp = None
    try:
        if src.is_file() and src.suffix.lower() == ".zip":
            tmp = tempfile.TemporaryDirectory(prefix="hwgenie-collect-")
            with zipfile.ZipFile(src) as z:
                z.extractall(tmp.name)
            root = Path(tmp.name)
        elif src.is_dir():
            root = src
        else:
            raise CollectError(f"{src} is neither a zip file nor a directory")

        units: list[Unit] = []
        skipped: list[str] = []
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
            slug = _slugify(m.group("name"))
            units.append(_collect_unit(folder, slug, m.group("mid"),
                                       sub_root / slug, tex_fallback,
                                       template_parts))
    finally:
        if tmp is not None:
            tmp.cleanup()

    if not units:
        raise CollectError(f"no Moodle submission folders found in {src}")

    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(src),
        "template": None if template is None else {
            "path": str(template), "parts": template_parts},
        "units": [u.to_json() for u in units],
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    return CollectResult(dest=dest, units=units, skipped=skipped,
                         template_parts=template_parts)


def add_parser(sub) -> None:
    p = sub.add_parser(
        "collect",
        help="Normalize a Moodle 'Download all submissions' zip into a "
             "grading folder.",
    )
    p.add_argument("src", help="Moodle zip (or an already-unpacked folder).")
    p.add_argument("--dest", required=True,
                   help="Grading folder to create/update (re-running is safe; "
                        "unit folders are overwritten).")
    p.add_argument("--template",
                   help="The assignment's generated [submission] .tex; enables "
                        "the solution-box count check.")
    p.add_argument("--tex-fallback",
                   help="Folder holding <slug>/submission.tex reconstructions "
                        "to use when a student submitted no tex.")


def run_collect(args: argparse.Namespace) -> int:
    try:
        result = collect(
            Path(args.src), Path(args.dest),
            template=Path(args.template) if args.template else None,
            tex_fallback=Path(args.tex_fallback) if args.tex_fallback else None,
        )
    except (CollectError, OSError, zipfile.BadZipFile) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    width = max(len(u.slug) for u in result.units)
    clean = 0
    for u in result.units:
        pdf = "pdf" if u.pdf else "---"
        tex = {"original": "tex", "reconstructed": "tex*", None: "---"}[u.tex_source]
        parts = "" if u.parts_found is None else f" [{u.parts_found} parts]"
        notes = "; ".join(a for a in u.anomalies)
        print(f"  {u.slug:<{width}}  {pdf} {tex:<4}{parts}"
              + (f"  !! {notes}" if notes else ""))
        if not u.anomalies:
            clean += 1
    for s in result.skipped:
        print(f"  skipped: {s}", file=sys.stderr)
    n = len(result.units)
    recon = sum(1 for u in result.units if u.tex_source == "reconstructed")
    print(f"Collected {n} submissions into {result.dest} "
          f"({clean} clean, {n - clean} flagged"
          + (f", {recon} tex reconstructed" if recon else "") + ")")
    print("  (tex* = reconstructed from PDF, not the student's original)")
    return 0
