"""Late-work policy: timestamps, tiers, decisions, incremental collect,
export penalties, the course gradebook, and the grader API."""

import csv
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from test_collect import STUDENT_TEX, TEMPLATE, make_moodle_dir
from test_grade import _start_server, make_grading_folder

from hwgenie import late
from hwgenie.collect import collect
from hwgenie.feedback import build_feedback
from hwgenie.grade import GradeStore, load_rubric

NY = ZoneInfo("America/New_York")
DUE = datetime(2026, 9, 4, 23, 59, tzinfo=NY)
POLICY = dict(late.DEFAULT_POLICY)


# ----------------------------------------------------------- parsing ------

def test_parse_worksheet_time():
    dt = late.parse_worksheet_time("Friday, September 4, 2026, 10:36 PM", NY)
    assert dt == datetime(2026, 9, 4, 22, 36, tzinfo=NY)
    assert late.parse_worksheet_time("-", NY) is None
    assert late.parse_worksheet_time("", NY) is None
    assert late.parse_worksheet_time("garbage", NY) is None


def test_parse_due_formats():
    assert late.parse_due("2026-09-04 23:59", NY) == DUE
    assert late.parse_due("2026-09-04T23:59", NY) == DUE
    assert late.parse_due("Friday, September 4, 2026, 11:59 PM", NY) == DUE
    with pytest.raises(late.LateError):
        late.parse_due("next friday", NY)


def test_fmt_hours():
    assert late.fmt_hours(0) == ""
    assert late.fmt_hours(0.05) == "3 min"
    assert late.fmt_hours(2.5) == "2 h 30 min"
    assert late.fmt_hours(26) == "1 d 2 h"


def test_rubric_settings_roundtrip(tmp_path):
    (tmp_path / "rubric.yml").write_text("parts:\n- 1.1: 4\n")
    late.write_setting(tmp_path, "due", "2026-09-04 23:59")
    late.write_setting(tmp_path, "timezone", "America/New_York")
    text = (tmp_path / "rubric.yml").read_text()
    assert text.index("due:") < text.index("parts:")
    assert late.read_settings(tmp_path) == {
        "due": "2026-09-04 23:59", "timezone": "America/New_York"}
    late.write_setting(tmp_path, "due", "2026-09-05 23:59")   # replace
    assert late.read_settings(tmp_path)["due"] == "2026-09-05 23:59"
    assert text.count("parts:") == 1
    # the rubric parser still reads the parts block
    assert [rp.max for rp in load_rubric(tmp_path, 1)] == [4]


# --------------------------------------------------------- resolution -----

def _unit(hours_after_due, mid="111"):
    when = DUE.timestamp() + hours_after_due * 3600
    return {"slug": "Doe-Jane", "moodle_id": mid,
            "submitted": late.iso(datetime.fromtimestamp(when, NY))}


def _resolve(hours, decision=None, used=None, have_book=True, key="ps02",
             policy=POLICY):
    return late.resolve(_unit(hours), due=DUE, decision=decision,
                        policy=policy, out_of=40, key=key,
                        free_used_on=used, have_book=have_book, tz=NY)


def test_on_time():
    st = _resolve(-0.5)
    assert not st.is_late and st.action == "none" and st.penalty_pts == 0


def test_first_late_is_free():
    st = _resolve(2)
    assert st.is_late and st.action == "free" and st.penalty_pts == 0
    assert st.free_available is True


def test_free_late_reused_on_same_assignment_is_idempotent():
    st = _resolve(2, used="ps02", key="ps02")
    assert st.action == "free"


def test_tiers_after_free_late_spent():
    assert _resolve(2, used="ps01").penalty_pct == 5
    assert _resolve(2, used="ps01").penalty_pts == 2      # 5% of 40
    assert _resolve(24, used="ps01").penalty_pct == 5     # boundary inclusive
    assert _resolve(24.1, used="ps01").penalty_pct == 10   # (minute clock)
    assert _resolve(72, used="ps01").penalty_pts == 4
    st = _resolve(72.5, used="ps01")
    assert st.hold and st.action == "discuss" and st.penalty_pts == 0


def test_free_late_not_for_very_late_work():
    st = _resolve(80)          # first late, but past the 72 h window
    assert st.hold and st.action == "discuss"


def test_free_late_used_later_assignment_warns():
    st = _resolve(2, used="ps05", key="ps02")
    assert st.action == "apply" and st.penalty_pct == 5
    assert any("ps05" in n for n in st.notes)


def test_decisions_override():
    assert _resolve(2, {"action": "waive"}, used="ps01").penalty_pts == 0
    assert _resolve(2, {"action": "apply"}).penalty_pct == 5   # free skipped
    assert _resolve(2, {"action": "discuss"}).hold
    st = _resolve(2, {"action": "free"}, used="ps01")   # spend it anyway
    assert st.action == "free" and st.notes


def test_extension_moves_the_deadline():
    ext = {"action": "extension",
           "extension": late.iso(datetime(2026, 9, 6, 23, 59, tzinfo=NY))}
    st = _resolve(30, ext, used="ps01")
    assert st.action == "extension" and st.penalty_pts == 0
    assert st.is_late                      # vs the original due date
    assert st.to_json()["late_text"] == "1 d 6 h"
    st2 = _resolve(50, ext, used="ps01")   # 2 h past the extension
    assert st2.penalty_pct == 5 and st2.action == "extension"


