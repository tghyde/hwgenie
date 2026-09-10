"""The assignment overview: how an assignment went, at a glance.

Instructor-only page (``/overview?folder=<grading folder>``) built from the
grading folder after the graders' work has been pulled: per-part averages
with the feedback pages' score pies, the distribution of totals, the
students who did particularly well or poorly, and what still needs
attention (ungraded parts, held late work, the export).

Totals here are RAW base points (extra credit and late penalties are shown
separately) — the question is how the class did on the mathematics, not
what Moodle will record.
"""

from __future__ import annotations

import html
import math
import statistics
import urllib.parse
from pathlib import Path

from . import late as late_mod
from .feedback import (_assignment_title, _base_out_of, _fmt_score, _pie_svg,
                       _split_totals, _worksheet_people, display_name,
                       find_worksheet)

# highlight rules: "did particularly well" = at least this fraction of the
# base points; "struggled" = below this fraction.  Either list always shows
# at least MIN_HIGHLIGHT students (when that many are fully graded) and at
# most MAX_HIGHLIGHT.
WELL_PCT = 0.9
POORLY_PCT = 0.7
MIN_HIGHLIGHT = 3
MAX_HIGHLIGHT = 8
BINS = 10   # histogram bins over 0–100 %


def _pct(score, out_of) -> float | None:
    if score is None or not out_of:
        return None
    return max(0.0, min(1.0, score / out_of))


