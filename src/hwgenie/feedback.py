"""``hwgenie return`` — package graded feedback for the Moodle round trip.

Reads a grading folder (manifest + rubric + groups + grades/) and writes::

    <folder>/return/
      feedback/<slug>/feedback.html   # per student: rendered work, inline
                                      # numbered comment markers, scores
      feedback/<slug>/feedback.pdf    # compiled sheet: scores + comments
                                      # (no anchors), via pdflatex
      moodle-feedback.zip             # one folder per submission, named
                                      # EXACTLY like Moodle's download zip
                                      # (manifest moodle_folder) so
                                      # "Upload multiple feedback files in
                                      # a zip" maps them back
      gradebook.csv                   # one row per student (groups fan
                                      # out to members), per-part scores

Only submissions with at least one graded part or comment are exported
(--all overrides).  AI drafts are never exported — they are grader-private.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import html as html_mod
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .grade import GradeError, split_preamble
from .htmlgen import HtmlConverter
from .htmltemplate import KATEX_VERSION
from .katexmacros import extract_macros

RETURN_DIR = "return"
ZIP_NAME = "moodle-feedback.zip"

SEG_RE = re.compile(r"\\begin\{solution\}HWGRADERBOX(\d+)\\end\{solution\}")


@dataclasses.dataclass
class ReturnResult:
    out_dir: Path
    exported: list[str]
    skipped: list[str]
    pdf_failures: list[str]
    warnings: list[str]
    worksheet: dict | None = None   # {"out", "filled", "locked", "unmatched"}
    extra_credit: dict | None = None   # {"out", "rows", "no_email"}

    @property
    def ok(self) -> bool:
        return bool(self.exported)


# ---------------------------------------------------------------- helpers --

def _tex_escape(s: str) -> str:
    """Escape for literal text (names, labels) in the PDF sheet."""
    return re.sub(r"([&%#_{}])", r"\\\1", s)


def display_name(slug: str) -> str:
    """'Doe-Jane' -> 'Jane Doe'.

    Moodle folder names are Lastname-Firstname; the last hyphen is the
    separator (last names may contain hyphens and spaces of their own,
    e.g. 'Smith-Jones-Ana' -> 'Ana Smith-Jones')."""
    last, sep, first = slug.rpartition("-")
    if not sep or not last or not first:
        return slug
    return f"{first.strip()} {last.strip()}"


def _fmt_score(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v)


def _assignment_title(app) -> str:
    """Pretty title from the template's hw metadata, else the folder name."""
    tmpl = (app.manifest.get("template") or {}).get("path")
    if tmpl and Path(tmpl).is_file():
        text = Path(tmpl).read_text(errors="replace")
        def grab(name):
            # last definition wins: templates \newcommand a placeholder,
            # then \renewcommand the real value
            ms = re.findall(r"\\(?:re)?newcommand\{\\" + name +
                            r"\}\{([^{}]*)\}", text)
            return ms[-1].strip() if ms else ""
        course = grab("hwcourse")
        sem = grab("hwsemester")
        m = re.search(r"\\hwnumber\{(\d+)\}", text)
        num = m.group(1) if m else ""
        m = re.search(r"\\hwtitle\{([^{}]*)\}", text)
        title = m.group(1).strip() if m else ""
        parts = []
        if num:
            parts.append(f"Problem Set {num}" + (f": {title}" if title else ""))
        if course or sem:
            parts.append(" ".join(x for x in (course, sem) if x))
        if parts:
            return " — ".join(parts)
    return app.folder.name


def _unit_members(app, slug: str) -> list[str]:
    return app.groups.get(slug) or [display_name(slug)]


def _gradable(data: dict) -> bool:
    return any(p["score"] is not None or p["comments"]
               for p in data["parts"].values())


def _statement_segments(app) -> tuple[dict, dict]:
    """Per-part problem-statement HTML from the template: for box n, the
    statement text between the previous solution box and box n.  Returns
    ({n: html}, template_katex_macros); empty when there is no template.
    Segments cut through wrapping environments lose only the wrapper —
    the converter keeps the content."""
    from .grade_gui import template_problem_blocks

    tmpl = (app.manifest.get("template") or {}).get("path")
    path = Path(tmpl) if tmpl else None
    if path is not None and not path.is_absolute():
        path = app.folder / path
    if path is None or not path.is_file():
        return {}, {}
    text = path.read_text(errors="replace")
    preamble = split_preamble(text)
    m = re.search(r"\\hwnumber\{(\d+)\}", text)
    section = m.group(1) if m else None
    try:
        macros = extract_macros(preamble)
    except Exception:
        macros = {}
    segs: dict = {}
    for blk in template_problem_blocks(text):
        pos = 0
        for sm in SEG_RE.finditer(blk["tex"]):
            n = int(sm.group(1))
            seg = blk["tex"][pos:sm.start()]
            pos = sm.end()
            if not seg.strip():
                continue
            try:
                conv = HtmlConverter(seg, include_solutions=True,
                                     extra_preamble=preamble,
                                     section=section)
                segs[n] = conv.convert()
            except Exception:
                pass
    return segs, macros


# ----------------------------------------------------------- feedback html --

