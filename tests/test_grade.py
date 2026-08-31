"""Tests for hwgenie grade (data model + grading web app)."""

import json
import threading
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from hwgenie.cli import main
from hwgenie.grade import (
    GradeError,
    GradeStore,
    body_is_empty,
    extract_solution_bodies,
    infer_n_parts,
    load_groups,
    load_manifest,
    load_rubric,
    split_preamble,
)

STUDENT_TEX = "\n".join([
    r"\documentclass[11pt]{article}",
    r"% type each answer between \begin{solution} and \end{solution}",
    r"\newcommand{\ZZ}{\mathbb{Z}}",
    r"\newcommand{\yourcollaborators}{None}",
    r"\begin{document}",
    r"\begin{solution}",
    r"We have $x \in \ZZ$.  % a kept comment",
    r"",
    r"Second paragraph.",
    r"\end{solution}",
    r"Interlude text.",
    r"\begin{solution}",
    "    %Write your solution here",
    r"\end{solution}",
    r"\begin{solution}inline body\end{solution}",
    r"\end{document}",
])


TEMPLATE_TEX = "\n".join([
    r"\documentclass{article}",
    r"% students type between \begin{solution} and \end{solution}",
    r"\newcommand{\blue}[1]{\textcolor{blue}{#1}}",
    r"\begin{document}",
    r"Assignment intro text.",
    r"\begin{problem}",
    r"    Statement one. \blue{Do part one.}",
    r"    \begin{solution}",
    "        %Write your solution here",
    r"    \end{solution}",
    r"    \blue{Do part two.}",
    r"    \begin{solution}",
    "        %Write your solution here",
    r"    \end{solution}",
    r"\end{problem}",
    r"Between problems.",
    r"\begin{problem}",
    r"    Statement two.",
    r"    \begin{solution}",
    "        %Write your solution here",
    r"    \end{solution}",
    r"\end{problem}",
    r"\end{document}",
])


def make_grading_folder(root: Path) -> Path:
    dest = root / "grading"
    manifest = {
        "created": "2026-08-13T00:00:00+00:00",
        "source": "moodle.zip",
        "template": {"path": "template.tex", "parts": 3},
        "units": [
            {"slug": "Doe-Jane", "moodle_folder": "Doe-Jane_111_x",
             "moodle_id": "111", "pdf": "jane.pdf", "tex": "jane.tex",
             "tex_source": "original", "extras": [], "anomalies": [],
             "sha256": {}, "parts_found": 3, "collaborators": "None"},
            {"slug": "Roe-Rick", "moodle_folder": "Roe-Rick_222_x",
             "moodle_id": "222", "pdf": "rick.pdf", "tex": "recon.tex",
             "tex_source": "reconstructed", "extras": [], "anomalies": [],
             "sha256": {}, "parts_found": 3,
             "collaborators": "Jane D., course notes"},
            {"slug": "Poe-Pat", "moodle_folder": "Poe-Pat_333_x",
             "moodle_id": "333", "pdf": "pat.pdf", "tex": None,
             "tex_source": None, "extras": [],
             "anomalies": ["no tex submitted"], "sha256": {},
             "parts_found": None, "collaborators": None},
        ],
    }
    for u in manifest["units"]:
        d = dest / "submissions" / u["slug"]
        d.mkdir(parents=True)
        (d / "submission.pdf").write_bytes(b"%PDF-1.4 " + u["slug"].encode())
        if u["tex"]:
            (d / "submission.tex").write_text(STUDENT_TEX)
    (dest / "manifest.json").write_text(json.dumps(manifest))
    (dest / "template.tex").write_text(TEMPLATE_TEX)
    (dest / "rubric.yml").write_text("\n".join([
        "# test rubric",
        "parts:",
        "- 1.1: 4",
        "- 1.2: 2.5",
        "- 2.1a",          # no max
    ]) + "\n")
    (dest / "groups.yml").write_text("\n".join([
        "# groups",
        "Doe-Jane:",
        "- Jane Doe",
        "- Extra Member",
    ]) + "\n")
    return dest


@pytest.fixture
def grading_folder(tmp_path):
    return make_grading_folder(tmp_path)


