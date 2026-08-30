"""Tests for the Courses page helpers (pure parts + state machine)."""

import threading

from hwgenie import course_admin as ca


def test_parse_pin():
    yml = ('      - name: Install hwgenie\n'
           '        run: pip install '
           '"git+https://github.com/tghyde/hwgenie.git@v0.28.0"\n')
    assert ca.parse_pin(yml) == "0.28.0"
    assert ca.parse_pin("no pin here") is None
    assert ca.parse_pin("hwgenie.git@0.5.0") == "0.5.0"


def test_parse_course_yml():
    text = ("# Course-wide settings\n"
            "course: Math 261\n"
            "title: Introduction to Number Theory\n"
            "semester: Fall 2025\n"
            "instructor: Prof. Trevor Hyde\n")
    got = ca.parse_course_yml(text)
    assert got == {"course": "Math 261",
                   "title": "Introduction to Number Theory",
                   "semester": "Fall 2025"}
    assert ca.parse_course_yml("") == {}


def test_latest_version_numeric_not_lexical():
    # v0.9.0 sorts after v0.28.0 lexically — must compare numerically
    assert ca.latest_version(["v0.9.0", "v0.28.0", "v0.10.1"]) == "0.28.0"
    assert ca.latest_version(["junk", "also-junk"]) is None
    assert ca.latest_version([]) is None


def test_repo_from_remote():
    f = ca.repo_from_remote
    assert f("git@github.com:tghyde/math261-fall2025.git") == \
        "tghyde/math261-fall2025"
    assert f("https://github.com/tghyde/math261-fall2025.git") == \
        "tghyde/math261-fall2025"
    assert f("https://github.com/tghyde/math261-fall2025") == \
        "tghyde/math261-fall2025"
    assert f("https://example.com/x/y.git") is None


def test_replace_pin():
    yml = ('        run: pip install '
           '"git+https://github.com/tghyde/hwgenie.git@v0.28.0"\n')
    out = ca.replace_pin(yml, "0.29.0")
    assert "hwgenie.git@v0.29.0" in out
    assert "0.28.0" not in out
    # idempotent on other text
    assert ca.replace_pin("nothing pinned", "0.29.0") == "nothing pinned"
    # unprefixed pins get normalized to v-prefixed
    assert ca.replace_pin("hwgenie.git@0.5.0", "0.29.0") == \
        "hwgenie.git@v0.29.0"


def test_ci_from_runs():
    assert ca.ci_from_runs(None) is None
    assert ca.ci_from_runs({"workflow_runs": []}) is None
    got = ca.ci_from_runs({"workflow_runs": [
        {"status": "completed", "conclusion": "success",
         "html_url": "https://github.com/x/y/actions/runs/1",
         "other": "ignored"}]})
    assert got == {"status": "completed", "conclusion": "success",
                   "url": "https://github.com/x/y/actions/runs/1"}


def test_state_refresh_guard(monkeypatch):
    state = ca._State()
    monkeypatch.setattr(ca, "COURSES", state)
    started = threading.Event()
    release = threading.Event()

    def fake_scan(roots, log=lambda s: None):
        started.set()
        release.wait(5)
        return {"scanned_at": 1.0, "latest": "0.28.0",
                "template_pin": "0.28.0", "courses": [], "errors": []}

    monkeypatch.setattr(ca, "scan", fake_scan)
    assert ca.start_refresh([])["ok"] is True
    started.wait(5)
    # a second refresh (or a sync) while one runs is refused
    assert ca.start_refresh([])["ok"] is False
    assert ca.start_sync(["tghyde/x"], [])["ok"] is False
    release.set()
    for _ in range(100):
        if state.snapshot()["phase"] == "idle":
            break
        threading.Event().wait(0.05)
    snap = state.snapshot()
    assert snap["phase"] == "idle"
    assert snap["data"]["latest"] == "0.28.0"