def _pie_svg(pct: float | None) -> str:
    """Miniature pie showing a part's score fraction (green/yellow/red by
    thirds); a dashed empty circle for ungraded parts."""
    if pct is None:
        return ('<svg class="pie" viewBox="0 0 20 20" aria-hidden="true">'
                '<circle cx="10" cy="10" r="7.5" class="pie-none"/></svg>')
    cls = "ok" if pct >= 2 / 3 else ("mid" if pct >= 1 / 3 else "low")
    if pct >= 0.999:
        return (f'<svg class="pie" viewBox="0 0 20 20" aria-hidden="true">'
                f'<circle cx="10" cy="10" r="8" class="pie-{cls}"/></svg>')
    ang = 2 * math.pi * pct
    x = 10 + 8 * math.sin(ang)
    y = 10 - 8 * math.cos(ang)
    large = 1 if pct > 0.5 else 0
    return (f'<svg class="pie" viewBox="0 0 20 20" aria-hidden="true">'
            f'<circle cx="10" cy="10" r="8" class="pie-track"/>'
            f'<path d="M10 10 L10 2 A8 8 0 {large} 1 {x:.2f} {y:.2f} Z" '
            f'class="pie-{cls}"/></svg>')


def _ordered_comments(tex: str | None, comments: list) -> list:
    """Comments in marker order (anchor END position in the tex, unanchored
    last) — same rule the grader applies, enforced again here so exports
    are right even for parts graded before that rule existed."""
    if not tex or len(comments) < 2:
        return comments

    def key(ic):
        i, c = ic
        at = tex.find(c["anchor"]) if c.get("anchor") else -1
        return (at + len(c["anchor"]) if at != -1 else float("inf"), i)

    return [c for _, c in sorted(enumerate(comments),
                                 key=lambda ic: key(ic))]


def _split_totals(app, data: dict) -> tuple[float, float]:
    """(base, extra-credit) points earned.  EC parts stay out of the base
    total because Moodle caps an assignment grade at the activity max —
    EC is returned to Moodle via a separate gradebook item."""
    base = ec = 0.0
    for n, rp in enumerate(app.rubric, start=1):
        s = data["parts"][str(n)]["score"]
        if s is None:
            continue
        if rp.ec:
            ec += s
        else:
            base += s
    return base, ec


def _base_out_of(app) -> float:
    return sum(rp.max or 0 for rp in app.rubric if not rp.ec)


def _has_ec(app) -> bool:
    return any(rp.ec for rp in app.rubric)


def _part_pct(app, data: dict, n: int) -> float | None:
    p = data["parts"][str(n)]
    rp = app.rubric[n - 1]
    if p["score"] is None or not rp.max:
        return None
    return max(0.0, min(1.0, p["score"] / rp.max))


def _score_overview(app, data: dict) -> str:
    """Per-problem columns of part cards (label + score pie), each a jump
    link to that part's section."""
    probs = [p for p in app.problems_payload()["problems"] if p["boxes"]]
    covered = {n for p in probs for n in p["boxes"]}
    leftover = [n for n in range(1, len(app.rubric) + 1) if n not in covered]
    columns = [(f"P{p['num']}", p["boxes"]) for p in probs]
    if leftover:
        columns.append(("Parts" if not columns else "Other", leftover))
    cols = []
    for head, boxes in columns:
        cells = []
        for n in boxes:
            p = data["parts"][str(n)]
            rp = app.rubric[n - 1]
            pct = _part_pct(app, data, n)
            cells.append(
                f'<a class="scard" href="#part-{n}" '
                f'title="{html_mod.escape(rp.label)}: '
                f'{_fmt_score(p["score"])} / {_fmt_score(rp.max)}">'
                f'<span>{html_mod.escape(rp.label)}</span>'
                f'{_pie_svg(pct)}</a>')
        cols.append(f'<div class="scol"><div class="shead">{head}</div>'
                    + "".join(cells) + "</div>")
    return f'<nav class="scoregrid">{"".join(cols)}</nav>'


