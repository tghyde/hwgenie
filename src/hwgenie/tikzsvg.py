"""Render tikzpicture/tikzcd environments to inline SVG for the HTML pages.

The variant text is compiled ONCE per document with the `preview` package
([active,tightpage]): every tikz environment ships out as its own tightly
cropped page, typeset with the document's real preamble (fonts, course
macros, colors all match the PDF).  Compilation targets DVI with PGF's
dvisvgm output driver, so dvisvgm converts the pages to SVG natively —
no Ghostscript required (glyphs become paths via FreeType).

Post-processing makes the SVG theme-reactive: black strokes/fills are
rewritten to `currentColor`, so the diagram follows the page text color
(var(--fg)) in both light and dark mode.  Non-black colors are kept as
authored.  Element ids are namespaced per diagram so several SVGs can be
inlined on one page without collisions.

Rendering degrades gracefully: if latex/dvisvgm are missing or the compile
fails, callers get an empty mapping plus a warning, and the HTML falls back
to the see-the-PDF placeholder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import texscan

TIKZ_ENVS = ("tikzpicture", "tikzcd")

_BEGIN_RE = re.compile(r"\\begin\{(?:tikzpicture|tikzcd)\}")
_DOCCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{[^{}]*\}")
_BEGIN_DOC_RE = re.compile(r"\\begin\{document\}")

_PREVIEW_SETUP = (
    "\\usepackage[active,tightpage]{preview}\n"
    "\\setlength\\PreviewBorder{2pt}\n"
    "\\PreviewEnvironment{tikzpicture}\n"
    "\\PreviewEnvironment{tikzcd}\n"
)
_DRIVER_DEF = "\\def\\pgfsysdriver{pgfsys-dvisvgm.def}\n"

# 1pt (TeX big point in SVG output) = 4/3 CSS px.
_PT_TO_PX = 4.0 / 3.0


def available() -> bool:
    """True when the latex→dvi→svg toolchain is on PATH."""
    return bool(
        (shutil.which("latexmk") or shutil.which("latex"))
        and shutil.which("dvisvgm")
    )


def tikz_positions(text: str) -> List[int]:
    """Offsets of every \\begin{tikzpicture|tikzcd} outside verbatim, in
    document order — the same order preview ships them out."""
    masked = texscan.mask_verbatim(text)
    return [m.start() for m in _BEGIN_RE.finditer(masked)]


def _inject_preview(text: str) -> Optional[str]:
    """Insert the dvisvgm driver + preview setup into a variant's preamble."""
    dc = _DOCCLASS_RE.search(text)
    bd = _BEGIN_DOC_RE.search(text)
    if not dc or not bd:
        return None
    return (
        text[: dc.end()] + "\n" + _DRIVER_DEF
        + text[dc.end(): bd.start()] + _PREVIEW_SETUP
        + text[bd.start():]
    )


def _preamble_signature(workdir: Path, extra_inputs: Optional[Path]) -> str:
    """Cache-buster for the shared preamble files the variant \\usepackage's:
    a change to hwgenie.sty/coursedata.tex must invalidate cached SVGs."""
    sig = []
    for d in filter(None, (workdir, extra_inputs)):
        for name in ("hwgenie.sty", "coursedata.tex"):
            p = Path(d) / name
            if p.exists():
                st = p.stat()
                sig.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(sig)


def _cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "hwgenie-tikzsvg-cache"


ID_RE = re.compile(r"id='([^']+)'")
SIZE_RE = re.compile(
    r"(<svg[^>]*?)\swidth='([0-9.]+)pt'\sheight='([0-9.]+)pt'"
)