def test_start_sync_requires_repos(monkeypatch):
    monkeypatch.setattr(ca, "COURSES", ca._State())
    assert ca.start_sync([], [])["ok"] is False


def test_api_routing(monkeypatch):
    state = ca._State()
    state.data = {"scanned_at": 1.0, "courses": []}
    monkeypatch.setattr(ca, "COURSES", state)
    obj, code = ca.api_get("/courses/api/state")
    assert code == 200 and obj["phase"] == "idle"
    assert obj["data"]["scanned_at"] == 1.0
    assert ca.api_get("/courses/api/nope") is None
    assert ca.api_post("/courses/api/nope", {}, []) is None

    calls = {}
    monkeypatch.setattr(ca, "start_refresh",
                        lambda roots: (calls.setdefault("r", roots),
                                       {"ok": True})[1])
    monkeypatch.setattr(ca, "start_sync",
                        lambda repos, roots: (calls.setdefault("s", repos),
                                              {"ok": True})[1])
    assert ca.api_post("/courses/api/refresh", {}, ["x"])[0]["ok"] is True
    obj, code = ca.api_post("/courses/api/sync",
                            {"repos": ["tghyde/a", 5, "tghyde/b"]}, [])
    assert obj["ok"] is True
    assert calls["s"] == ["tghyde/a", "tghyde/b"]   # non-strings dropped


def test_api_pull_push_bump(monkeypatch):
    monkeypatch.setattr(ca, "COURSES", ca._State())
    jobs = []
    monkeypatch.setattr(ca, "_start_job",
                        lambda fn, roots: (jobs.append(fn), {"ok": True})[1])
    for ep in ("pull", "push"):
        obj, code = ca.api_post(f"/courses/api/{ep}", {}, [])
        assert code == 400 and obj["ok"] is False   # repo required
        obj, code = ca.api_post(f"/courses/api/{ep}",
                                {"repo": "tghyde/x"}, [])
        assert code == 200 and obj["ok"] is True
    obj, code = ca.api_post("/courses/api/bump-template", {}, [])
    assert code == 200 and obj["ok"] is True
    assert len(jobs) == 3


def test_do_bump_template_edits_and_pushes(tmp_path, monkeypatch):
    state = ca._State()
    state.data = {"latest": "0.29.0"}
    state.roots = [tmp_path.parent]
    monkeypatch.setattr(ca, "COURSES", state)
    clone = tmp_path
    wf = clone / ".github" / "workflows" / "build.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text('pip install "git+https://github.com/tghyde/'
                  'hwgenie.git@v0.28.0"\n')
    monkeypatch.setattr(ca, "find_clone_of", lambda repo, roots: clone)

    ran = []

    def fake_run(cmd, cwd=None, timeout=0):
        ran.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(ca, "_run", fake_run)
    ca._do_bump_template()
    assert "hwgenie.git@v0.29.0" in wf.read_text()
    assert ["git", "push", "-q"] in ran
    assert any("v0.29.0" in line for line in state.lines)
    # already-current pin: no further commit
    ran.clear()
    ca._do_bump_template()
    assert not any(c[:2] == ["git", "commit"] for c in ran)


def test_find_local_clones(tmp_path, monkeypatch):
    course = tmp_path / "math261"
    (course / ".git").mkdir(parents=True)
    (course / "course.yml").write_text("course: Math 261\n")
    plain = tmp_path / "not-a-course"
    (plain / ".git").mkdir(parents=True)

    def fake_run(cmd, cwd=None, timeout=0):
        class P:
            returncode = 0
            stdout = "git@github.com:tghyde/math261.git\n"
        return P()

    monkeypatch.setattr(ca, "_run", fake_run)
    clones = ca.find_local_clones([tmp_path])
    assert list(clones) == ["tghyde/math261"]
    assert clones["tghyde/math261"] == course.resolve()


def test_render_courses_has_nav_and_lamp():
    page = ca.render_courses()
    assert "__BASE__" not in page and "__NAV__" not in page
    assert "__LAMP__" not in page
    assert 'class="appnav"' in page
    assert "Update All Courses" in page
    assert '/new-course' in page   # New Course lives here now


