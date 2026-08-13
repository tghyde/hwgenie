"""Tests for hwgenie collect (Moodle bulk-download normalization)."""

import json
import zipfile
from pathlib import Path

import pytest

from hwgenie.cli import main
from hwgenie.collect import CollectError, collect

TEMPLATE = "\n".join([
    r"\documentclass[11pt]{article}",
    r"\begin{document}",
    r"\begin{solution}",
    "    \t%Write your solution here",
    r"\end{solution}",
    r"\begin{solution}",
    "    \t%Write your solution here",
    r"\end{solution}",
    r"\end{document}",
])

STUDENT_TEX = "\n".join([
    r"\documentclass[11pt]{article}",
    r"% type each answer between \begin{solution} and \end{solution}",
    r"\newcommand{\yourcollaborators}{Alice B., course notes}",
    r"\begin{document}",
    r"\begin{solution}My answer.\end{solution}",
    r"\begin{solution}Another.\end{solution}",
    r"\end{document}",
])


def make_moodle_dir(root: Path) -> Path:
    src = root / "moodle-raw"
    jane = src / "Doe-Jane_111_assignsubmission_file_"
    jane.mkdir(parents=True)
    (jane / "Jane Final (1).pdf").write_bytes(b"%PDF-1.4 jane")
    (jane / "ps1_jane.tex").write_text(STUDENT_TEX)
    rick = src / "Pitt Roe-Rick_222_assignsubmission_file_"
    rick.mkdir(parents=True)
    (rick / "rick.pdf").write_bytes(b"%PDF-1.4 rick")
    (rick / "notes.txt").write_text("hi")
    online = src / "Doe-Jane_111_assignsubmission_onlinetext_"
    online.mkdir(parents=True)
    (online / "onlinetext.html").write_text("<p>hi</p>")
    (src / "unrelated-folder").mkdir()
    return src


@pytest.fixture
def moodle_dir(tmp_path):
    return make_moodle_dir(tmp_path)


@pytest.fixture
def template_file(tmp_path):
    p = tmp_path / "template.tex"
    p.write_text(TEMPLATE)
    return p


def test_collect_from_dir(moodle_dir, template_file, tmp_path):
    dest = tmp_path / "grading"
    result = collect(moodle_dir, dest, template=template_file)

    assert [u.slug for u in result.units] == ["Doe-Jane", "Pitt Roe-Rick"]
    assert result.template_parts == 2

    jane = result.units[0]
    assert jane.moodle_folder == "Doe-Jane_111_assignsubmission_file_"
    assert jane.moodle_id == "111"
    assert jane.pdf == "Jane Final (1).pdf"
    assert jane.tex == "ps1_jane.tex"
    assert jane.tex_source == "original"
    assert jane.parts_found == 2
    assert jane.collaborators == "Alice B., course notes"
    assert jane.anomalies == []
    assert (dest / "submissions/Doe-Jane/submission.pdf").read_bytes() \
        == b"%PDF-1.4 jane"
    assert (dest / "submissions/Doe-Jane/submission.tex").exists()

    rick = result.units[1]
    assert rick.pdf == "rick.pdf"
    assert rick.tex is None
    assert any("no tex" in a for a in rick.anomalies)
    assert rick.extras == ["notes.txt"]
    assert (dest / "submissions/Pitt Roe-Rick/notes.txt").exists()

    assert any("onlinetext" in s for s in result.skipped)
    assert any("unrelated-folder" in s for s in result.skipped)

    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["template"]["parts"] == 2
    assert [u["slug"] for u in manifest["units"]] == ["Doe-Jane", "Pitt Roe-Rick"]
    assert manifest["units"][0]["sha256"]["pdf"]


def test_collect_from_zip(moodle_dir, tmp_path):
    zip_path = tmp_path / "PS 1-12345.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for p in moodle_dir.rglob("*"):
            z.write(p, p.relative_to(moodle_dir))
    dest = tmp_path / "grading-zip"
    result = collect(zip_path, dest)
    assert [u.slug for u in result.units] == ["Doe-Jane", "Pitt Roe-Rick"]
    assert (dest / "submissions/Doe-Jane/submission.pdf").exists()


def test_tex_fallback(moodle_dir, tmp_path):
    fallback = tmp_path / "reconstructed"
    (fallback / "Pitt Roe-Rick").mkdir(parents=True)
    (fallback / "Pitt Roe-Rick/submission.tex").write_text(STUDENT_TEX)
    result = collect(moodle_dir, tmp_path / "grading", tex_fallback=fallback)
    rick = result.units[1]
    assert rick.tex_source == "reconstructed"
    assert rick.parts_found == 2
    assert not any("no tex" in a for a in rick.anomalies)


def test_multiple_pdfs_flagged(tmp_path):
    src = tmp_path / "raw"
    folder = src / "Doe-Jane_111_assignsubmission_file_"
    folder.mkdir(parents=True)
    (folder / "v1.pdf").write_bytes(b"%PDF a")
    (folder / "v2.pdf").write_bytes(b"%PDF b")
    result = collect(src, tmp_path / "grading")
    jane = result.units[0]
    assert jane.pdf is None
    assert any("multiple pdf" in a for a in jane.anomalies)
    assert (tmp_path / "grading/submissions/Doe-Jane/v1.pdf").exists()
    assert (tmp_path / "grading/submissions/Doe-Jane/v2.pdf").exists()


def test_parts_mismatch_flagged(moodle_dir, tmp_path):
    template = tmp_path / "t.tex"
    template.write_text(TEMPLATE + "\n\\begin{solution}\n"
                        "    \t%Write your solution here\n\\end{solution}\n")
    result = collect(moodle_dir, tmp_path / "grading", template=template)
    jane = result.units[0]
    assert jane.parts_found == 2
    assert any("solution boxes; template has 3" in a for a in jane.anomalies)


def test_empty_source_errors(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(CollectError):
        collect(tmp_path / "empty", tmp_path / "grading")


def test_bad_template_errors(moodle_dir, tmp_path):
    bad = tmp_path / "bad.tex"
    bad.write_text(r"\documentclass{article}")
    with pytest.raises(CollectError):
        collect(moodle_dir, tmp_path / "grading", template=bad)


def test_cli_wiring(moodle_dir, tmp_path, capsys):
    rc = main(["collect", str(moodle_dir), "--dest", str(tmp_path / "g")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Collected 2 submissions" in out
    assert "Doe-Jane" in out