def _feedback_html(app, unit: dict, data: dict, title: str,
                   stmts: dict, tmacros: dict) -> str:
    slug = unit["slug"]
    total, ec_total = _split_totals(app, data)
    out_of = _base_out_of(app)
    cdata: dict = {}
    sections = []
    for n, rp in enumerate(app.rubric, start=1):
        p = data["parts"][str(n)]
        pay = app.part_payload(slug, n)
        if pay["html"] is not None:
            content = pay["html"]
        elif pay["tex"] is not None:
            content = (f'<pre class="texsrc">'
                       f'{html_mod.escape(pay["tex"], quote=False)}</pre>')
        else:
            content = ('<p class="note">(No LaTeX on record for this part — '
                       'see your submitted PDF.)</p>')
        comments = _ordered_comments(pay["tex"], p["comments"])
        cdata[str(n)] = {"comments": comments, "macros": pay["macros"]}
        clist = ""
        if comments:
            items = "".join(
                f'<li><span class="cmark">{i + 1}</span>'
                f"<div>{html_mod.escape(c['text'], quote=False)}</div></li>"
                for i, c in enumerate(comments))
            clist = ('<div class="fbcard"><div class="fbhead">Feedback</div>'
                     f'<ul class="clist">{items}</ul></div>')
        score = _fmt_score(p["score"])
        mx = _fmt_score(rp.max)
        stmt = stmts.get(n)
        stoggle = ('<button class="stoggle">▸ Problem</button>'
                   if stmt else "")
        pstmt = (f'<div class="pstmt" hidden>{stmt}</div>' if stmt else "")
        ecb = (' <span class="ecbadge" title="Extra credit — counted '
               'separately from the assignment total">extra credit</span>'
               if rp.ec else "")
        sections.append(f"""
  <section class="part" data-n="{n}" id="part-{n}">
    <div class="part-head">
      <span class="plabel">Problem {html_mod.escape(rp.label)}</span>{ecb}
      {stoggle}
      <span class="sp"></span>
      <span class="pscore">{score} / {mx}</span>
    </div>
    {pstmt}
    <div class="pcontent">{content}</div>
    {clist}
  </section>""")

    recon = ""
    if unit.get("tex_source") == "reconstructed":
        recon = ("""<p class="recon">Your work below was transcribed from the
        PDF you submitted (you did not submit LaTeX source), so minor
        formatting differences are possible. The comments refer to this
        transcription.</p>""")

    cdata["_tmacros"] = tmacros
    jumps = "".join(
        f'<a class="jump" href="#part-{n}">{html_mod.escape(rp.label)}'
        f'{_pie_svg(_part_pct(app, data, n))}</a>'
        for n, rp in enumerate(app.rubric, start=1))
    return FEEDBACK_PAGE \
        .replace("__KATEX__", KATEX_VERSION) \
        .replace("__TITLE__", html_mod.escape(title)) \
        .replace("__NAME__", html_mod.escape(display_name(slug))) \
        .replace("__TOTAL__", f"{_fmt_score(total)} / {_fmt_score(out_of)}"
                 + (f" (+{_fmt_score(ec_total)} extra credit)"
                    if ec_total else "")) \
        .replace("__RECON__", recon) \
        .replace("__OVERVIEW__", _score_overview(app, data)) \
        .replace("__JUMPS__", jumps) \
        .replace("__SECTIONS__", "\n".join(sections)) \
        .replace("__CDATA__", json.dumps(cdata))


# ------------------------------------------------------------ feedback pdf --

def _feedback_tex(app, unit: dict, data: dict, title: str) -> str:
    """Score table + comments (no anchors) on the template's preamble, so
    grader comments may use the course macros ($\\ZZ$ etc.)."""
    tmpl = (app.manifest.get("template") or {}).get("path")
    preamble = None
    if tmpl and Path(tmpl).is_file():
        preamble = split_preamble(Path(tmpl).read_text(errors="replace"))
    if not preamble:
        preamble = "\n".join([
            r"\documentclass[11pt]{article}",
            r"\usepackage{amsmath, amssymb}",
            r"\usepackage{fullpage}",
        ])
    total, ec_total = _split_totals(app, data)
    out_of = _base_out_of(app)
    rows = "\n".join(
        rf"{_tex_escape(rp.label)}{' (extra credit)' if rp.ec else ''} & "
        rf"{_fmt_score(data['parts'][str(n)]['score']).replace('—', '--')} & "
        rf"{_fmt_score(rp.max)} \\"
        for n, rp in enumerate(app.rubric, start=1))
    blocks = []
    for n, rp in enumerate(app.rubric, start=1):
        comments = data["parts"][str(n)]["comments"]
        if not comments:
            continue
        items = "\n".join(rf"\item {c['text']}" for c in comments)
        blocks.append(
            rf"\subsection*{{{_tex_escape(rp.label)}}}"
            "\n\\begin{itemize}\n" + items + "\n\\end{itemize}")
    body = "\n".join([
        r"\begin{document}",
        r"\begin{center}",
        rf"{{\LARGE Feedback}}\\[.3em]",
        rf"{{\large {_tex_escape(title)}}}\\[.3em]",
        rf"{{\large {_tex_escape(display_name(unit['slug']))}}}\\[.5em]",
        rf"{{\large Total: {_fmt_score(total).replace('—', '--')} / "
        rf"{_fmt_score(out_of)}"
        + (rf" \ (+{_fmt_score(ec_total)} extra credit)" if ec_total else "")
        + "}",
        r"\end{center}",
        r"\begin{center}",
        r"\begin{tabular}{lcc}",
        r"Part & Score & Out of \\ \hline",
        rows,
        r"\end{tabular}",
        r"\end{center}",
        "\n".join(blocks),
        r"\end{document}",
    ])
    return preamble + "\n" + body