def test_grace_minutes_policy():
    pol = dict(POLICY, grace_minutes=10)
    st = _resolve(5 / 60, policy=pol, used="ps01")
    assert st.is_late and st.action == "waive" and st.penalty_pts == 0
    assert _resolve(15 / 60, policy=pol, used="ps01").penalty_pct == 5


def test_no_gradebook_leaves_free_late_open():
    st = _resolve(2, have_book=False)
    assert st.action == "apply" and st.penalty_pct == 5
    assert st.free_available is None
    assert "unless a free late" in st.label


def test_no_due_or_no_timestamp():
    st = late.resolve(_unit(2), due=None, decision=None, policy=POLICY,
                      out_of=40, key="ps01")
    assert not st.is_late and st.label == "no due date set"
    st = late.resolve({"slug": "x", "moodle_id": "1"}, due=DUE,
                      decision=None, policy=POLICY, out_of=40, key="ps01")
    assert not st.is_late and st.to_json()["submitted"] is None


def test_save_decision_file(tmp_path):
    d = late.save_decision(tmp_path, "Doe-Jane", "waive", note="2 min")
    assert d["action"] == "waive"
    assert late.load_decisions(tmp_path)["Doe-Jane"]["note"] == "2 min"
    with pytest.raises(late.LateError):
        late.save_decision(tmp_path, "Doe-Jane", "extension")   # no date
    late.save_decision(tmp_path, "Doe-Jane", "extension",
                       extension="2026-09-06 23:59", tz=NY)
    assert late.load_decisions(tmp_path)["Doe-Jane"]["extension"] \
        .startswith("2026-09-06T23:59")
    late.save_decision(tmp_path, "Doe-Jane", "auto")      # back to policy
    assert "Doe-Jane" not in late.load_decisions(tmp_path)
    with pytest.raises(late.LateError):
        late.save_decision(tmp_path, "Doe-Jane", "banana")


# ---------------------------------------------------------- gradebook -----

def test_gradebook_tracks_free_late(tmp_path):
    book = late.Gradebook(tmp_path / "gradebook.json")
    book.record("111", "ps01", {"total": 38, "out_of": 40, "hours_late": 2,
                                "action": "free"}, name="Jane Doe",
                email="jd@x.edu")
    book.record("111", "ps02", {"total": 30, "out_of": 40, "hours_late": 0,
                                "action": "none"})
    book.save()
    again = late.Gradebook(tmp_path / "gradebook.json")
    assert again.free_late_used_on("111") == "ps01"
    assert again.free_late_used_on("222") is None
    assert again.assignment_keys() == ["ps01", "ps02"]
    rows = list(csv.reader((tmp_path / "gradebook.csv").open()))
    assert rows[0] == ["student", "email", "moodle_id", "ps01", "ps01 out of",
                       "ps02", "ps02 out of", "free late used on",
                       "late submissions"]
    assert rows[1][:5] == ["Jane Doe", "jd@x.edu", "111", "38", "40"]
    assert rows[1][7] == "ps01" and "ps01: 2 h late (free)" in rows[1][8]
    # changing the decision on re-export releases the free late
    again.record("111", "ps01", {"total": 36, "out_of": 40, "hours_late": 2,
                                 "action": "apply"})
    assert again.free_late_used_on("111") is None


def test_course_dir_and_key(tmp_path):
    g = tmp_path / "math221" / "ps01" / "grading"
    g.mkdir(parents=True)
    assert late.assignment_key(g) == "ps01"
    assert late.course_dir(g) == (tmp_path / "math221").resolve()
    other = tmp_path / "math221" / "ps02-grading"
    other.mkdir()
    assert late.assignment_key(other) == "ps02-grading"
    assert late.course_dir(other) == (tmp_path / "math221").resolve()


# ------------------------------------------------------ collect: times ----

WS_HEADER = ["Identifier", "Full name", "Email address", "Status", "Grade",
             "Maximum grade", "Grade can be changed",
             "Last modified (submission)", "Last modified (grade)"]