def overview_data(app) -> dict:
    """Everything the page shows, as plain data (also handy for tests)."""
    folder = app.folder
    out_of = _base_out_of(app)
    ws = find_worksheet(folder)
    people = _worksheet_people(ws) if ws else {}
    ctx, late_err = app.late_context()
    key = late_mod.assignment_key(folder)
    try:
        book = late_mod.Gradebook.for_folder(folder)
    except late_mod.LateError:
        book = None

    n_parts = len(app.rubric)
    base_idx = [n for n, rp in enumerate(app.rubric, 1) if not rp.ec]
    units: list[dict] = []
    part_scores: dict[int, list[float]] = {n: [] for n in range(1, n_parts + 1)}
    part_comments = {n: 0 for n in range(1, n_parts + 1)}
    part_graders: dict[int, set] = {n: set() for n in range(1, n_parts + 1)}
    for u in app.units:
        slug = u["slug"]
        data = app.store.load(slug)
        scores = []
        n_comments = 0
        for n in range(1, n_parts + 1):
            p = data["parts"][str(n)]
            s = p.get("score")
            scores.append(s)
            if s is not None:
                part_scores[n].append(float(s))
                if p.get("by"):
                    part_graders[n].add(p["by"])
            cs = [c for c in p.get("comments", []) if c.get("text")]
            part_comments[n] += len(cs)
            n_comments += len(cs)
        raw, ec = _split_totals(app, data)
        graded = sum(1 for s in scores if s is not None)
        complete = all(scores[n - 1] is not None for n in base_idx)
        ls = ctx.status(u) if ctx else None
        mid = str(u["moodle_id"])
        person = people.get(mid) or {}
        exported = None
        if book is not None:
            exported = ((book.data["students"].get(mid) or {})
                        .get("assignments", {}).get(key))
        units.append({
            "slug": slug, "moodle_id": mid,
            "name": person.get("name") or display_name(slug),
            "scores": scores, "graded": graded, "n_parts": n_parts,
            "complete": complete, "raw": raw, "ec": ec,
            "pct": _pct(raw, out_of) if complete else None,
            "comments": n_comments,
            "late": ls.to_json(ctx.tz) if ls else None,
            "hold": bool(ls and ls.hold),
            "penalty_pts": (ls.penalty_pts if ls else 0) or 0,
            "provisional": late_mod.apply_penalty(raw, ls) if ls else raw,
            "exported": exported,
        })

    parts = []
    for n, rp in enumerate(app.rubric, 1):
        xs = part_scores[n]
        mean = statistics.fmean(xs) if xs else None
        full = sum(1 for x in xs if rp.max and x >= rp.max - 1e-9)
        zero = sum(1 for x in xs if x <= 1e-9)
        parts.append({
            "n": n, "label": rp.label, "max": rp.max, "ec": bool(rp.ec),
            "scored": len(xs), "mean": mean,
            "mean_pct": _pct(mean, rp.max),
            "median": statistics.median(xs) if xs else None,
            "full": full, "partial": len(xs) - full - zero, "zero": zero,
            "comments": part_comments[n],
            "graders": sorted(part_graders[n]),
        })

    done = [u for u in units if u["complete"]]
    totals = [u["raw"] for u in done]
    hist = [0] * BINS
    for u in done:
        hist[min(BINS - 1, int(u["pct"] * BINS))] += 1
    stats = {
        "n": len(done), "out_of": out_of,
        "mean": statistics.fmean(totals) if totals else None,
        "median": statistics.median(totals) if totals else None,
        "stdev": statistics.pstdev(totals) if len(totals) >= 2 else None,
        "min": min(totals) if totals else None,
        "max": max(totals) if totals else None,
        "perfect": sum(1 for t in totals if t >= out_of - 1e-9),
        "ec_earned": sum(1 for u in done if u["ec"] > 0),
        "hist": hist,
    }

    ranked = sorted(done, key=lambda u: (-u["raw"], -u["ec"], u["name"]))
    well = [u for u in ranked if u["pct"] >= WELL_PCT][:MAX_HIGHLIGHT]
    worst = list(reversed(ranked))
    poorly = [u for u in worst if u["pct"] < POORLY_PCT][:MAX_HIGHLIGHT]
    # always name a few at each end — once the class is big enough that
    # the two ends can't overlap
    if len(ranked) >= 2 * MIN_HIGHLIGHT:
        if len(well) < MIN_HIGHLIGHT:
            well = ranked[:MIN_HIGHLIGHT]
        if len(poorly) < MIN_HIGHLIGHT:
            poorly = worst[:MIN_HIGHLIGHT]

    graded_parts = sum(u["graded"] for u in units)
    total_parts = len(units) * n_parts
    n_exported = sum(1 for u in units if u["exported"])
    last_export = max((u["exported"]["exported"] for u in units
                       if u["exported"]), default=None)
    return {
        "folder": str(folder), "title": _assignment_title(app),
        "key": key, "course": late_mod.course_dir(folder).name,
        "n_units": len(units), "graded_parts": graded_parts,
        "total_parts": total_parts, "n_parts": n_parts,
        "units": units, "parts": parts, "stats": stats,
        "well": well, "poorly": poorly,
        "incomplete": [u for u in units if not u["complete"]],
        "held": [u for u in units if u["hold"]],
        "late": sum(1 for u in units if u["late"] and u["late"].get("is_late")),
        "late_error": late_err,
        "problems": [(f"P{p['num']}", p["boxes"]) for p in
                     app.problems_payload()["problems"] if p["boxes"]],
        "exported": n_exported, "last_export": last_export,
    }


# ------------------------------------------------------------------- html --

def _num(x) -> str:
    if x is None:
        return "—"
    return _fmt_score(round(x, 2)) if isinstance(x, float) else _fmt_score(x)


def _pct_text(p) -> str:
    return "—" if p is None else f"{round(p * 100)}%"


