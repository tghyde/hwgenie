"""Tests for the external grading server integration (remote_grading)."""

import json
import time

import pytest

from test_grade import _start_server, make_grading_folder

import hwgenie.remote_grading as rg
from hwgenie.grade import GradeError


@pytest.fixture
def grading_folder(tmp_path):
    return make_grading_folder(tmp_path)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "remote.json"
    path.write_text(json.dumps({
        "host": "testhost", "root": "/srv/lab",
        "url": "https://example.test/grading"}))
    monkeypatch.setattr(rg, "CONFIG_PATH", path)
    return rg.load_config()


def test_load_config_defaults(cfg):
    assert cfg["host"] == "testhost"
    assert cfg["owner"] == "hwgrader:hwgrader"       # defaults filled in
    assert cfg["python"] == "/opt/hwgenie/bin/python"


def test_load_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "CONFIG_PATH", tmp_path / "nope.json")
    assert rg.load_config() is None


def test_server_name(tmp_path):
    assert rg.server_name(tmp_path / "ps01" / "grading") == "ps01"
    assert rg.server_name(tmp_path / "ps02-grading") == "ps02-grading"


def test_push_stages_and_bundles_template(grading_folder, cfg, monkeypatch,
                                          tmp_path):
    # the fixture manifest's template path is folder-relative; make it
    # absolute to exercise the bundling
    mf = grading_folder / "manifest.json"
    m = json.loads(mf.read_text())
    m["template"]["path"] = str(grading_folder / "template.tex")
    mf.write_text(json.dumps(m))
    (grading_folder / "return").mkdir()
    (grading_folder / "return" / "junk.txt").write_text("x")

    calls = []

    def fake_run(cmd, log, input_text=None, timeout=600):
        calls.append(cmd)
        if cmd[0] == "ssh" and "test -d" in cmd[-1]:
            raise GradeError("no grades/ on the server yet")
        if cmd[0] == "rsync":
            # the staged copy is arg -2 (trailing slash) — check contents
            stage = cmd[-2].rstrip("/")
            staged = json.loads(
                (rg.Path(stage) / "manifest.json").read_text())
            assert staged["template"]["path"] == "template.tex"
            assert (rg.Path(stage) / "template.tex").is_file()
            assert not (rg.Path(stage) / "return").exists()
        return ""

    monkeypatch.setattr(rg, "_run", fake_run)
    name = rg.push(grading_folder, cfg)
    # the fixture folder is literally named "grading" -> parent's name
    assert name == grading_folder.parent.name
    assert calls[0][0] == "ssh" and "test -d" in calls[0][-1]  # grades?
    rs = calls[1]
    assert rs[0] == "rsync" and "--delete" in rs
    assert rs[-1] == f"testhost:/srv/lab/{name}/"
    assert "late.json" in rs and "grades/" not in rs   # first push seeds
    assert calls[2][0] == "ssh"       # chown follows
    assert "chown -R hwgrader:hwgrader" in calls[2][-1]


def test_repush_never_touches_server_grades(grading_folder, cfg, monkeypatch):
    calls = []

    def fake_run(cmd, log, input_text=None, timeout=600):
        calls.append(cmd)
        if cmd[0] == "ssh" and "test -d" in cmd[-1]:
            return ""          # the server already has grades/
        return ""

    monkeypatch.setattr(rg, "_run", fake_run)
    rg.push(grading_folder, cfg)
    rs = next(c for c in calls if c[0] == "rsync")
    assert "grades/" in rs and rs[rs.index("grades/") - 1] == "--exclude"


def test_first_push_seeds_grades(grading_folder, cfg, monkeypatch):
    def fake_run(cmd, log, input_text=None, timeout=600):
        if cmd[0] == "ssh" and "test -d" in cmd[-1]:
            raise GradeError("no such dir")
        return ""

    calls = []
    monkeypatch.setattr(rg, "_run", lambda *a, **k: (calls.append(a[0]),
                                                      fake_run(*a, **k))[1])
    rg.push(grading_folder, cfg)
    rs = next(c for c in calls if c[0] == "rsync")
    assert "grades/" not in rs


def test_push_rejects_non_grading_folder(tmp_path, cfg):
    with pytest.raises(GradeError, match="not a grading folder"):
        rg.push(tmp_path, cfg)


def test_pull_syncs_grades_only(grading_folder, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        rg, "_run",
        lambda cmd, log, input_text=None, timeout=600: calls.append(cmd))
    rg.pull(grading_folder, cfg)
    (cmd,) = calls
    assert cmd[0] == "rsync" and "--delete" not in cmd
    assert cmd[-2] == (f"testhost:/srv/lab/{grading_folder.parent.name}"
                       "/grades/")
    assert cmd[-1].endswith("/grades/")
    assert (grading_folder / "grades").is_dir()


def test_remote_list_parses_json(cfg, monkeypatch):
    monkeypatch.setattr(
        rg, "_run",
        lambda cmd, log, input_text=None, timeout=600:
            '[{"name": "ps01", "units": 3, "graded": 1, "total": 9}]\n')
    lst = rg.remote_list(cfg)
    assert lst[0]["name"] == "ps01"


def test_remote_list_bad_output(cfg, monkeypatch):
    monkeypatch.setattr(
        rg, "_run",
        lambda cmd, log, input_text=None, timeout=600: "garbage")
    with pytest.raises(GradeError, match="parse"):
        rg.remote_list(cfg)


# ----------------------------------------------------------- api / server --