# ------------------------------------------------------------ tex parsing --

def test_extract_solution_bodies():
    bodies = extract_solution_bodies(STUDENT_TEX)
    assert len(bodies) == 3          # the commented mention doesn't count
    assert "$x \\in \\ZZ$" in bodies[0]
    assert "% a kept comment" in bodies[0]      # body keeps its comments
    assert "Second paragraph." in bodies[0]
    assert "Interlude text." not in bodies[0]
    assert bodies[2] == "inline body"


def test_extract_ignores_commented_delimiters():
    tex = "\n".join([
        r"% \begin{solution} in a comment",
        r"\begin{solution}",
        r"real % \end{solution} faked-out end",
        r"\end{solution}",
    ])
    bodies = extract_solution_bodies(tex)
    assert len(bodies) == 1
    assert "real" in bodies[0]
    assert "faked-out end" in bodies[0]  # comment text stays in the body


def test_body_is_empty():
    bodies = extract_solution_bodies(STUDENT_TEX)
    assert not body_is_empty(bodies[0])
    assert body_is_empty(bodies[1])      # only the template marker comment
    assert body_is_empty("\n   \n  % note\n")
    assert not body_is_empty("x")


def test_split_preamble():
    pre = split_preamble(STUDENT_TEX)
    assert r"\newcommand{\ZZ}" in pre
    assert "Second paragraph" not in pre


# ----------------------------------------------------------- rubric/groups --

def test_load_rubric(grading_folder):
    rubric = load_rubric(grading_folder, 3)
    assert [rp.label for rp in rubric] == ["1.1", "1.2", "2.1a"]
    assert [rp.max for rp in rubric] == [4, 2.5, 5]  # no max -> default 5


def test_load_rubric_pads_defaults(grading_folder):
    (grading_folder / "rubric.yml").unlink()
    rubric = load_rubric(grading_folder, 2)
    assert [rp.label for rp in rubric] == ["Part 1", "Part 2"]
    assert all(rp.max == 5 for rp in rubric)


def test_load_rubric_too_long(grading_folder):
    with pytest.raises(GradeError, match="3 parts"):
        load_rubric(grading_folder, 2)


def test_load_rubric_bad_max(tmp_path):
    (tmp_path / "rubric.yml").write_text("parts:\n- 1.1: four\n")
    with pytest.raises(GradeError, match="bad max"):
        load_rubric(tmp_path, 1)


def test_load_groups(grading_folder):
    groups = load_groups(grading_folder)
    assert groups == {"Doe-Jane": ["Jane Doe", "Extra Member"]}


def test_load_groups_missing(tmp_path):
    assert load_groups(tmp_path) == {}


def test_infer_n_parts(grading_folder):
    manifest = load_manifest(grading_folder)
    assert infer_n_parts(manifest) == 3
    manifest["template"] = None
    assert infer_n_parts(manifest) == 3      # falls back to parts_found
    for u in manifest["units"]:
        u["parts_found"] = None
    with pytest.raises(GradeError):
        infer_n_parts(manifest)


def test_load_manifest_missing(tmp_path):
    with pytest.raises(GradeError, match="collect"):
        load_manifest(tmp_path)


# ------------------------------------------------------------ grades store --

@pytest.fixture
def store(grading_folder):
    rubric = load_rubric(grading_folder, 3)
    return GradeStore(grading_folder, rubric)


def test_store_defaults(store):
    data = store.load("Doe-Jane")
    assert set(data["parts"]) == {"1", "2", "3"}
    p1 = data["parts"]["1"]
    assert p1 == {"score": None, "max": 4, "ec": False, "status": "ungraded",
                  "comments": [], "ai_draft": None}


def test_store_update_roundtrip(store):
    store.update("Doe-Jane", 1, {
        "score": 3.5,
        "comments": [{"anchor": "$x \\in \\ZZ$", "text": "nice"},
                     {"anchor": "", "text": "general note"}],
    })
    data = store.load("Doe-Jane")
    p1 = data["parts"]["1"]
    assert p1["score"] == 3.5
    assert p1["status"] == "graded"
    assert p1["comments"][0] == {"anchor": "$x \\in \\ZZ$", "text": "nice"}
    assert p1["comments"][1]["anchor"] is None   # empty anchor normalized
    # clearing the score flips status back
    store.update("Doe-Jane", 1, {"score": None})
    assert store.load("Doe-Jane")["parts"]["1"]["status"] == "ungraded"