def _histogram_svg(hist: list[int], out_of: float) -> str:
    """One-series histogram of totals (percent bins): thin accent bars with
    rounded tops, a recessive baseline, counts on the non-empty bars and a
    native tooltip per bar."""
    W, H = 640, 200
    left, right, top, bottom = 34, 10, 18, 34
    pw, ph = W - left - right, H - top - bottom
    peak = max(hist) or 1
    slot = pw / BINS
    gap = 3
    bw = slot - gap
    out = [f'<svg class="hist" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="distribution of totals">']
    # recessive grid: quarter lines
    steps = 4
    for v in sorted({round(peak * i / steps) for i in range(1, steps + 1)}):
        if not v:
            continue
        y = top + ph - (v / peak) * ph
        out.append(f'<line class="grid" x1="{left}" x2="{W - right}" '
                   f'y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" '
                   f'text-anchor="end">{v}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{W - right}" '
               f'y1="{top + ph}" y2="{top + ph}"/>')
    r = 4
    for i, c in enumerate(hist):
        x = left + i * slot + gap / 2
        lo, hi = i * 100 // BINS, (i + 1) * 100 // BINS
        plo = round(lo / 100 * out_of, 1)
        phi = round(hi / 100 * out_of, 1)
        tip = (f"{lo}–{hi}% ({_num(plo)}–{_num(phi)} of {_num(out_of)} pts): "
               f"{c} student{'' if c == 1 else 's'}")
        if c:
            h = max(2.0, c / peak * ph)
            y = top + ph - h
            rr = min(r, h)
            path = (f"M{x:.1f} {top + ph} V{y + rr:.1f} "
                    f"Q{x:.1f} {y:.1f} {x + rr:.1f} {y:.1f} "
                    f"H{x + bw - rr:.1f} Q{x + bw:.1f} {y:.1f} "
                    f"{x + bw:.1f} {y + rr:.1f} V{top + ph} Z")
            out.append(f'<path class="bar" d="{path}"><title>{html.escape(tip)}'
                       '</title></path>')
            out.append(f'<text class="cnt" x="{x + bw / 2:.1f}" y="{y - 5:.1f}" '
                       f'text-anchor="middle">{c}</text>')
        else:
            out.append(f'<rect class="hit" x="{x:.1f}" y="{top}" '
                       f'width="{bw:.1f}" height="{ph}"><title>'
                       f'{html.escape(tip)}</title></rect>')
        out.append(f'<text class="tick" x="{x + bw / 2:.1f}" '
                   f'y="{top + ph + 16}" text-anchor="middle">'
                   f'{lo}–{hi}</text>')
    out.append(f'<text class="tick" x="{left + pw / 2:.1f}" y="{H - 4}" '
               'text-anchor="middle">percent of base points</text>')
    out.append("</svg>")
    return "".join(out)


def _student_link(folder: str, u: dict) -> str:
    return ("/grading?folder=" + urllib.parse.quote(folder)
            + "&student=" + urllib.parse.quote(u["slug"]))


def _highlight_list(folder: str, rows: list[dict], out_of: float) -> str:
    if not rows:
        return '<p class="none">nobody yet</p>'
    esc = html.escape
    lis = []
    for u in rows:
        extra = f' <span class="ec">+{_num(u["ec"])} EC</span>' if u["ec"] else ""
        lis.append(
            f'<li><a href="{_student_link(folder, u)}">{esc(u["name"])}</a>'
            f'<span class="sc">{_num(u["raw"])}<span class="oo">/{_num(out_of)}'
            f'</span> · {_pct_text(u["pct"])}{extra}</span></li>')
    return "<ol>" + "".join(lis) + "</ol>"


def _part_cards(d: dict) -> str:
    esc = html.escape
    parts = {p["n"]: p for p in d["parts"]}
    covered = {n for _, boxes in d["problems"] for n in boxes}
    columns = list(d["problems"])
    leftover = [n for n in parts if n not in covered]
    if leftover:
        columns.append(("Parts" if not columns else "Other", leftover))
    cols = []
    for head, boxes in columns:
        cards = []
        for n in boxes:
            p = parts.get(n)
            if not p:
                continue
            tot = p["scored"] or 1
            seg = "".join(
                f'<span class="{cls}" style="width:{100 * v / tot:.1f}%" '
                f'title="{v} {what}"></span>'
                for cls, v, what in (("full", p["full"], "full marks"),
                                     ("part", p["partial"], "partial credit"),
                                     ("zero", p["zero"], "zero")) if v)
            ec = ' <span class="ecchip">EC</span>' if p["ec"] else ""
            by = (f'<span class="by" title="graded by">{esc(", ".join(p["graders"]))}'
                  '</span>') if p["graders"] else ""
            cards.append(
                f'<div class="pcard">'
                f'<div class="ptop"><span class="plabel">{esc(p["label"])}{ec}</span>'
                f'{_pie_svg(p["mean_pct"])}</div>'
                f'<div class="pmean"><b>{_num(p["mean"])}</b>'
                f'<span class="oo">/{_num(p["max"])}</span>'
                f'<span class="ppct">{_pct_text(p["mean_pct"])}</span></div>'
                f'<div class="dist" title="{p["full"]} full · {p["partial"]} '
                f'partial · {p["zero"]} zero">{seg}</div>'
                f'<div class="pmeta">{p["scored"]}/{d["n_units"]} graded · '
                f'median {_num(p["median"])} · {p["comments"]} comment'
                f'{"" if p["comments"] == 1 else "s"}</div>{by}'
                '</div>')
        cols.append(f'<div class="pcol"><div class="phead">{esc(head)}</div>'
                    + "".join(cards) + "</div>")
    return '<div class="partgrid">' + "".join(cols) + "</div>"