def _wait_idle(client, tries=100):
    for _ in range(tries):
        st = client.get("/api/remote")
        if not st["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("remote job never finished")


@pytest.fixture
def fresh_state(monkeypatch):
    monkeypatch.setattr(rg, "REMOTE", rg._State())
    return rg.REMOTE


def test_api_remote_endpoints(grading_folder, cfg, fresh_state,
                              monkeypatch):
    from hwgenie.grade_gui import AppHolder

    monkeypatch.setattr(
        rg, "remote_list",
        lambda c, log=None: [{"name": "grading", "units": 3,
                              "graded": 2, "total": 9}])
    pulled = []
    monkeypatch.setattr(rg, "pull",
                        lambda folder, c, log: pulled.append(folder))

    holder = AppHolder(root=grading_folder)
    server, client = _start_server(holder)
    try:
        st = client.get("/api/remote")
        assert st["configured"] is True and st["assignments"] is None
        assert client.post("/api/remote/scan", {})["ok"] is True
        st = _wait_idle(client)
        assert st["error"] is None
        assert st["assignments"][0]["name"] == "grading"
        assert st["age"] is not None

        r = client.post("/api/remote/pull",
                        {"path": str(grading_folder)})
        assert r["ok"] is True
        st = _wait_idle(client)
        assert st["error"] is None
        assert pulled == [grading_folder]
    finally:
        server.shutdown()


def test_api_remote_unconfigured(grading_folder, fresh_state, monkeypatch,
                                 tmp_path):
    from hwgenie.grade_gui import AppHolder

    monkeypatch.setattr(rg, "CONFIG_PATH", tmp_path / "none.json")
    holder = AppHolder(root=grading_folder)
    server, client = _start_server(holder)
    try:
        st = client.get("/api/remote")
        assert st["configured"] is False
        err = client.post("/api/remote/scan", {}, expect=400)
        assert "no grading server configured" in err["error"]
    finally:
        server.shutdown()


def test_api_remote_hidden_in_grader_mode(grading_folder, cfg,
                                          fresh_state):
    from hwgenie.grade_gui import AppHolder

    holder = AppHolder(root=grading_folder, grader_only=True)
    holder.current = holder.get_app(grading_folder)
    server, client = _start_server(holder)
    try:
        client.get("/api/remote", expect=404)
        client.post("/api/remote/scan", {}, expect=404)
        client.post("/api/remote/push",
                    {"path": str(grading_folder)}, expect=404)
    finally:
        server.shutdown()


def test_api_remote_job_error_surfaces(grading_folder, cfg, fresh_state,
                                       monkeypatch):
    from hwgenie.grade_gui import AppHolder

    def boom(c, log=None):
        raise GradeError("ssh: connection refused")

    monkeypatch.setattr(rg, "remote_list", boom)
    holder = AppHolder(root=grading_folder)
    server, client = _start_server(holder)
    try:
        assert client.post("/api/remote/scan", {})["ok"] is True
        st = _wait_idle(client)
        assert "connection refused" in st["error"]
        assert st["running"] is None      # not stuck
    finally:
        server.shutdown()


def test_push_bundles_template_from_relative_or_build(grading_folder, cfg,
                                                       monkeypatch):
    """A manifest with a relative (cwd-dependent) template path, or none
    that resolves, still ships the ../build template to the server."""
    mf = grading_folder / "manifest.json"
    m = json.loads(mf.read_text())
    m["template"]["path"] = "../somewhere/else/PS1-submission.tex"
    mf.write_text(json.dumps(m))
    (grading_folder / "template.tex").unlink()
    build = grading_folder.parent / "build"
    build.mkdir()
    (build / "PS1-submission-Math.tex").write_text("TEMPLATE FROM BUILD")
    staged = {}

    def fake_run(cmd, log, input_text=None, timeout=600):
        if cmd[0] == "ssh" and "test -d" in cmd[-1]:
            raise GradeError("no grades yet")
        if cmd[0] == "rsync":
            stage = rg.Path(cmd[-2].rstrip("/"))
            staged["manifest"] = json.loads((stage / "manifest.json").read_text())
            staged["template"] = (stage / "template.tex").read_text()
        return ""

    monkeypatch.setattr(rg, "_run", fake_run)
    rg.push(grading_folder, cfg)
    assert staged["manifest"]["template"]["path"] == "template.tex"
    assert staged["template"] == "TEMPLATE FROM BUILD"


def test_server_version_and_upgrade(monkeypatch):
    cfg = rg.load_config.__wrapped__() if hasattr(rg.load_config, "__wrapped__") else {
        "host": "testhost", "python": "/opt/hwgenie/bin/python",
        "service": "hwgrader", "port": 8461}
    calls = []

    def fake_run(cmd, log, input_text=None, timeout=600):
        calls.append(cmd)
        if "importlib.metadata" in cmd[-1]:
            return "0.44.0\nactive\n"
        return ""

    monkeypatch.setattr(rg, "_run", fake_run)
    assert rg.server_version(cfg) == {"version": "0.44.0", "active": "active",
                                      "error": None}
    info = rg.upgrade_server(cfg, "0.44.0")
    assert info["version"] == "0.44.0"
    pip = next(c for c in calls if "pip install" in c[-1])
    assert "refs/tags/v0.44.0.tar.gz" in pip[-1] and cfg["python"] in pip[-1]
    restart = next(c for c in calls if "systemctl restart" in c[-1])
    assert "hwgrader" in restart[-1] and ":8461/grading" in restart[-1]

    def dead_run(cmd, log, input_text=None, timeout=600):
        raise GradeError("ssh: connect to host timed out")

    monkeypatch.setattr(rg, "_run", dead_run)
    info = rg.server_version(cfg)
    assert info["version"] is None and "timed out" in info["error"]