# ------------------------------------------------- resolve (real git) --

def _git(cwd, *args):
    import subprocess
    p = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
         "-c", "commit.gpgsign=false"] + list(args),
        cwd=cwd, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def _diverged_pair(tmp_path, overlap: bool):
    """origin + clone where the clone is 1 ahead and origin 1 ahead of it.
    ``overlap`` = both sides touched the same file."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")
    other = tmp_path / "overleaf"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "ps01.tex").write_text("problems\n")
    (other / "hwgenie.sty").write_text("v1\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "base")
    _git(other, "push", "-q")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    # local side: a sync commit
    (clone / "hwgenie.sty").write_text("v2\n")
    _git(clone, "commit", "-aqm", "sync sty")
    # remote side: an Overleaf push
    (other / ("hwgenie.sty" if overlap else "ps01.tex")).write_text("edit\n")
    _git(other, "commit", "-aqm", "Updates from Overleaf")
    _git(other, "push", "-q")
    return clone


def _counts(clone):
    _git(clone, "fetch", "-q")
    return tuple(int(x) for x in _git(
        clone, "rev-list", "--left-right", "--count",
        "HEAD...@{upstream}").split())


def _resolve_setup(monkeypatch, clone):
    state = ca._State()
    monkeypatch.setattr(ca, "COURSES", state)
    monkeypatch.setattr(ca, "_known_clones",
                        lambda: {"tghyde/x": str(clone)})
    return state


def test_do_resolve_disjoint_rebases_and_pushes(tmp_path, monkeypatch):
    clone = _diverged_pair(tmp_path, overlap=False)
    assert _counts(clone) == (1, 1)
    state = _resolve_setup(monkeypatch, clone)
    ca._do_resolve("tghyde/x")
    assert _counts(clone) == (0, 0)          # in sync with origin
    # both sides' work survived
    assert (clone / "hwgenie.sty").read_text() == "v2\n"
    assert (clone / "ps01.tex").read_text() == "edit\n"
    assert any("resolved and pushed" in l for l in state.lines)


def test_do_resolve_refuses_overlap(tmp_path, monkeypatch):
    clone = _diverged_pair(tmp_path, overlap=True)
    state = _resolve_setup(monkeypatch, clone)
    ca._do_resolve("tghyde/x")
    assert _counts(clone) == (1, 1)          # untouched
    assert any("BOTH sides" in l for l in state.lines)
    assert any("hwgenie.sty" in l for l in state.lines)


def test_do_resolve_refuses_dirty_and_not_diverged(tmp_path, monkeypatch):
    clone = _diverged_pair(tmp_path, overlap=False)
    (clone / "scratch.tex").write_text("wip\n")
    _git(clone, "add", "scratch.tex")
    state = _resolve_setup(monkeypatch, clone)
    ca._do_resolve("tghyde/x")
    assert _counts(clone) == (1, 1)
    assert any("uncommitted local edits" in l for l in state.lines)
    # clean but merely behind: point at Pull/Push instead
    _git(clone, "reset", "-q", "--hard", "HEAD~1")
    _git(clone, "clean", "-qfd")
    state2 = _resolve_setup(monkeypatch, clone)
    ca._do_resolve("tghyde/x")
    assert any("use Pull or Push" in l for l in state2.lines)


def test_api_resolve_routes(monkeypatch):
    monkeypatch.setattr(ca, "COURSES", ca._State())
    jobs = []
    monkeypatch.setattr(ca, "_start_job",
                        lambda fn, roots: (jobs.append(fn), {"ok": True})[1])
    obj, code = ca.api_post("/courses/api/resolve", {}, [])
    assert code == 400 and obj["ok"] is False
    obj, code = ca.api_post("/courses/api/resolve", {"repo": "tghyde/x"}, [])
    assert code == 200 and obj["ok"] is True and len(jobs) == 1