def postprocess(svg: str, uid: str) -> str:
    """Recolor black to currentColor, namespace ids, make the size fluid."""
    # Strip the XML prolog and comments; the SVG is inlined into HTML.
    svg = re.sub(r"<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r"<!--.*?-->\s*", "", svg, flags=re.S)

    for attr in ("fill", "stroke", "stop-color"):
        svg = re.sub(
            rf"{attr}='(?:#000|#000000|black)'", f"{attr}='currentColor'", svg
        )

    ids = set(ID_RE.findall(svg))
    for old in sorted(ids, key=len, reverse=True):
        svg = svg.replace(f"id='{old}'", f"id='{uid}-{old}'")
        svg = svg.replace(f"url(#{old})", f"url(#{uid}-{old})")
        svg = svg.replace(f"href='#{old}'", f"href='#{uid}-{old}'")

    m = SIZE_RE.search(svg)
    if m:
        width_px = round(float(m.group(2)) * _PT_TO_PX, 2)
        svg = svg[: m.start()] + (
            f"{m.group(1)} style='width:{width_px}px;max-width:100%;"
            "height:auto'"
        ) + svg[m.end():]
    return svg


def render_document(
    text: str,
    workdir: Path,
    extra_inputs: Optional[Path] = None,
) -> Tuple[Dict[int, str], Optional[str]]:
    """Render every tikz environment in a variant text.

    Returns ({source offset -> svg markup}, warning).  The mapping is empty
    when the document has no tikz environments (no warning) or when the
    toolchain is unavailable / the compile fails (with a warning).
    """
    positions = tikz_positions(text)
    if not positions:
        return {}, None

    injected = _inject_preview(text)
    if injected is None:
        return {}, "tikz→svg: no \\documentclass/\\begin{document} found."

    key = hashlib.sha256(
        (injected + "\0" + _preamble_signature(workdir, extra_inputs)).encode()
    ).hexdigest()
    svgs, warning = _cached_or_render(key, injected, workdir, extra_inputs)
    if warning:
        return {}, warning
    if len(svgs) != len(positions):
        return {}, (
            f"tikz→svg: expected {len(positions)} diagram(s), "
            f"rendered {len(svgs)}; falling back to placeholders."
        )
    return dict(zip(positions, svgs)), None


def _cached_or_render(
    key: str,
    injected: str,
    workdir: Path,
    extra_inputs: Optional[Path],
) -> Tuple[List[str], Optional[str]]:
    cache_file = _cache_dir() / f"{key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8")), None
        except (OSError, ValueError):
            pass

    if not available():
        return [], (
            "tikz→svg: latex/dvisvgm not found; diagrams rendered as "
            "see-the-PDF placeholders."
        )

    svgs, warning = _compile_svgs(key, injected, workdir, extra_inputs)
    if warning is None:
        try:
            _cache_dir().mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(svgs), encoding="utf-8")
        except OSError:
            pass
    return svgs, warning


def _compile_svgs(
    key: str,
    injected: str,
    workdir: Path,
    extra_inputs: Optional[Path],
) -> Tuple[List[str], Optional[str]]:
    env = os.environ.copy()
    if extra_inputs is not None:
        env["TEXINPUTS"] = f".:{Path(extra_inputs).resolve()}:"

    with tempfile.TemporaryDirectory(prefix="hwgenie-tikz-") as tmp:
        tmp_dir = Path(tmp)
        tex = tmp_dir / "_hwg_tikz.tex"
        tex.write_text(injected, encoding="utf-8")

        if shutil.which("latexmk"):
            cmd = [
                "latexmk", "-dvi", "-interaction=nonstopmode",
                f"-output-directory={tmp_dir}", str(tex),
            ]
            cmds = [cmd]
        else:
            cmd = [
                "latex", "-interaction=nonstopmode",
                "-output-directory", str(tmp_dir), str(tex),
            ]
            cmds = [cmd, cmd]
        stdout = ""
        try:
            for cmd in cmds:
                result = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True,
                    errors="replace", timeout=300, env=env,
                )
                stdout = result.stdout or ""
                if result.returncode != 0:
                    return [], f"tikz→svg compile failed:\n{_log_tail(stdout)}"
        except (OSError, subprocess.TimeoutExpired) as e:
            return [], f"tikz→svg compile failed: {e}"

        dvi = tmp_dir / "_hwg_tikz.dvi"
        if not dvi.exists():
            return [], f"tikz→svg: no DVI produced:\n{_log_tail(stdout)}"

        try:
            result = subprocess.run(
                ["dvisvgm", "--no-fonts=1", "--exact-bbox", "--optimize",
                 "--page=1-", "-o", "tikz-%p.svg", str(dvi)],
                cwd=tmp_dir, capture_output=True, text=True,
                errors="replace", timeout=300,
            )
            if result.returncode != 0:
                return [], (
                    "tikz→svg: dvisvgm failed:\n"
                    + (result.stderr or result.stdout or "").strip()[-800:]
                )
        except (OSError, subprocess.TimeoutExpired) as e:
            return [], f"tikz→svg: dvisvgm failed: {e}"

        pages = sorted(
            tmp_dir.glob("tikz-*.svg"),
            key=lambda p: int(p.stem.split("-")[-1]),
        )
        return [
            postprocess(p.read_text(encoding="utf-8"), f"tz{key[:8]}p{i}")
            for i, p in enumerate(pages, start=1)
        ], None


def _log_tail(stdout: str) -> str:
    lines = stdout.splitlines()
    bang = [i for i, l in enumerate(lines) if l.startswith("!")]
    if bang:
        i = bang[0]
        return "\n".join(lines[i: i + 4])
    return "\n".join(lines[-10:])