def _all_scores_table(d: dict) -> str:
    esc = html.escape
    out_of = d["stats"]["out_of"]
    head = "".join(
        f'<th title="{esc(p["label"])} / {_num(p["max"])}">{esc(p["label"])}'
        f'{" <small>EC</small>" if p["ec"] else ""}</th>' for p in d["parts"])
    rows = sorted(d["units"], key=lambda u: (u["pct"] is None,
                                             -(u["raw"]), u["name"]))
    trs = []
    for u in rows:
        cells = []
        for p, s in zip(d["parts"], u["scores"]):
            if s is None:
                cells.append('<td class="none">·</td>')
                continue
            pc = _pct(s, p["max"])
            cls = ("ok" if pc is None or pc >= 2 / 3 else
                   "mid" if pc >= 1 / 3 else "low")
            cells.append(f'<td class="s {cls}">{_num(s)}</td>')
        flags = []
        if u["late"] and u["late"].get("is_late"):
            flags.append(f'<span class="lt">{esc(u["late"].get("late_text") or "")} '
                         f'late{" · held" if u["hold"] else ""}</span>')
        if not u["complete"]:
            flags.append(f'<span class="lt">{u["graded"]}/{u["n_parts"]} graded</span>')
        trs.append(
            f'<tr><td class="nm"><a href="{_student_link(d["folder"], u)}">'
            f'{esc(u["name"])}</a>{"".join(flags)}</td>' + "".join(cells)
            + f'<td class="tot"><b>{_num(u["raw"])}</b><span class="oo">/'
            f'{_num(out_of)}</span></td>'
            f'<td class="tot">{_num(u["ec"]) if u["ec"] else ""}</td>'
            f'<td class="tot muted">{_pct_text(u["pct"])}</td></tr>')
    return (f'<table class="scores"><thead><tr><th>Student</th>{head}'
            '<th>Total</th><th>EC</th><th>%</th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table>")


def render_overview(app) -> str:
    from .appicon import LAMP_SVG
    from .webstyle import BASE_CSS, nav_header
    d = overview_data(app)
    esc = html.escape
    st = d["stats"]
    out_of = st["out_of"]
    folder_q = urllib.parse.quote(d["folder"])

    notes = []
    if d["graded_parts"] < d["total_parts"]:
        left = d["total_parts"] - d["graded_parts"]
        k = len(d["incomplete"])
        notes.append(f'<span class="warn">{left} part{"" if left == 1 else "s"} '
                     f'still ungraded — {k} student{"" if k == 1 else "s"} '
                     f'{"is" if k == 1 else "are"} not fully graded and '
                     f'{"is" if k == 1 else "are"} left out of the statistics '
                     'below.</span>')
    if d["late_error"]:
        notes.append(f'<span class="warn">Late policy: {esc(d["late_error"])}</span>')
    if d["exported"]:
        when = (d["last_export"] or "")[:10]
        notes.append(f'Recorded in the course gradebook for {d["exported"]} '
                     f'students (last export {esc(when)}).')
    else:
        notes.append('Not exported yet — <b>Export</b> in the grader writes '
                     'the feedback files and records the totals in the '
                     'course gradebook.')
    if d["late"]:
        notes.append(f'{d["late"]} late submission{"" if d["late"] == 1 else "s"}'
                     + (f', {len(d["held"])} held for discussion: '
                        + ", ".join(esc(u["name"]) for u in d["held"])
                        if d["held"] else "") + ".")

    tiles = [
        ("Mean", f'{_num(st["mean"])}<span class="oo">/{_num(out_of)}</span>',
         _pct_text(_pct(st["mean"], out_of))),
        ("Median", _num(st["median"]), _pct_text(_pct(st["median"], out_of))),
        ("Std dev", _num(st["stdev"]), "points"),
        ("Range", f'{_num(st["min"])}–{_num(st["max"])}', "low – high"),
        ("Perfect", str(st["perfect"]), f'of {st["n"]} graded'),
    ]
    if any(p["ec"] for p in d["parts"]):
        tiles.append(("Earned EC", str(st["ec_earned"]), f'of {st["n"]} graded'))
    tile_html = "".join(
        f'<div class="tile"><div class="tv">{v}</div><div class="tl">{esc(l)}'
        f'</div><div class="ts">{esc(s)}</div></div>' for l, v, s in tiles)

    if st["n"]:
        dist = (f'<div class="tiles">{tile_html}</div>'
                + _histogram_svg(st["hist"], out_of))
    else:
        dist = ('<p class="none">No student is fully graded yet — the '
                'distribution appears once base parts have scores.</p>')

    body = (
        f'<p class="lede">{d["n_units"]} submissions · '
        f'{d["graded_parts"]}/{d["total_parts"]} parts graded · '
        f'{d["n_parts"]} parts, {_num(out_of)} base points'
        f'{" + extra credit" if any(p["ec"] for p in d["parts"]) else ""}</p>'
        + (('<p class="notes">' + " ".join(notes) + "</p>") if notes else "")
        + '<h2>Totals</h2>' + dist
        + '<h2>By part</h2><p class="sub">Average score per part — pies use '
        'the same thirds as the students&rsquo; feedback pages; the bar '
        'under each shows how many got full marks, partial credit, or '
        'zero.</p>' + _part_cards(d)
        + '<div class="hl"><div><h2>Did particularly well</h2>'
        + _highlight_list(d["folder"], d["well"], out_of)
        + '</div><div><h2>Struggled</h2>'
        + _highlight_list(d["folder"], d["poorly"], out_of) + "</div></div>"
        + '<details class="all"><summary>All scores</summary>'
        + _all_scores_table(d) + "</details>")
    return (OVERVIEW_PAGE.replace("__NAV__", nav_header("grading"))
            .replace("__LAMP__", LAMP_SVG)
            .replace("__CSS__", BASE_CSS)
            .replace("__TITLE__", esc(d["title"]))
            .replace("__KEY__", esc(f'{d["course"]} / {d["key"]}'))
            .replace("__FOLDER__", folder_q)
            .replace("__BODY__", body))


OVERVIEW_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>hwGenie — Overview</title>
<style>
__CSS__
:root { --ok: #2c6a3f; --mid: #a07d1a; --low: #b3223a; }
@media (prefers-color-scheme: dark) {
  :root { --ok: #98cda5; --mid: #d9bd6a; --low: #e87a90; }
}
html, body { height: auto; min-height: 100%; }
body { overflow: auto; display: block; }
main { max-width: 74rem; margin: 1.5rem auto; padding: 0 1rem 3rem; }
h1 { margin: 0 0 .2rem; }
h2 { font-size: .95rem; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); margin: 1.8rem 0 .6rem; }
.src, .sub, .none, .lede { color: var(--muted); font-size: .88rem; }
.src a { text-decoration: none; margin-right: .8rem; }
.src a:hover { text-decoration: underline; }
.lede { margin: 0 0 .5rem; }
.notes { font-size: .9rem; background: var(--card-bg); padding: .6rem .9rem;
  margin: .4rem 0 0; }
.notes .warn { color: var(--alert); }
.oo { color: var(--muted); font-size: .8em; margin-left: .1em; }
.tiles { display: flex; flex-wrap: wrap; gap: .6rem; margin: 0 0 1rem; }
.tile { background: var(--card-bg); padding: .55rem .9rem; min-width: 8rem; }
.tile .tv { font-size: 1.45rem; font-weight: 600; line-height: 1.2; }
.tile .tl { font-size: .72rem; letter-spacing: .05em; text-transform: uppercase;
  color: var(--muted); margin-top: .15rem; }
.tile .ts { font-size: .78rem; color: var(--muted); }
svg.hist { width: 100%; max-width: 46rem; height: auto; display: block;
  font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; }
svg.hist .grid { stroke: var(--border); stroke-width: 1; }
svg.hist .axis { stroke: var(--muted); stroke-width: 1; }
svg.hist .tick { fill: var(--muted); }
svg.hist .cnt { fill: var(--fg); font-weight: 600; }
svg.hist .bar { fill: var(--accent); }
svg.hist .bar:hover { fill: color-mix(in srgb, var(--accent) 75%, var(--fg)); }
svg.hist .hit { fill: transparent; }
.partgrid { display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-start; }
.pcol { display: flex; flex-direction: column; gap: .5rem; }
.phead { font-size: .78rem; font-weight: 600; color: var(--muted);
  letter-spacing: .05em; text-align: center; }
.pcard { background: var(--card-bg); padding: .5rem .7rem; width: 12.5rem; }
.ptop { display: flex; justify-content: space-between; align-items: center; }
.plabel { font-weight: 600; }
.ecchip { font-size: .68rem; letter-spacing: .05em; padding: 0 .3rem;
  background: var(--accent); color: var(--bg); margin-left: .35rem;
  vertical-align: middle; }
.pie { width: 30px; height: 30px; flex-shrink: 0; }
.pie-ok { fill: var(--ok); } .pie-mid { fill: var(--mid); }
.pie-low { fill: var(--low); } .pie-track { fill: var(--code-bg); }
.pie-none { fill: none; stroke: var(--muted); stroke-width: 1.5;
  stroke-dasharray: 2.5 2.5; }
.pmean { font-size: 1.2rem; margin-top: .1rem; }
.pmean .ppct { color: var(--muted); font-size: .85rem; margin-left: .5rem; }
.dist { display: flex; gap: 2px; height: 7px; margin: .35rem 0 .3rem;
  background: var(--code-bg); }
.dist span { display: block; height: 100%; }
.dist .full { background: var(--ok); } .dist .part { background: var(--mid); }
.dist .zero { background: var(--low); }
.pmeta, .by { font-size: .74rem; color: var(--muted); }
.by { display: block; }
.hl { display: flex; gap: 2rem; flex-wrap: wrap; }
.hl > div { flex: 1 1 16rem; }
.hl ol { margin: 0; padding-left: 1.4rem; }
.hl li { margin: .2rem 0; display: flex; gap: .6rem; align-items: baseline; }
.hl li a { text-decoration: none; }
.hl li a:hover { text-decoration: underline; }
.hl .sc { color: var(--muted); font-size: .85rem; }
.hl .ec { color: var(--accent); }
details.all { margin-top: 1.8rem; }
details.all summary { cursor: pointer; color: var(--accent); font-size: .9rem; }
table.scores { border-collapse: collapse; font-size: .84rem; margin-top: .6rem; }
table.scores th, table.scores td { padding: .28rem .5rem; text-align: right;
  border-bottom: 1px solid var(--border); white-space: nowrap; }
table.scores th { font-size: .72rem; letter-spacing: .04em;
  text-transform: uppercase; color: var(--muted); }
table.scores th:first-child, table.scores td.nm { text-align: left; }
table.scores td.nm a { text-decoration: none; }
table.scores td.nm .lt { display: block; font-size: .68rem; color: var(--alert);
  letter-spacing: .03em; text-transform: uppercase; }
table.scores td.s.mid { color: var(--mid); }
table.scores td.s.low { color: var(--low); font-weight: 600; }
table.scores td.none, .muted { color: var(--muted); }
</style></head><body>
__NAV__
<main>
<h1>__TITLE__</h1>
<p class="src">__KEY__ ·
  <a href="/grading?folder=__FOLDER__">Open in the grader</a>
  <a href="/gradebook?folder=__FOLDER__">Course gradebook</a>
  <a href="/grading?pick=1&view=remote">External grading</a></p>
__BODY__
</main></body></html>"""