def _compile_pdf(tex: str, dest: Path) -> bool:
    pdflatex = shutil.which("pdflatex")
    if pdflatex is None:
        return False
    with tempfile.TemporaryDirectory(prefix="hwgenie-return-") as tmp:
        src = Path(tmp) / "feedback.tex"
        src.write_text(tex)
        try:
            proc = subprocess.run(
                [pdflatex, "-interaction=nonstopmode", "-halt-on-error",
                 "feedback.tex"],
                cwd=tmp, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        pdf = Path(tmp) / "feedback.pdf"
        if proc.returncode != 0 or not pdf.is_file():
            return False
        shutil.copy2(pdf, dest)
    return True


# ------------------------------------------------------------------- build --

def build_feedback(folder: Path, out: Path | None = None, pdf: bool = False,
                   include_ungraded: bool = False,
                   worksheet: Path | None = None,
                   progress=lambda s: None, app=None) -> ReturnResult:
    # GradingApp already knows how to render parts with template labels,
    # macros and caching; reuse it rather than duplicating that stack.
    # (An existing instance — e.g. the running hwGrader's — can be passed
    # in to reuse its render cache.)
    if app is None:
        from .grade_gui import GradingApp
        app = GradingApp(folder)
    out_dir = Path(out) if out else app.folder / RETURN_DIR
    title = _assignment_title(app)
    stmts, tmacros = _statement_segments(app)
    exported: list[str] = []
    skipped: list[str] = []
    pdf_failures: list[str] = []
    warnings: list[str] = []

    fb_root = out_dir / "feedback"
    for unit in app.units:
        slug = unit["slug"]
        data = app.store.load(slug)
        if not include_ungraded and not _gradable(data):
            skipped.append(slug)
            continue
        udir = fb_root / slug
        udir.mkdir(parents=True, exist_ok=True)
        (udir / "feedback.html").write_text(
            _feedback_html(app, unit, data, title, stmts, tmacros))
        if pdf:
            if not _compile_pdf(_feedback_tex(app, unit, data, title),
                                udir / "feedback.pdf"):
                pdf_failures.append(slug)
        else:
            # a stale sheet from an earlier run would still get zipped
            (udir / "feedback.pdf").unlink(missing_ok=True)
        exported.append(slug)
        progress(f"  {slug}")

    if not exported:
        raise GradeError("nothing to export — no submission has a graded "
                         "part or comment yet")

    # Moodle re-upload zip: folder names must match the download exactly.
    zpath = out_dir / ZIP_NAME
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for unit in app.units:
            if unit["slug"] not in exported:
                continue
            udir = fb_root / unit["slug"]
            for f in sorted(udir.iterdir()):
                z.write(f, f"{unit['moodle_folder']}/{f.name}")

    has_ec = _has_ec(app)
    labels = [rp.label + (" (EC)" if rp.ec else "") for rp in app.rubric]
    with (out_dir / "gradebook.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["submission", "moodle_id", "student"] + labels +
                   ["total", "out_of"] +
                   (["extra_credit"] if has_ec else []))
        out_of = _base_out_of(app)
        for unit in app.units:
            slug = unit["slug"]
            if slug not in exported:
                continue
            data = app.store.load(slug)
            scores = [data["parts"][str(n)]["score"]
                      for n in range(1, len(app.rubric) + 1)]
            total, ec_total = _split_totals(app, data)
            for member in _unit_members(app, slug):
                w.writerow([slug, unit["moodle_id"], member] +
                           ["" if s is None else _fmt_score(s)
                            for s in scores] +
                           [_fmt_score(total), _fmt_score(out_of)] +
                           ([_fmt_score(ec_total)] if has_ec else []))

    if pdf and shutil.which("pdflatex") is None:
        warnings.append("pdflatex not found — no PDF sheets were produced")

    ws = Path(worksheet) if worksheet else find_worksheet(app.folder)
    ws_info = None
    if ws is not None:
        try:
            ws_info = fill_worksheet(app, exported, out_dir, ws)
            if ws_info["max_mismatch"]:
                got, want = ws_info["max_mismatch"]
                warnings.append(
                    f"worksheet Maximum grade is {got} but the rubric "
                    f"totals {_fmt_score(want)} — check the assignment's "
                    "grade settings")
            if ws_info["locked"]:
                warnings.append(
                    "grades locked in Moodle, not filled: "
                    + ", ".join(ws_info["locked"]))
            if ws_info["unmatched"]:
                warnings.append(
                    "graded but not in the worksheet: "
                    + ", ".join(ws_info["unmatched"]))
        except GradeError as e:
            warnings.append(f"worksheet not filled: {e}")

    ec_info = None
    if has_ec:
        ec_info = _write_extra_credit(app, exported, out_dir, ws)
        if ec_info["no_email"]:
            warnings.append(
                f"extra-credit CSV: {ec_info['no_email']} row(s) have no "
                "email address — drop the Moodle grading worksheet into the "
                "grading folder and re-export (the gradebook import matches "
                "students by email)")
    return ReturnResult(out_dir=out_dir, exported=exported, skipped=skipped,
                        pdf_failures=pdf_failures, warnings=warnings,
                        worksheet=ws_info, extra_credit=ec_info)


# ------------------------------------------------------------ extra credit --

EXTRA_CREDIT_OUT = "extra-credit-upload.csv"


def _worksheet_people(path: Path) -> dict:
    """{moodle_id: {"email", "name"}} from a Moodle grading worksheet —
    the source of the email addresses the gradebook CSV import matches
    students by."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                return {}
            rows = list(reader)
    except OSError:
        return {}

    def col(name):
        return header.index(name) if name in header else None

    ci, ce, cn = col("Identifier"), col("Email address"), col("Full name")
    if ci is None:
        return {}
    people: dict = {}
    for row in rows:
        m = re.search(r"\d+", row[ci]) if len(row) > ci else None
        if not m:
            continue
        people[m.group(0)] = {
            "email": row[ce] if ce is not None and len(row) > ce else "",
            "name": row[cn] if cn is not None and len(row) > cn else "",
        }
    return people


def _write_extra_credit(app, exported: list[str], out_dir: Path,
                        ws: Path | None) -> dict:
    """extra-credit-upload.csv: one row per exported submission with its
    extra-credit points, formatted for Moodle's Grades → Import → CSV
    (matched by email address) into a manual "extra credit" grade item.
    Moodle assignments cannot exceed their maximum grade, so EC travels
    via a separate gradebook item flagged Extra credit under Natural
    aggregation."""
    people = _worksheet_people(ws) if ws is not None else {}
    out_path = Path(out_dir) / EXTRA_CREDIT_OUT
    rows_written = no_email = 0
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Email address", "Full name", "Extra credit"])
        for unit in app.units:
            slug = unit["slug"]
            if slug not in exported:
                continue
            data = app.store.load(slug)
            _, ec_total = _split_totals(app, data)
            person = people.get(unit["moodle_id"]) or {}
            email = person.get("email", "")
            if not email:
                no_email += 1
            w.writerow([email, person.get("name") or display_name(slug),
                        _fmt_score(ec_total)])
            rows_written += 1
    return {"out": str(out_path), "rows": rows_written, "no_email": no_email}


# --------------------------------------------------------- moodle worksheet --

WORKSHEET_OUT = "grading-worksheet-upload.csv"


def find_worksheet(folder: Path) -> Path | None:
    """Moodle grading-worksheet exports are named Grades-<course>-....csv;
    auto-detect one dropped into the grading folder."""
    hits = sorted(Path(folder).glob("Grades-*.csv"))
    return hits[0] if len(hits) == 1 else None


def fill_worksheet(app, exported: list[str], out_dir: Path,
                   path: Path) -> dict:
    """Copy a Moodle offline grading worksheet with the Grade column set
    to each exported submission's total, ready to upload back.

    Moodle matches rows by its own Identifier ("Participant <id>", the
    same id as in the download-zip folder names), so every other column is
    passed through untouched.  Rows marked "Grade can be changed" = No are
    left alone and reported.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise GradeError(f"{path} is empty")
        rows = list(reader)

    def col(name):
        return header.index(name) if name in header else None

    ci, cg = col("Identifier"), col("Grade")
    cmax, clock = col("Maximum grade"), col("Grade can be changed")
    if ci is None or cg is None:
        raise GradeError(
            f"{path.name} has no Identifier/Grade columns — is it a Moodle "
            "grading worksheet export?")

    by_id = {u["moodle_id"]: u for u in app.units}
    out_of = _base_out_of(app)
    filled, locked, seen = 0, [], set()
    max_mismatch = None
    for row in rows:
        m = re.search(r"\d+", row[ci]) if len(row) > ci else None
        unit = by_id.get(m.group(0)) if m else None
        if unit is None or unit["slug"] not in exported:
            continue
        seen.add(unit["slug"])
        if clock is not None and row[clock].strip().lower() == "no":
            locked.append(unit["slug"])
            continue
        data = app.store.load(unit["slug"])
        total, _ = _split_totals(app, data)
        row[cg] = f"{total:.2f}"
        filled += 1
        if cmax is not None and max_mismatch is None:
            try:
                if abs(float(row[cmax]) - out_of) > 1e-9:
                    max_mismatch = (row[cmax], out_of)
            except ValueError:
                pass

    out_path = Path(out_dir) / WORKSHEET_OUT
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    info = {"out": str(out_path), "filled": filled, "locked": locked,
            "unmatched": sorted(set(exported) - seen),
            "max_mismatch": max_mismatch}
    return info


# --------------------------------------------------------------------- cli --

def add_parser(sub) -> None:
    p = sub.add_parser(
        "return",
        help="Package graded feedback: per-student HTML/PDF, Moodle "
             "feedback zip, gradebook CSV.",
    )
    p.add_argument("folder", nargs="?", default=".",
                   help="Grading folder (default: cwd).")
    p.add_argument("--out", type=Path, default=None,
                   help="Output directory (default: <folder>/return).")
    p.add_argument("--pdf", action="store_true",
                   help="Also compile PDF feedback sheets (experimental; "
                        "the HTML is the primary feedback artifact).")
    p.add_argument("--all", action="store_true",
                   help="Also export submissions with nothing graded.")
    p.add_argument("--worksheet", type=Path, default=None,
                   help="Moodle offline grading worksheet CSV to fill with "
                        "totals (default: a single Grades-*.csv found in "
                        "the grading folder).")


def run_return(args: argparse.Namespace) -> int:
    try:
        result = build_feedback(
            Path(args.folder), out=args.out, pdf=args.pdf,
            include_ungraded=args.all, worksheet=args.worksheet,
            progress=print)
    except (GradeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Exported {len(result.exported)} submissions to {result.out_dir}")
    print(f"  Moodle zip: {result.out_dir / ZIP_NAME}")
    print(f"  Gradebook:  {result.out_dir / 'gradebook.csv'}")
    if result.worksheet:
        print(f"  Worksheet:  {result.worksheet['out']} "
              f"({result.worksheet['filled']} grades filled — upload via "
              "the assignment's 'Upload grading worksheet')")
    if result.extra_credit:
        print(f"  Extra credit: {result.extra_credit['out']} "
              f"({result.extra_credit['rows']} rows — import via Gradebook "
              "→ Import → CSV into the extra-credit grade item, matched by "
              "email)")
    if result.skipped:
        print(f"  ({len(result.skipped)} submissions had nothing graded; "
              "use --all to include them)")
    for s in result.pdf_failures:
        print(f"warning: PDF sheet failed for {s} (HTML still written)",
              file=sys.stderr)
    for wmsg in result.warnings:
        print(f"warning: {wmsg}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- the page --

# Marker placement + math rendering mirror the grader app (grade_gui.py):
# markers go in BEFORE KaTeX renders so anchors quoted from the student's
# tex can match raw math source, then degrade to the numbered list below.
FEEDBACK_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedback — __NAME__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@__KATEX__/dist/contrib/auto-render.min.js"></script>
<style>
  :root {
    --bg: #faf9f6; --fg: #20242a; --muted: #5d646f; --accent: #24589f;
    --alert: #b3223a; --border: #dcdad0; --card-bg: #efeee8;
    --sol-accent: #2c6a3f; --code-bg: #f1f0ea; --hover-bg: #e2e8f3;
    --mark-bg: #ffd76e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15171c; --fg: #e7e5e0; --muted: #9aa1ad; --accent: #8db1ea;
      --alert: #e87a90; --border: #33363e; --card-bg: #1f222a;
      --sol-accent: #98cda5; --code-bg: #22252d; --hover-bg: #2b3242;
      --mark-bg: #8a6d1d;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.55 Charter, Georgia, serif;
  }
  main { max-width: 46rem; margin: 0 auto; padding: 1.5rem 1.1rem 4rem; }
  header.doc { text-align: center; margin: 1.5rem 0 2rem;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  header.doc .course { font-size: .85rem; letter-spacing: .1em;
    text-transform: uppercase; color: var(--muted); margin: 0 0 .4rem; }
  header.doc h1 { font-size: 1.4rem; margin: 0; }
  header.doc .total { display: inline-block; margin-top: .8rem;
    padding: .2rem .9rem; font-weight: 600; color: var(--sol-accent);
    background: var(--card-bg); }
  .recon { font-size: .85rem; color: var(--muted);
    background: var(--card-bg); padding: .5rem .8rem; }
  a { color: var(--accent); }
  .sp { flex: 1; }
  :root {
    --ok: #2c6a3f; --mid: #a07d1a; --low: #b3223a;
    --bar-bg: color-mix(in srgb, var(--accent) 10%, var(--bg));
  }
  @media (prefers-color-scheme: dark) {
    :root { --ok: #98cda5; --mid: #d9bd6a; --low: #e87a90; }
  }
  .scoregrid { display: flex; gap: .7rem; justify-content: center;
    align-items: flex-start; flex-wrap: wrap; margin: 1.2rem 0 1.8rem;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .scol { display: flex; flex-direction: column; gap: .3rem; }
  .shead { text-align: center; font-size: .78rem; font-weight: 600;
    color: var(--muted); letter-spacing: .05em; }
  .scard { display: flex; align-items: center; gap: .5rem;
    justify-content: space-between; background: var(--card-bg);
    padding: .25rem .6rem; font-size: .8rem; font-weight: 600;
    color: var(--fg); text-decoration: none; min-width: 5.2rem; }
  .scard:hover { background: var(--hover-bg); }
  .pie { width: 17px; height: 17px; flex-shrink: 0; }
  .pie-ok { fill: var(--ok); }
  .pie-mid { fill: var(--mid); }
  .pie-low { fill: var(--low); }
  .pie-track { fill: var(--code-bg); }
  .pie-none { fill: none; stroke: var(--muted); stroke-width: 1.5;
    stroke-dasharray: 2.5 2.5; }
  #fnav-wrap { position: sticky; top: 0; height: 0; z-index: 20;
    margin: 0 -1.1rem; }
  #fnav { display: none; align-items: center; gap: .3rem; flex-wrap: wrap;
    background: var(--bar-bg); padding: .35rem 1.1rem;
    width: fit-content; max-width: 100%; margin: 0 auto;
    font-family: system-ui, sans-serif;
    box-shadow: 0 2px 8px rgba(0,0,0,.18); }
  #fnav.show { display: flex; }
  #fnav .nm { font-weight: 700; font-size: .9rem; margin-right: .3rem; }
  #fnav .jump { font-size: .74rem; padding: .05rem .4rem;
    background: var(--bg); color: var(--accent); text-decoration: none;
    display: inline-flex; align-items: center; gap: .2rem; }
  #fnav .jump:hover { background: var(--hover-bg); }
  #fnav .pie { width: 12px; height: 12px; }
  #fnav button { font: inherit; font-size: .8rem; border: none;
    cursor: pointer; background: transparent; color: var(--accent);
    padding: .1rem .5rem; }
  #fnav button:hover { background: var(--hover-bg); }
  section.part { background: var(--card-bg); padding: .8rem 1rem;
    margin: 0 0 1rem; scroll-margin-top: 4.6rem; }
  .part-head { display: flex; align-items: baseline; gap: .7rem;
    font-family: system-ui, sans-serif; background: var(--bar-bg);
    margin: -.8rem -1rem .6rem; padding: .45rem 1rem; }
  .plabel { font-weight: 700; font-size: .95rem; }
  .ecbadge { font-size: .66rem; font-weight: 700; letter-spacing: .05em;
    padding: .08rem .32rem; background: var(--accent); color: var(--bg);
    white-space: nowrap; align-self: center; }
  .pscore { font-weight: 700; color: var(--sol-accent); }
  .stoggle { font: 600 .78rem/1.4 system-ui, sans-serif; border: none;
    cursor: pointer; background: transparent; color: var(--accent);
    padding: .05rem .4rem; }
  .stoggle:hover { background: var(--hover-bg); }
  .pstmt { background: var(--bg); padding: .6rem .8rem; margin: 0 0 .8rem;
    border-left: 3px solid var(--accent); font-size: .92rem; }
  .pstmt p { margin: 0 0 .55em; }
  .pstmt .math-display { overflow-x: auto; padding: .15rem 0; }
  .pstmt .thmblock { border-left: 3px solid var(--border);
    padding-left: .7rem; margin: .6em 0; }
  .pstmt .thm-head { font-weight: 700; font-family: system-ui, sans-serif;
    font-size: .82rem; margin: 0 0 .2em; }
  .pstmt details.solution > summary { display: none; }
  .pcontent { overflow-x: auto; }
  .pcontent p { margin: 0 0 .55em; }
  .pcontent .math-display { overflow-x: auto; padding: .15rem 0; }
  .pcontent pre.texsrc, .pcontent pre.code {
    font: .8rem/1.45 ui-monospace, Menlo, monospace; white-space: pre-wrap;
    background: var(--code-bg); padding: .6rem .7rem; margin: 0;
    overflow-x: auto;
  }
  .pcontent .thmblock, .pcontent .proof {
    border-left: 3px solid var(--border); padding-left: .7rem; margin: .6em 0;
  }
  .pcontent .thm-head, .pcontent .proof-label { font-weight: 700;
    font-family: system-ui, sans-serif; font-size: .85rem; margin: 0 0 .2em; }
  .pcontent details.solution > summary { display: none; }
  .note { color: var(--muted); font-style: italic; }
  .task { color: var(--accent); }
  .alert { color: var(--alert); }
  /* the converter glues inline math to trailing punctuation with this
     class; without nowrap a sentence-ending period can wrap alone */
  .nw { white-space: nowrap; }
  sup.cmark, .clist .cmark, .cpop .cmark {
    display: inline-block; cursor: pointer; user-select: none;
    background: var(--mark-bg); color: var(--fg); font: 700 .72rem/1.35
    system-ui, sans-serif; border-radius: 50%; width: 1.35em; height: 1.35em;
    text-align: center; margin: 0 .1em; vertical-align: super;
  }
  .cpop { display: flex; gap: .55rem; align-items: flex-start;
    background: var(--hover-bg);
    background: color-mix(in srgb, var(--sol-accent) 13%, var(--bg));
    border-left: 3px solid var(--sol-accent);
    padding: .45rem .6rem; margin: .3rem 0;
    font: .84rem/1.45 system-ui, sans-serif; }
  .cpop .cmark { flex-shrink: 0; vertical-align: baseline;
    margin-top: .1em; cursor: default; }
  /* the Feedback card: comments live here, labelled with the SAME gold
     markers that sit in the student's work, so the two read as one */
  .fbcard {
    margin-top: .7rem; padding: .55rem .8rem;
    background: color-mix(in srgb, var(--sol-accent) 13%, var(--bg));
    border-left: 3px solid var(--sol-accent);
  }
  .fbhead { font: 700 .72rem/1.4 system-ui, sans-serif;
    letter-spacing: .06em; text-transform: uppercase;
    color: var(--sol-accent); margin-bottom: .35rem; }
  ul.clist { font: .9rem/1.5 system-ui, sans-serif; margin: 0;
    padding: 0; list-style: none; }
  ul.clist li { margin: .35rem 0; display: flex; gap: .55rem;
    align-items: flex-start; }
  ul.clist .cmark { vertical-align: baseline; margin: .15em 0 0;
    cursor: default; flex-shrink: 0; }
  footer { text-align: center; color: var(--muted); font-size: .8rem;
    font-family: system-ui, sans-serif; margin-top: 2.5rem; }
</style>
</head>
<body>
<main>
<div id="fnav-wrap"><div id="fnav">
  __JUMPS__
  <button id="totop" title="Back to top">↑ Top</button>
</div></div>
<header class="doc">
  <p class="course">__TITLE__</p>
  <h1>Feedback — __NAME__</h1>
  <span class="total">Total: __TOTAL__</span>
</header>
__OVERVIEW__
__RECON__
__SECTIONS__
<footer>Numbered markers in your work are comments — click them, or see
the list under each part.</footer>
</main>
<script id="cdata" type="application/json">__CDATA__</script>
<script>
"use strict";
const CDATA = JSON.parse(document.getElementById("cdata").textContent);

function typeset(el, macros) {
  if (!window.renderMathInElement) return;
  try {
    renderMathInElement(el, {
      macros: macros || {}, throwOnError: false, strict: false,
      delimiters: [
        {left: "$$", right: "$$", display: true},
        {left: "\\[", right: "\\]", display: true},
        {left: "$", right: "$", display: false},
        {left: "\\(", right: "\\)", display: false},
      ]});
  } catch (e) {}
}

function mathSpans(t) {
  const spans = [];
  const re = /\\\(|\\\)|\\\[|\\\]|\$/g;
  let m, open = -1, closer = null;
  while ((m = re.exec(t))) {
    if (m[0] === "$" && m.index > 0 && t[m.index - 1] === "\\") continue;
    if (open === -1) {
      closer = {"$": "$", "\\(": "\\)", "\\[": "\\]"}[m[0]];
      if (closer) open = m.index;
    } else if (m[0] === closer) {
      spans.push([open, m.index + m[0].length]);
      open = -1;
    }
  }
  return spans;
}

function placeMarkers(box, comments) {
  const jobs = [];
  comments.forEach((c, i) => {
    const needle = c.anchor && c.anchor.replace(/\s+/g, " ").trim();
    if (needle) jobs.push({i, needle});
  });
  if (!jobs.length) return;
  const blockSel = "p,div,li,td,th,blockquote,pre,h1,h2,h3,figcaption";
  const nodes = [];
  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
    acceptNode: nd => nd.parentElement &&
      nd.parentElement.closest("sup.cmark, .cpop")
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
  let tn;
  while ((tn = walker.nextNode())) nodes.push(tn);
  let norm = "";
  const map = [];
  let lastSpace = true, lastBlock = null;
  nodes.forEach((node, ni) => {
    const blk = node.parentElement.closest(blockSel) || box;
    if (blk !== lastBlock && !lastSpace && norm) {
      norm += " "; map.push({ni, off: 0}); lastSpace = true;
    }
    lastBlock = blk;
    const t = node.textContent;
    for (let off = 0; off < t.length; off++) {
      if (/\s/.test(t[off])) {
        if (lastSpace) continue;
        norm += " "; map.push({ni, off}); lastSpace = true;
      } else {
        norm += t[off]; map.push({ni, off}); lastSpace = false;
      }
    }
  });
  const inserts = [];
  for (const j of jobs) {
    const at = norm.indexOf(j.needle);
    if (at === -1) continue;
    let {ni, off} = map[at + j.needle.length - 1];
    off += 1;
    for (const [s, e] of mathSpans(nodes[ni].textContent))
      if (off > s && off < e) { off = e; break; }
    inserts.push({ni, off, i: j.i});
  }
  inserts.sort((a, b) => b.ni - a.ni || b.off - a.off || b.i - a.i);
  for (const ins of inserts) {
    const node = nodes[ins.ni];
    const mark = document.createElement("sup");
    mark.className = "cmark"; mark.dataset.ci = ins.i;
    mark.textContent = ins.i + 1;
    const disp = node.parentElement.closest(".math-display");
    if (disp) { disp.after(mark); continue; }
    const rest = node.splitText(Math.min(ins.off, node.textContent.length));
    node.parentNode.insertBefore(mark, rest);
  }
}

// sticky nav appears once the header has scrolled away
window.addEventListener("scroll", () => {
  document.getElementById("fnav").classList.toggle(
    "show", window.scrollY > 250);
}, {passive: true});
document.getElementById("totop").addEventListener("click", () =>
  window.scrollTo({top: 0, behavior: "smooth"}));

document.querySelectorAll("section.part").forEach(sec => {
  const d = CDATA[sec.dataset.n] || {comments: [], macros: {}};
  const box = sec.querySelector(".pcontent");
  placeMarkers(box, d.comments);
  const st = sec.querySelector(".stoggle");
  const ps = sec.querySelector(".pstmt");
  if (st && ps) st.addEventListener("click", () => {
    ps.hidden = !ps.hidden;
    st.textContent = (ps.hidden ? "▸" : "▾") + " Problem";
  });
  const paint = () => {
    typeset(box, d.macros);
    if (ps) typeset(ps, CDATA._tmacros || {});
    const cl = sec.querySelector(".clist");
    if (cl) typeset(cl, d.macros);
  };
  if (window.renderMathInElement) paint();
  else window.addEventListener("load", paint);
  sec.querySelectorAll("sup.cmark").forEach(m => {
    m.addEventListener("click", () => {
      const open = m.nextElementSibling;
      if (open && open.classList.contains("cpop")) { open.remove(); return; }
      const c = d.comments[Number(m.dataset.ci)];
      if (!c) return;
      const pop = document.createElement("span");
      pop.className = "cpop";
      const badge = document.createElement("span");
      badge.className = "cmark";
      badge.textContent = Number(m.dataset.ci) + 1;
      const body = document.createElement("div");
      body.textContent = c.text;
      pop.append(badge, body);
      m.after(pop);
      typeset(body, d.macros);
    });
  });
});
</script>
</body>
</html>
"""