def test_store_integer_scores_stay_integers(store):
    store.update("Doe-Jane", 1, {"score": 4.0})
    raw = json.loads(store.path("Doe-Jane").read_text())
    assert raw["parts"]["1"]["score"] == 4
    assert isinstance(raw["parts"]["1"]["score"], int)


def test_store_rejects_bad_input(store):
    with pytest.raises(GradeError):
        store.update("Doe-Jane", 9, {"score": 1})
    with pytest.raises(GradeError):
        store.update("Doe-Jane", 1, {"score": "abc"})
    with pytest.raises(GradeError):
        store.update("Doe-Jane", 1, {"score": -1})
    with pytest.raises(GradeError):
        store.update("Doe-Jane", 1, {"comments": "not a list"})


def test_store_preserves_ai_draft(store):
    # the AI-review skill writes ai_draft into the same file; app writes
    # must never clobber it (or any unknown future field)
    store.update("Doe-Jane", 1, {"score": 2})
    path = store.path("Doe-Jane")
    raw = json.loads(path.read_text())
    raw["parts"]["1"]["ai_draft"] = {
        "suggested_score": 3, "feedback": "check the base case",
        "issues": ["missing induction step"]}
    raw["reviewed_by"] = "grade-review-skill"
    path.write_text(json.dumps(raw))

    store.update("Doe-Jane", 1, {"score": 3, "comments": []})
    after = json.loads(path.read_text())
    assert after["parts"]["1"]["ai_draft"]["suggested_score"] == 3
    assert after["reviewed_by"] == "grade-review-skill"


def test_store_max_follows_rubric(grading_folder, store):
    store.update("Doe-Jane", 1, {"score": 2})
    (grading_folder / "rubric.yml").write_text("parts:\n- 1.1: 10\n- b: 1\n- c: 1\n")
    rubric = load_rubric(grading_folder, 3)
    store2 = GradeStore(grading_folder, rubric)
    assert store2.load("Doe-Jane")["parts"]["1"]["max"] == 10


def test_progress(store):
    slugs = ["Doe-Jane", "Roe-Rick", "Poe-Pat"]
    assert store.progress(slugs) == (0, 9)
    store.update("Doe-Jane", 1, {"score": 1})
    store.update("Poe-Pat", 2, {"score": 0})     # zero still counts as graded
    assert store.progress(slugs) == (2, 9)


# ------------------------------------------------------------- cli summary --

def test_cli_summary(grading_folder, capsys):
    rubric = load_rubric(grading_folder, 3)
    GradeStore(grading_folder, rubric).update("Doe-Jane", 1, {"score": 4})
    assert main(["grade", str(grading_folder)]) == 0
    out = capsys.readouterr().out
    assert "Doe-Jane" in out and "1/3" in out
    assert "[tex*]" in out and "[no tex]" in out
    assert "Graded 1/9 parts" in out


