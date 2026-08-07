"""PDF compilation via latexmk (preferred) or pdflatex."""

from __future__ import annotations

import os
import shutil
import subprocess
from glob import escape as glob_escape
from pathlib import Path
from typing import List, Optional, Tuple


def compile_pdf(
    tex_path: Path,
    workdir: Path,
    out_dir: Path,
    extra_inputs: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Compile tex_path to PDF.  workdir is the cwd (so relative image paths
    resolve); out_dir receives the PDF and aux files.  extra_inputs (e.g. the
    repo root holding hwgenie.sty) is added to TEXINPUTS.
    Returns (ok, log_excerpt).
    """
    tex_path = Path(tex_path).resolve()
    out_dir = Path(out_dir).resolve()
    env = os.environ.copy()
    if extra_inputs is not None:
        # trailing ':' keeps the default search path
        env["TEXINPUTS"] = f".:{Path(extra_inputs).resolve()}:"
    if shutil.which("latexmk"):
        cmds = [[
            "latexmk", "-pdf", "-interaction=nonstopmode",
            f"-output-directory={out_dir}", str(tex_path),
        ]]
    else:
        cmd = [
            "pdflatex", "-interaction=nonstopmode",
            "-output-directory", str(out_dir), str(tex_path),
        ]
        cmds = [cmd, cmd]

    stdout = ""
    for cmd in cmds:
        result = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, errors="replace", timeout=300, env=env
        )
        stdout = result.stdout or ""
        if result.returncode != 0:
            return False, _error_excerpt(stdout, result.stderr or "")
    pdf = out_dir / (tex_path.stem + ".pdf")
    if not pdf.exists():
        return False, _error_excerpt(stdout, "PDF was not produced.")
    return True, ""


def _error_excerpt(stdout: str, stderr: str) -> str:
    """Pull the LaTeX error lines (starting with '!') plus context."""
    lines = stdout.splitlines()
    excerpt: List[str] = []
    for i, line in enumerate(lines):
        if line.startswith("!"):
            excerpt.extend(lines[i : i + 4])
            excerpt.append("")
    if not excerpt:
        excerpt = lines[-25:] if lines else stderr.splitlines()[-25:]
    return "\n".join(excerpt).strip()


def cleanup_aux(out_dir: Path, stem: str) -> None:
    for f in Path(out_dir).glob(f"{glob_escape(stem)}.*"):
        if f.suffix in {".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex", ".gz", ".toc"}:
            f.unlink(missing_ok=True)