def _write_ws(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(WS_HEADER)
        for mid, name, when in rows:
            w.writerow([f"Participant {mid}", name, f"{mid}@x.edu",
                        "Submitted for grading -  - ", "", "10.00", "Yes",
                        when, when])


def test_collect_reads_worksheet_times(tmp_path):
    src = make_moodle_dir(tmp_path)
    _write_ws(src / "Grades-TEST-PS1--1.csv", [
        ("111", "Jane Doe", "Friday, September 4, 2026, 10:36 PM"),
        ("222", "Rick Roe", "Saturday, September 5, 2026, 1:15 AM")])
    dest = tmp_path / "grading"
    res = collect(src, dest, due="2026-09-04 23:59",
                  timezone_name="America/New_York")
    assert res.worksheet is not None
    jane, rick = res.units
    assert jane.submitted == "2026-09-04T22:36-04:00"
    assert rick.submitted == "2026-09-05T01:15-04:00"
    assert jane.collected and jane.source_sha256
    assert late.read_settings(dest)["due"] == "2026-09-04 23:59"
    assert res.late["Doe-Jane"]["is_late"] is False
    assert res.late["Pitt Roe-Rick"]["is_late"] is True
    assert res.late["Pitt Roe-Rick"]["late_text"] == "1 h 16 min"
    # the worksheet was copied next to the manifest for the return trip
    assert (dest / "Grades-TEST-PS1--1.csv").is_file()
    # ...and a re-download under the same name replaces that copy
    _write_ws(src / "Grades-TEST-PS1--1.csv", [
        ("111", "Jane Doe", "Friday, September 4, 2026, 10:36 PM"),
        ("222", "Rick Roe", "Saturday, September 5, 2026, 1:15 AM"),
        ("333", "Kim Lee", "Sunday, September 6, 2026, 9:00 AM")])
    collect(src, dest)
    assert "Kim Lee" in (dest / "Grades-TEST-PS1--1.csv").read_text()


def test_collect_zip_times_fallback(tmp_path):
    src = make_moodle_dir(tmp_path)
    zpath = tmp_path / "moodle.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for p in src.rglob("*"):
            if p.is_file():
                info = zipfile.ZipInfo(str(p.relative_to(src)),
                                       date_time=(2026, 9, 5, 0, 30, 0))
                z.writestr(info, p.read_bytes())
    res = collect(zpath, tmp_path / "grading",
                  timezone_name="America/New_York")
    assert res.worksheet is None
    assert res.units[0].submitted == "2026-09-05T00:30-04:00"


# --------------------------------------------------- collect: update ------

def test_collect_update_adds_and_preserves(tmp_path):
    src = make_moodle_dir(tmp_path)
    dest = tmp_path / "grading"
    collect(src, dest)
    # instructor hand-edits: a reconstructed tex and a note
    mf = json.loads((dest / "manifest.json").read_text())
    jane = next(u for u in mf["units"] if u["slug"] == "Doe-Jane")
    jane["tex_source"] = "reconstructed"
    jane["anomalies"].append("pdf chosen by hand")
    (dest / "manifest.json").write_text(json.dumps(mf))
    (dest / "submissions" / "Doe-Jane" / "submission.tex").write_text(
        "hand-fixed")
    # a late student appears in the next download
    kim = src / "Lee-Kim_333_assignsubmission_file_"
    kim.mkdir()
    (kim / "kim.pdf").write_bytes(b"%PDF-1.4 kim")
    (kim / "kim.tex").write_text(STUDENT_TEX)

    res = collect(src, dest)
    assert res.update
    assert res.added == ["Lee-Kim"]
    assert sorted(res.unchanged) == ["Doe-Jane", "Pitt Roe-Rick"]
    assert not res.replaced and not res.resubmitted
    mf2 = json.loads((dest / "manifest.json").read_text())
    assert [u["slug"] for u in mf2["units"]] == [
        "Doe-Jane", "Lee-Kim", "Pitt Roe-Rick"]
    jane2 = next(u for u in mf2["units"] if u["slug"] == "Doe-Jane")
    assert jane2["tex_source"] == "reconstructed"
    assert "pdf chosen by hand" in jane2["anomalies"]
    assert jane2["source_sha256"]           # back-filled for next time
    assert (dest / "submissions/Doe-Jane/submission.tex").read_text() \
        == "hand-fixed"
    kim2 = next(u for u in mf2["units"] if u["slug"] == "Lee-Kim")
    assert "added by a later collect" in kim2["anomalies"]
    assert mf2["created"] == mf["created"] and mf2["updated"]


def test_collect_update_resubmission_rules(tmp_path):
    src = make_moodle_dir(tmp_path)
    dest = tmp_path / "grading"
    collect(src, dest)
    # Jane is graded; Rick is not.  Both re-upload.
    rubric = load_rubric(dest, 2)
    GradeStore(dest, rubric).update("Doe-Jane", 1, {"score": 3})
    (src / "Doe-Jane_111_assignsubmission_file_" / "Jane Final (1).pdf") \
        .write_bytes(b"%PDF-1.4 jane v2")
    (src / "Pitt Roe-Rick_222_assignsubmission_file_" / "rick.pdf") \
        .write_bytes(b"%PDF-1.4 rick v2")
    res = collect(src, dest)
    assert res.resubmitted == ["Doe-Jane"] and res.replaced == ["Pitt Roe-Rick"]
    mf = {u["slug"]: u for u in
          json.loads((dest / "manifest.json").read_text())["units"]}
    assert (dest / "submissions/Doe-Jane/submission.pdf").read_bytes() \
        == b"%PDF-1.4 jane"                        # graded: kept
    assert any(a.startswith("resubmitted") and "NOT replaced" in a
               for a in mf["Doe-Jane"]["anomalies"])
    assert (dest / "submissions/Pitt Roe-Rick/submission.pdf").read_bytes() \
        == b"%PDF-1.4 rick v2"                     # ungraded: replaced
    assert mf["Pitt Roe-Rick"]["source_sha256"]
    # a third run with nothing new changes nothing
    res2 = collect(src, dest)
    assert not res2.added and not res2.replaced
    assert res2.resubmitted == ["Doe-Jane"]       # still flagged, once
    assert sum(a.startswith("resubmitted")
               for a in json.loads((dest / "manifest.json").read_text())
               ["units"][0]["anomalies"]) == 1


def test_collect_update_legacy_manifest_trusts_clock(tmp_path):
    """Manifests from before source hashes: only a worksheet time after
    the first collect counts as a change; hand-fixed units survive."""
    src = make_moodle_dir(tmp_path)
    dest = tmp_path / "grading"
    collect(src, dest)
    mf = json.loads((dest / "manifest.json").read_text())
    mf["created"] = "2026-09-05T14:00:00+00:00"
    for u in mf["units"]:
        u.pop("source_sha256"); u.pop("submitted"); u.pop("collected")
        u["sha256"]["pdf"] = "0" * 64        # pdf swapped by hand
    (dest / "manifest.json").write_text(json.dumps(mf))
    # the worksheet from the FIRST download is still in the grading folder
    _write_ws(dest / "Grades-TEST--1.csv", [
        ("111", "Jane Doe", "Friday, September 4, 2026, 10:36 PM"),
        ("222", "Rick Roe", "Friday, September 4, 2026, 9:00 PM")])
    _write_ws(src / "Grades-TEST--1.csv", [
        ("111", "Jane Doe", "Friday, September 4, 2026, 10:36 PM"),
        ("222", "Rick Roe", "Sunday, September 6, 2026, 9:00 AM")])
    res = collect(src, dest, timezone_name="America/New_York")
    assert res.unchanged == ["Doe-Jane"]          # before created: kept
    assert res.replaced == ["Pitt Roe-Rick"]      # re-uploaded afterwards
    mf2 = {u["slug"]: u for u in
           json.loads((dest / "manifest.json").read_text())["units"]}
    assert mf2["Doe-Jane"]["submitted"] == "2026-09-04T22:36-04:00"
    # Rick's on-time first submission survives; the re-upload is separate
    assert mf2["Pitt Roe-Rick"]["submitted"] == "2026-09-04T21:00-04:00"
    assert mf2["Pitt Roe-Rick"]["resubmitted"] == "2026-09-06T09:00-04:00"
    assert "Rick Roe,222@x.edu" not in (dest / "Grades-TEST--1.csv").read_text() \
        or "Sunday" in (dest / "Grades-TEST--1.csv").read_text()  # refreshed


def test_collect_fresh_overwrites(tmp_path):
    src = make_moodle_dir(tmp_path)
    dest = tmp_path / "grading"
    collect(src, dest)
    mf = json.loads((dest / "manifest.json").read_text())
    mf["units"][0]["anomalies"].append("hand note")
    (dest / "manifest.json").write_text(json.dumps(mf))
    res = collect(src, dest, fresh=True)
    assert not res.update
    assert "hand note" not in json.loads(
        (dest / "manifest.json").read_text())["units"][0]["anomalies"]


# ------------------------------------------------------------- export -----

@pytest.fixture
def course(tmp_path):
    """<tmp>/math221/ps02/grading with a due date, timestamps and grades:
    Jane 2 h late, Rick 30 h late, Pat on time.  Rubric total 11.5."""
    folder = make_grading_folder(tmp_path / "math221" / "ps02")
    mf = json.loads((folder / "manifest.json").read_text())
    times = {"Doe-Jane": "2026-09-05T01:59-04:00",
             "Roe-Rick": "2026-09-06T05:59-04:00",
             "Poe-Pat": "2026-09-04T20:00-04:00"}
    for u in mf["units"]:
        u["submitted"] = times[u["slug"]]
    (folder / "manifest.json").write_text(json.dumps(mf))
    late.write_setting(folder, "due", "2026-09-04 23:59")
    late.write_setting(folder, "timezone", "America/New_York")
    store = GradeStore(folder, load_rubric(folder, 3))
    for slug in ("Doe-Jane", "Roe-Rick", "Poe-Pat"):
        store.update(slug, 1, {"score": 4})
        store.update(slug, 2, {"score": 2})
        store.update(slug, 3, {"score": 4})   # raw total 10 / 11.5
    _write_ws(folder / "Grades-TEST-PS2--2.csv", [
        ("111", "Jane Doe", "Saturday, September 5, 2026, 1:59 AM"),
        ("222", "Rick Roe", "Sunday, September 6, 2026, 5:59 AM"),
        ("333", "Pat Poe", "Friday, September 4, 2026, 8:00 PM")])
    return folder


def _ws_grades(result):
    rows = list(csv.reader(
        Path(result.worksheet["out"]).open(encoding="utf-8-sig")))
    return {r[0]: r[4] for r in rows[1:]}


def test_export_applies_policy(course):
    # Rick already spent his free late on ps01
    book = late.Gradebook.for_folder(course)
    book.record("222", "ps01", {"total": 9, "out_of": 11.5,
                                "hours_late": 3, "action": "free"})
    book.save()

    result = build_feedback(course, pdf=False)
    assert result.late["late"] == 2
    assert result.late["free"] == 1 and result.late["penalized"] == 1
    grades = _ws_grades(result)
    assert grades["Participant 111"] == "10.00"        # Jane: free late
    assert grades["Participant 222"] == "8.85"         # Rick: 10% of 11.5
    assert grades["Participant 333"] == "10.00"

    rows = list(csv.reader((result.out_dir / "gradebook.csv").open()))
    assert rows[0][-5:] == ["raw_total", "submitted", "late_by",
                            "late_action", "penalty"]
    rick = next(r for r in rows if r[0] == "Roe-Rick")
    assert rick[rows[0].index("total")] == "8.85"
    assert rick[-5:] == ["10", "Sun Sep 6, 5:59 AM", "1 d 6 h", "apply",
                         "1.15"]
    jane = next(r for r in rows if r[0] == "Doe-Jane")
    assert jane[-2:] == ["free", ""]

    html = (result.out_dir / "feedback/Roe-Rick/feedback.html").read_text()
    assert "10% late penalty" in html and "1.15 of 11.5 points" in html
    assert "Total: 8.85 / 11.5" in html
    html_j = (result.out_dir / "feedback/Doe-Jane/feedback.html").read_text()
    assert "free late assignment" in html_j and "Total: 10 / 11.5" in html_j
    html_p = (result.out_dir / "feedback/Poe-Pat/feedback.html").read_text()
    assert "latenote" not in html_p.split("</header>")[0].split("<header")[1]

    book = late.Gradebook.for_folder(course)
    assert book.free_late_used_on("111") == "ps02"
    assert book.free_late_used_on("222") == "ps01"
    jane_e = book.data["students"]["111"]["assignments"]["ps02"]
    assert jane_e["total"] == 10 and jane_e["action"] == "free"
    assert book.data["students"]["111"]["email"] == "111@x.edu"
    assert (late.course_dir(course) / "gradebook.csv").is_file()


def test_export_hold_and_waive(course):
    late.save_decision(course, "Doe-Jane", "waive", note="2 min")
    late.save_decision(course, "Roe-Rick", "discuss")
    result = build_feedback(course, pdf=False)
    grades = _ws_grades(result)
    assert grades["Participant 111"] == "10.00"
    assert grades["Participant 222"] == ""              # held: left blank
    assert result.worksheet["held"] == ["Roe-Rick"]
    assert any("held for discussion" in w for w in result.warnings)
    html = (result.out_dir / "feedback/Roe-Rick/feedback.html").read_text()
    assert "Total: pending" in html and "pending a conversation" in html
    book = late.Gradebook.for_folder(course)
    assert book.data["students"]["222"]["assignments"]["ps02"]["total"] \
        is None
    assert book.free_late_used_on("111") is None        # waived, not spent


def test_export_without_due_date_is_unchanged(tmp_path):
    folder = make_grading_folder(tmp_path / "c" / "ps01")
    GradeStore(folder, load_rubric(folder, 3)).update("Doe-Jane", 1,
                                                      {"score": 4})
    result = build_feedback(folder, pdf=False)
    assert result.late is None
    rows = list(csv.reader((result.out_dir / "gradebook.csv").open()))
    assert rows[0][-2:] == ["total", "out_of"]
    assert not (tmp_path / "c" / "gradebook.json").exists()


def test_export_bad_due_warns(course):
    late.write_setting(course, "due", "someday")
    result = build_feedback(course, pdf=False)
    assert any("late policy not applied" in w for w in result.warnings)


# ---------------------------------------------------------------- api -----

def _server(course, grader_only=False):
    from hwgenie.grade_gui import AppHolder
    holder = AppHolder(root=course, grader_only=grader_only)
    holder.current = holder.get_app(course)
    return _start_server(holder)


def test_api_state_and_late_decision(course):
    server, client = _server(course)
    try:
        st = client.get("/api/state")
        assert st["late"]["due"] == "2026-09-04T23:59-04:00"
        assert st["late"]["key"] == "ps02"
        u = {x["slug"]: x for x in st["units"]}
        assert u["Poe-Pat"]["late"]["is_late"] is False
        assert u["Doe-Jane"]["late"]["late_text"] == "2 h"
        assert u["Doe-Jane"]["late"]["action"] == "free"
        assert u["Roe-Rick"]["late"]["action"] == "free"   # book is empty
        r = client.post("/api/late", {"slug": "Roe-Rick", "action": "apply",
                                      "note": "asked for none"})
        assert r["late"]["action"] == "apply"
        assert r["late"]["penalty_pts"] == 1.15
        assert r["late"]["decision"]["note"] == "asked for none"
        r = client.post("/api/late", {"slug": "Roe-Rick", "action": "extension",
                                      "extension": "2026-09-07 23:59"})
        assert r["late"]["action"] == "extension" and r["late"]["penalty_pts"] == 0
        client.post("/api/late", {"slug": "Roe-Rick", "action": "extension"},
                    expect=400)
        client.post("/api/late", {"slug": "Nobody", "action": "waive"},
                    expect=400)
        page = client.get("/gradebook")            # falls back to the
        assert b"written at the first export" in page   # open assignment
        assert b"Rick Roe" in page and b"3/3 graded" in page
        build_feedback(course, pdf=False)
        page = client.get("/gradebook")
        assert b"with a CSV twin" in page and b"<b>10</b>" in page
    finally:
        server.shutdown()


def test_api_late_locked_on_grader_server(course):
    server, client = _server(course, grader_only=True)
    try:
        st = client.get("/api/state")
        u = {x["slug"]: x for x in st["units"]}
        # tier shown, free-late question deferred to the instructor
        assert u["Doe-Jane"]["late"]["action"] == "apply"
        assert u["Doe-Jane"]["late"]["free_available"] is None
        client.post("/api/late", {"slug": "Doe-Jane", "action": "waive"},
                    expect=403)
        client.get("/gradebook", expect=404)
    finally:
        server.shutdown()


# ------------------------------------------------------- gui collect ------

def _assignment(tmp_path):
    """<tmp>/math221/ps01/{moodle-raw/<zip>+worksheet, build/template}."""
    src = make_moodle_dir(tmp_path)
    ps = tmp_path / "math221" / "ps01"
    (ps / "moodle-raw").mkdir(parents=True)
    (ps / "build").mkdir()
    zpath = ps / "moodle-raw" / "MATH-221-PS1.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, str(f.relative_to(src)))
    _write_ws(ps / "moodle-raw" / "Grades-MATH-221-PS1--9.csv", [
        ("111", "Jane Doe", "Friday, September 4, 2026, 10:36 PM"),
        ("222", "Rick Roe", "Saturday, September 5, 2026, 1:15 AM")])
    (ps / "build" / "PS1-submission.tex").write_text(TEMPLATE)
    return ps, zpath, src


def test_locate_layout(tmp_path):
    from hwgenie.collect import CollectError, locate
    ps, zpath, _ = _assignment(tmp_path)
    plan = locate(zip_path=zpath)
    assert plan["dest"] == ps / "grading" and not plan["update"]
    assert plan["template"] == ps / "build" / "PS1-submission.tex"
    # a zip anywhere else: <stem>-grading beside it, no template
    loose = tmp_path / "loose.zip"
    loose.write_bytes(zpath.read_bytes())
    plan2 = locate(zip_path=loose)
    assert plan2["dest"] == tmp_path / "loose-grading"
    assert plan2["template"] is None
    with pytest.raises(CollectError, match="no Moodle zip"):
        locate(folder=tmp_path / "math221" / "ps02" / "grading")
    with pytest.raises(CollectError):
        locate()


def test_api_collect_and_recollect(tmp_path):
    from hwgenie.grade_gui import AppHolder
    ps, zpath, src = _assignment(tmp_path)
    holder = AppHolder(root=tmp_path / "math221")
    server, client = _start_server(holder)
    try:
        r = client.post("/api/collect", {"zip": str(zpath),
                                         "due": "2026-09-04 23:59",
                                         "timezone": "America/New_York"})
        assert r["ok"] and not r["update"] and r["units"] == 2
        assert r["folder"] == str(ps / "grading")
        assert r["template"].endswith("PS1-submission.tex")
        assert r["worksheet"].endswith("Grades-MATH-221-PS1--9.csv")
        assert r["due"] == "2026-09-04 23:59"
        assert r["late"] == ["Pitt Roe-Rick"]
        assert any("LATE 1 h 16 min" in ln for ln in r["lines"])
        # the folder now shows up in the scan, and opens
        scan = client.get("/api/scan")
        assert [f["path"] for f in scan["folders"]] == [str(ps / "grading")]
        st = client.get("/api/state?folder=" + str(ps / "grading"))
        assert st["late"]["due"] == "2026-09-04T23:59-04:00"
        assert st["n_parts"] == 2                      # template found
        # a late student arrives; Moodle's zip is re-downloaded
        kim = src / "Lee-Kim_333_assignsubmission_file_"
        kim.mkdir()
        (kim / "kim.pdf").write_bytes(b"%PDF-1.4 kim")
        time.sleep(0.02)
        with zipfile.ZipFile(zpath, "w") as z:
            for f in src.rglob("*"):
                if f.is_file():
                    z.write(f, str(f.relative_to(src)))
        r2 = client.post("/api/collect", {"folder": str(ps / "grading")})
        assert r2["update"] and r2["added"] == ["Lee-Kim"]
        assert r2["unchanged"] == 2 and r2["due"] == "2026-09-04 23:59"
        assert r2["template"].endswith("PS1-submission.tex")
        st = client.get("/api/state?folder=" + str(ps / "grading"))
        assert [u["slug"] for u in st["units"]] == [
            "Doe-Jane", "Lee-Kim", "Pitt Roe-Rick"]   # cache refreshed
        client.post("/api/collect", {"zip": "/nope.zip"}, expect=400)
        client.post("/api/collect", {}, expect=400)
    finally:
        server.shutdown()


def test_api_collect_locked_on_grader_server(tmp_path):
    from hwgenie.grade_gui import AppHolder
    ps, zpath, _ = _assignment(tmp_path)
    holder = AppHolder(root=tmp_path / "math221", grader_only=True)
    server, client = _start_server(holder)
    try:
        client.post("/api/collect", {"zip": str(zpath)}, expect=403)
    finally:
        server.shutdown()


def test_open_zip_shortcut_uses_layout(tmp_path):
    from hwgenie.grade_gui import AppHolder
    ps, zpath, _ = _assignment(tmp_path)
    holder = AppHolder(root=tmp_path / "math221")
    app = holder.open_path(zpath)
    assert app.folder == ps / "grading"
    assert app.n_parts == 2


# -------------------------------------------------- instructor solutions --

SOURCE_TEX = "\n".join([
    r"\documentclass[11pt]{article}",
    r"\hwnumber{1}",
    r"\begin{document}",
    r"\begin{problem}",
    r"Part a.  % \begin{solution} in a comment",
    r"\begin{solution}",
    r"    The answer is $x = 2$.",
    r"\end{solution}",
    r"Part b.",
    r"\begin{solution}Inline: trivially.\end{solution}",
    r"\end{problem}",
    r"\begin{problem}",
    r"Only one part.",
    r"\begin{solution}",
    "    \t%Write your solution here",      # a real tab, like the template
    r"\end{solution}",
    r"\end{problem}",
    r"\end{document}",
])


def test_problem_blocks_capture_solutions():
    from hwgenie.grade_gui import template_problem_blocks
    blocks = template_problem_blocks(SOURCE_TEX)
    assert [b["boxes"] for b in blocks] == [[1, 2], [3]]
    assert blocks[0]["solutions"] == {1: "    The answer is $x = 2$.",
                                      2: "Inline: trivially."}
    assert blocks[1]["solutions"] == {}          # blank box: nothing
    assert "HWGRADERBOX1" in blocks[0]["tex"] and "x = 2" not in blocks[0]["tex"]
    # the blank submission template still yields no solutions at all
    assert all(b["solutions"] == {} for b in template_problem_blocks(TEMPLATE))


def test_collect_accepts_source_as_template(tmp_path):
    src = make_moodle_dir(tmp_path)
    tpl = tmp_path / "ps01.tex"
    tpl.write_text(SOURCE_TEX)
    res = collect(src, tmp_path / "grading", template=tpl)
    assert res.template_parts == 3
    blank = tmp_path / "empty.tex"
    blank.write_text(r"\begin{document}nothing\end{document}")
    from hwgenie.collect import CollectError
    with pytest.raises(CollectError, match="no solution boxes"):
        collect(src, tmp_path / "grading2", template=blank)


def test_newest_tex_prefers_source(tmp_path):
    from hwgenie.collect import newest_tex
    build = tmp_path / "build"
    build.mkdir()
    assert newest_tex(build) is None
    sub = build / "PS1-submission.tex"
    sub.write_text("x")
    src = build / "ps01.tex"
    src.write_text("y")
    import os
    now = time.time()
    os.utime(sub, (now, now)); os.utime(src, (now, now))
    assert newest_tex(build) == src                # same age: source wins
    os.utime(sub, (now + 5, now + 5))
    assert newest_tex(build) == sub                # newer file wins


def test_solutions_reach_grader_pane_but_not_feedback(tmp_path):
    from hwgenie.grade_gui import GradingApp
    folder = make_grading_folder(tmp_path / "c" / "ps01")
    (folder / "template.tex").write_text(SOURCE_TEX)
    app = GradingApp(folder)
    pay = app.problems_payload()
    assert set(pay["solutions"]) == {"1", "2"}
    assert "x = 2" in pay["solutions"]["1"]
    assert all("x = 2" not in p["html"] for p in pay["problems"])
    GradeStore(folder, load_rubric(folder, 3)).update("Doe-Jane", 1,
                                                      {"score": 4})
    result = build_feedback(folder, pdf=False)
    html = (result.out_dir / "feedback/Doe-Jane/feedback.html").read_text()
    assert "Part a." in html and "x = 2" not in html and "trivially" not in html


# ------------------------------------------------------- course macros ----

STY = "\\def\\RR{\\mathbb{R}}\n\\newcommand{\\vecv}{\\mathbf{v}}\n"


def test_find_course_dir_walks_up_then_matches_name(tmp_path):
    from hwgenie.collect import find_course_dir, course_macro_text
    course = tmp_path / "math221-fall2026"
    (course / "source" / "problem-sets" / "ps01").mkdir(parents=True)
    (course / "hwgenie.sty").write_text(STY)
    (course / "coursedata.tex").write_text("\\def\\CC{\\mathbb{C}}\n")
    src = course / "source" / "problem-sets" / "ps01" / "ps01.tex"
    src.write_text("x")
    lab = tmp_path / "grading-lab" / "math221" / "ps01" / "grading"
    lab.mkdir(parents=True)
    assert find_course_dir(src, lab) == course              # walked up
    assert find_course_dir(tmp_path / "elsewhere.tex", lab) == course  # by name
    other = tmp_path / "grading-lab" / "math999" / "ps01" / "grading"
    other.mkdir(parents=True)
    assert find_course_dir(None, other) is None
    text = course_macro_text(course)
    assert "\\RR" in text and "\\CC" in text


def test_collect_bundles_course_macros_and_grader_uses_them(tmp_path):
    from hwgenie.collect import COURSE_MACROS
    from hwgenie.grade_gui import GradingApp
    course = tmp_path / "math221-fall2026"
    course.mkdir()
    (course / "hwgenie.sty").write_text(STY)
    src = make_moodle_dir(tmp_path)
    ps = tmp_path / "grading-lab" / "math221" / "ps01"
    (ps / "build").mkdir(parents=True)
    tpl = ps / "build" / "ps01.tex"
    tpl.write_text(SOURCE_TEX.replace("Part a.", r"Part a: $\\vecv \\in \\RR^2$."))
    collect(src, ps / "grading", template=tpl)
    mf = json.loads((ps / "grading" / "manifest.json").read_text())
    assert mf["macros"] == COURSE_MACROS
    assert "\\RR" in (ps / "grading" / COURSE_MACROS).read_text()
    (ps / "grading" / "rubric.yml").write_text("parts:\n- 1.1\n- 1.2\n- 2\n")
    app = GradingApp(ps / "grading")
    pay = app.problems_payload()
    assert pay["macros"]["\\RR"] == "\\mathbb{R}"
    assert pay["macros"]["\\vecv"] == "\\mathbf{v}"
    assert app.part_payload("Doe-Jane", 1)["macros"]["\\RR"] == "\\mathbb{R}"
    # a re-collect without --template keeps the bundle
    collect(src, ps / "grading")
    assert json.loads((ps / "grading" / "manifest.json").read_text())["macros"] \
        == COURSE_MACROS
    # the feedback export sees them too (statement segments)
    GradeStore(ps / "grading", load_rubric(ps / "grading", 3)).update(
        "Doe-Jane", 1, {"score": 4})
    res = build_feedback(ps / "grading", pdf=False)
    html = (res.out_dir / "feedback/Doe-Jane/feedback.html").read_text()
    assert "\\\\RR" in html or "mathbb{R}" in html   # macro table shipped


def test_course_preamble_runtime_fallback(tmp_path):
    """No bundled copy (older folder) but the course clone is on this
    machine: the grader still finds the macros."""
    from hwgenie.grade_gui import GradingApp
    course = tmp_path / "c-fall"
    course.mkdir()
    (course / "hwgenie.sty").write_text(STY)
    folder = make_grading_folder(tmp_path / "grading-lab" / "c" / "ps01")
    assert GradingApp(folder).course_preamble().count("\\RR") == 1
    assert GradingApp(make_grading_folder(tmp_path / "lab2" / "zz" / "ps01")) \
        .course_preamble() == ""


# ----------------------------------------------------- live gradebook ----

def test_course_assignments_layouts(tmp_path):
    from hwgenie.grade_gui import course_assignments
    c = tmp_path / "math221"
    (c / "ps01" / "grading").mkdir(parents=True)
    (c / "ps01" / "grading" / "manifest.json").write_text("{}")
    (c / "ps02-grading").mkdir()
    (c / "ps02-grading" / "manifest.json").write_text("{}")
    (c / "notes").mkdir()
    assert course_assignments(c) == [c / "ps01" / "grading", c / "ps02-grading"]
    assert course_assignments(tmp_path / "nope") == []


def test_gradebook_data_live_and_exported(course):
    from hwgenie.grade_gui import gradebook_data, render_gradebook
    course_dir = late.course_dir(course)                # <tmp>/math221
    # a second, ungraded assignment with only Jane in it
    ps03 = make_grading_folder(course_dir / "ps03")
    late.write_setting(ps03, "due", "2026-09-11 23:59")
    # ps02 was exported for Rick only (say), with his free late spent
    book = late.Gradebook.for_folder(course)
    book.record("222", "ps02", {"total": 8.85, "raw": 10, "out_of": 11.5,
                                "hours_late": 30, "action": "free",
                                "penalty_pts": 0, "exported": "2026-09-07T00:00"},
                name="Rick Roe")
    book.save()
    d = gradebook_data(course_dir)
    assert d["keys"] == ["ps02", "ps03"] and d["has_book"]
    by = {st["moodle_id"]: st for st in d["students"]}
    jane = by["111"]["cells"]["ps02"]
    assert jane["graded"] == 3 and jane["raw"] == 10 and jane["exported"] is None
    assert jane["provisional"] == 10                  # free late → no penalty
    assert jane["late"]["is_late"] and jane["late"]["action"] == "free"
    assert by["111"]["free_late"] == {"used": None, "provisional": "ps02"}
    rick = by["222"]
    assert rick["cells"]["ps02"]["exported"]["total"] == 8.85
    assert rick["free_late"]["used"] == "ps02"
    assert by["111"]["cells"]["ps03"]["graded"] == 0
    assert "ps03" not in by["333"]["cells"] or by["333"]["cells"]["ps03"]["graded"] == 0
    page = render_gradebook(course_dir)
    assert "Gradebook — math221" in page
    assert "<b>8.85</b>" in page and "3/3 graded" in page
    assert "used on ps02" in page and "ps02 \n" not in page
    assert "/grading?folder=" in page


def test_gradebook_route_accepts_course_or_folder(course):
    server, client = _server(course)
    try:
        cdir = str(late.course_dir(course))
        page = client.get("/gradebook?course=" + cdir)
        assert b"Gradebook" in page and b"Jane Doe" in page
        page2 = client.get("/gradebook?folder=" + str(course))
        assert b"Jane Doe" in page2
        empty = client.get("/gradebook?course=" + str(course.parent.parent / "nope"))
        assert b"No grading folders" in empty
        hub = client.get("/grading?pick=1")
        assert b'id="hub"' in hub and b"data-view" in hub
    finally:
        server.shutdown()