def test_cli_summary_bad_folder(tmp_path, capsys):
    assert main(["grade", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err


# ----------------------------------------------------------------- web app --

def _start_server(holder):
    from hwgenie.grade_gui import make_handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(holder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Client:
        def get(self, path, expect=200):
            return self._req(urllib.request.Request(base + path), expect)

        def post(self, path, obj, expect=200):
            req = urllib.request.Request(
                base + path, data=json.dumps(obj).encode(), method="POST")
            return self._req(req, expect)

        def _req(self, req, expect):
            try:
                with urllib.request.urlopen(req) as r:
                    code, body, ctype = (r.status, r.read(),
                                         r.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as e:
                code, body, ctype = (e.code, e.read(),
                                     e.headers.get("Content-Type", ""))
            assert code == expect, body
            if ctype.startswith("application/json"):
                return json.loads(body)
            return body

    return server, Client()


@pytest.fixture
def client(grading_folder):
    from hwgenie.grade_gui import AppHolder, GradingApp

    holder = AppHolder(root=grading_folder)
    holder.current = GradingApp(grading_folder)
    server, client = _start_server(holder)
    yield client
    server.shutdown()


@pytest.fixture
def picker_client(grading_folder, tmp_path, monkeypatch):
    import hwgenie.grade_gui as gg

    monkeypatch.setattr(gg, "RECENTS_PATH", tmp_path / "recents.json")
    holder = gg.AppHolder(root=tmp_path)
    server, client = _start_server(holder)
    yield client
    server.shutdown()


def test_api_state(client):
    s = client.get("/api/state")
    assert s["n_parts"] == 3
    assert [r["label"] for r in s["rubric"]] == ["1.1", "1.2", "2.1a"]
    assert s["progress"] == [0, 9]
    jane, rick, pat = s["units"]
    assert jane["members"] == ["Jane Doe", "Extra Member"]
    assert rick["tex_source"] == "reconstructed"
    assert rick["collaborators"] == "Jane D., course notes"
    assert pat["tex"] is False
    assert set(jane["parts"]) == {"1", "2", "3"}


def test_api_part(client):
    p = client.get("/api/part?slug=Doe-Jane&part=1")
    assert "Second paragraph." in p["tex"]
    assert p["html"] and "Second paragraph." in p["html"]
    assert p["empty"] is False
    assert p["macros"].get("\\ZZ") == "\\mathbb{Z}"
    p2 = client.get("/api/part?slug=Doe-Jane&part=2")
    assert p2["empty"] is True
    p3 = client.get("/api/part?slug=Poe-Pat&part=1")   # no tex
    assert p3["tex"] is None and p3["html"] is None
    client.get("/api/part?slug=Nobody&part=1", expect=404)


def test_api_grade_and_progress(client, grading_folder):
    r = client.post("/api/grade", {
        "slug": "Doe-Jane", "part": 1, "score": 4,
        "comments": [{"anchor": "inline body", "text": "ok"}]})
    assert r["ok"] is True
    assert r["parts"]["1"]["status"] == "graded"
    assert r["progress"] == [1, 9]
    # persisted to disk for the export step
    raw = json.loads((grading_folder / "grades" / "Doe-Jane.json").read_text())
    assert raw["parts"]["1"]["score"] == 4
    # validation errors surface as 400s
    err = client.post("/api/grade", {"slug": "Doe-Jane", "part": 1,
                                    "score": "abc"}, expect=400)
    assert err["ok"] is False
    err = client.post("/api/grade", {"slug": "Nobody", "part": 1, "score": 1},
                      expect=400)
    assert "unknown" in err["error"]


def test_template_problem_blocks():
    from hwgenie.grade_gui import template_problem_blocks

    blocks = template_problem_blocks(TEMPLATE_TEX)
    assert [b["num"] for b in blocks] == [1, 2]
    assert blocks[0]["boxes"] == [1, 2]
    assert blocks[1]["boxes"] == [3]
    assert "HWGRADERBOX1" in blocks[0]["tex"]
    assert "HWGRADERBOX3" in blocks[1]["tex"]
    assert "%Write your solution here" not in blocks[0]["tex"]
    assert "Do part two." in blocks[0]["tex"]
    for b in blocks:                       # only problem bodies are kept
        assert "Between problems." not in b["tex"]
        assert "Assignment intro" not in b["tex"]


def test_api_problems(client):
    p = client.get("/api/problems")
    assert [pr["boxes"] for pr in p["problems"]] == [[1, 2], [3]]
    assert "Do part one." in p["problems"][0]["html"]
    assert "HWGRADERBOX1" in p["problems"][0]["html"]


def test_problems_payload_missing_template(grading_folder):
    from hwgenie.grade_gui import GradingApp

    (grading_folder / "template.tex").unlink()
    app = GradingApp(grading_folder)
    payload = app.problems_payload()
    assert payload["problems"] == []


def test_api_pdfmap(client):
    # the fixture PDFs are fake bytes: the map must degrade to empty, not 500
    m = client.get("/api/pdfmap?slug=Doe-Jane")
    assert m == {"parts": {}}
    client.get("/api/pdfmap?slug=Nobody", expect=404)


def test_pdf_and_page(client):
    body = client.get("/pdf/Doe-Jane")
    assert body.startswith(b"%PDF")
    client.get("/pdf/Nobody", expect=404)
    client.get("/pdf/..%2Fmanifest.json", expect=404)   # no traversal
    page = client.get("/grading")
    assert b"hwGenie" in page
    assert b"katex" in page
    # the app's home page is course management
    home = client.get("/")
    assert b"Update All Courses" in home
    assert client.get("/courses") == home   # old URL still works


def test_manifest_and_icons(client):
    # installable as a Chrome app: manifest + real icon PNGs
    m = json.loads(client.get("/manifest.webmanifest"))
    assert m["name"] == "hwGenie" and m["display"] == "standalone"
    png = client.get("/icon-192.png")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert b'rel="manifest"' in client.get("/")


def test_ping_bye_endpoints(client):
    assert client.post("/ping", {})["ok"] is True
    assert client.post("/bye", {})["ok"] is True


def test_watchdog_decision():
    from hwgenie.grade_gui import _watchdog_should_exit as should_exit

    # tab closed: /bye then silence -> exit after the grace period
    assert not should_exit(now=105, started=0, last_ping=99, bye_at=100)
    assert should_exit(now=111, started=0, last_ping=99, bye_at=100)
    # reload: /bye but a newer ping cancels it
    assert not should_exit(now=200, started=0, last_ping=101, bye_at=100)
    # browser gone without /bye: long ping timeout
    assert not should_exit(now=100, started=0, last_ping=20, bye_at=None)
    assert should_exit(now=300, started=0, last_ping=20, bye_at=None)
    # browser never connected at all
    assert not should_exit(now=100, started=0, last_ping=None, bye_at=None)
    assert should_exit(now=301, started=0, last_ping=None, bye_at=None)


# ------------------------------------------------------------------ picker --

def test_picker_flow(picker_client, grading_folder):
    # nothing open: /grading serves the picker, APIs refuse politely
    page = picker_client.get("/grading")
    assert b"Pick the assignment" in page
    err = picker_client.get("/api/state", expect=409)
    assert "no assignment open" in err["error"]
    # the scan finds the grading folder under the root
    scan = picker_client.get("/api/scan")
    assert [f["path"] for f in scan["folders"]] == [str(grading_folder)]
    assert scan["folders"][0]["units"] == 3
    # opening it switches to the grading app and records a recent
    r = picker_client.post("/api/open", {"path": str(grading_folder)})
    assert r["ok"] is True
    assert b"katex" in picker_client.get("/grading")   # grader now
    assert picker_client.get("/api/state")["n_parts"] == 3
    assert picker_client.get("/api/scan")["recents"] == [str(grading_folder)]
    # closing returns to the picker
    picker_client.post("/api/close", {})
    assert b"Pick the assignment" in picker_client.get("/grading")


def test_picker_open_errors(picker_client, tmp_path):
    err = picker_client.post("/api/open", {"path": str(tmp_path / "nope")},
                             expect=400)
    assert err["ok"] is False


def test_picker_opens_moodle_zip(picker_client, tmp_path):
    from test_collect import make_moodle_dir

    src = make_moodle_dir(tmp_path / "zipsrc")
    zpath = tmp_path / "PS1 downloads.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for p in src.rglob("*"):
            z.write(p, p.relative_to(src))
    r = picker_client.post("/api/open", {"path": str(zpath)})
    assert r["ok"] is True
    dest = tmp_path / "PS1 downloads-grading"
    assert (dest / "manifest.json").is_file()
    assert r["folder"] == str(dest)
    state = picker_client.get("/api/state")
    assert {u["slug"] for u in state["units"]} == {"Doe-Jane", "Pitt Roe-Rick"}


# ------------------------------------------- multi-assignment / grader mode --

def test_rubric_extra_credit(tmp_path):
    (tmp_path / "rubric.yml").write_text(
        "parts:\n- 1.1: 4\n- 2.5: 3 ec\n- 2.6: EC\n")
    rubric = load_rubric(tmp_path, 3)
    assert [rp.ec for rp in rubric] == [False, True, True]
    assert [rp.max for rp in rubric] == [4, 3, 5]   # bare "ec" keeps default


def test_store_records_grader(store):
    data = store.update("Doe-Jane", 1, {"score": 3}, by="Alex G.")
    assert data["parts"]["1"]["by"] == "Alex G."
    # a save without a name leaves the last attribution alone
    data = store.update("Doe-Jane", 1, {"score": 2})
    assert data["parts"]["1"]["by"] == "Alex G."


def test_api_folder_param(client, grading_folder, tmp_path):
    # a second assignment on the same server, addressed per-request
    other = make_grading_folder(tmp_path / "other")
    enc = urllib.parse.quote(str(other))
    r = client.post(f"/api/grade?folder={enc}",
                    {"slug": "Doe-Jane", "part": 1, "score": 2, "by": "Sam"})
    assert r["ok"] is True
    assert r["parts"]["1"]["by"] == "Sam"
    raw = json.loads((other / "grades" / "Doe-Jane.json").read_text())
    assert raw["parts"]["1"]["score"] == 2
    assert raw["parts"]["1"]["by"] == "Sam"
    # the default (CLI-opened) assignment is untouched
    assert not (grading_folder / "grades" / "Doe-Jane.json").exists()
    # GET APIs are addressable too; the rubric carries the ec flag
    s = client.get(f"/api/state?folder={enc}")
    assert Path(s["folder"]) == other.resolve()
    assert [rp["ec"] for rp in s["rubric"]] == [False, False, False]
    client.get("/api/state?folder="
               + urllib.parse.quote(str(tmp_path / "nope")), expect=404)


def test_grading_page_folder_param(picker_client, grading_folder):
    enc = urllib.parse.quote(str(grading_folder))
    page = picker_client.get(f"/grading?folder={enc}")
    assert b"katex" in page                      # the grader, not the picker
    assert grading_folder.name.encode() in page  # folder baked into CFG
    # ?pick=1 forces the picker even while an assignment is open
    assert b"Pick the assignment" in picker_client.get("/grading?pick=1")
    # a bad folder redirects back to the picker (urllib follows the 302)
    bad = urllib.parse.quote(str(grading_folder / "nope"))
    assert b"Pick the assignment" in picker_client.get(
        f"/grading?folder={bad}")


@pytest.fixture
def grader_client(grading_folder):
    from hwgenie.grade_gui import AppHolder

    holder = AppHolder(root=grading_folder, grader_only=True)
    holder.current = holder.get_app(grading_folder)
    server, client = _start_server(holder)
    yield client
    server.shutdown()


def test_grader_mode_locks_down(grader_client, tmp_path):
    # home redirects to the grading page; the page knows it's grader mode
    assert b'"grader": true' in grader_client.get("/")
    # course admin, quote bank and the wizard are gone
    grader_client.get("/quotes", expect=404)
    grader_client.get("/new-course", expect=404)
    grader_client.get("/quotes/api/list", expect=404)
    # exporting and opening arbitrary paths are instructor-only
    err = grader_client.post("/api/export", {"pdf": False}, expect=403)
    assert err["ok"] is False
    grader_client.post("/api/open", {"path": str(tmp_path)}, expect=403)
    # the recents list stays private
    assert grader_client.get("/api/scan")["recents"] == []
    # assignments outside the served root are unreachable
    outside = make_grading_folder(tmp_path / "elsewhere")
    enc = urllib.parse.quote(str(outside))
    err = grader_client.get(f"/api/state?folder={enc}", expect=404)
    assert "outside" in err["error"]


def test_grader_mode_grading_still_works(grader_client, grading_folder):
    r = grader_client.post("/api/grade", {
        "slug": "Doe-Jane", "part": 1, "score": 3, "by": "Grader Two"})
    assert r["ok"] is True
    raw = json.loads((grading_folder / "grades" / "Doe-Jane.json").read_text())
    assert raw["parts"]["1"]["by"] == "Grader Two"
    # the picker is reachable and flagged grader-mode
    page = grader_client.get("/grading?pick=1")
    assert b"Pick the assignment" in page and b'"grader": true' in page
