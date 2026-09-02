"""Orchestrates a full build: source .tex → the derivative files.

Phase 1 outputs (problemset):
  1. handout PDF      — solutions removed, figures kept, %CLEAR tables emptied
  2. submission .tex  — solutions blanked, figures removed, %CLEAR tables
                        emptied, metadata removed
  3. solutions PDF    — everything, SOLUTIONS banner
  plus a copy of the source .tex alongside the outputs.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import compile as texcompile
from . import texscan, tikzsvg, transforms
from .courseconfig import find_course_config, load_course_config
from .htmlgen import HtmlConverter
from .htmltemplate import DEFAULT_THEME_CSS, render_page, scrollbar_html, toc_html
from .katexmacros import extract_macros
from .metadata import Metadata, latex_plain, parse_metadata
from .themes import theme_from_config


class BuildError(RuntimeError):
    pass


@dataclass
class BuildResult:
    meta: Metadata
    out_dir: Path
    files: Dict[str, Path] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def read_preamble(sty_dir: Path) -> str:
    """The course preamble text used for theorem/KaTeX macro extraction:
    hwgenie.sty plus coursedata.tex (the course-owned, sync-safe home for
    course-wide macros — the sty loads it via \\InputIfFileExists)."""
    parts = []
    for name in ("hwgenie.sty", "coursedata.tex"):
        p = Path(sty_dir) / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def merge_course_config(meta: Metadata, source_dir: Path) -> Metadata:
    """Fill missing course/semester from a course.yml found near the source."""
    if meta.course and meta.semester:
        return meta
    cfg_path = find_course_config(source_dir)
    if cfg_path:
        cfg = load_course_config(cfg_path)
        meta.course = meta.course or cfg.get("course")
        meta.semester = meta.semester or cfg.get("semester")
    return meta


def require_course_fields(meta: Metadata) -> None:
    missing = [k for k in ("course", "semester") if not getattr(meta, k)]
    if missing:
        raise BuildError(
            f"Missing {', '.join(missing)}: add to the metadata block or to a "
            "course.yml in the repo root."
        )


def compile_variant_pdf(
    tex_content: str,
    out_dir: Path,
    workdir: Path,
    final_name: str,
    stem: str,
    extra_inputs: Optional[Path] = None,
):
    """Compile a tex variant; returns (pdf_path or None, error or None)."""
    tmp_tex = out_dir / f"{stem}.tex"
    tmp_tex.write_text(tex_content, encoding="utf-8")
    ok, log = texcompile.compile_pdf(
        tmp_tex, workdir=workdir, out_dir=out_dir, extra_inputs=extra_inputs
    )
    error = None
    pdf_path = None
    if ok:
        pdf_path = out_dir / final_name
        (out_dir / f"{stem}.pdf").replace(pdf_path)
        tmp_tex.unlink(missing_ok=True)
    else:
        error = (
            f"LaTeX error while compiling {final_name}:\n{log}\n"
            f"(intermediate file kept for inspection: {tmp_tex})"
        )
    texcompile.cleanup_aux(out_dir, stem)
    return pdf_path, error


def output_names(meta: Metadata) -> Dict[str, str]:
    require_course_fields(meta)
    base = f"Problem Set {meta.number}"
    tag = f"({meta.course} {meta.semester})"
    return {
        "source": f"{base} [source] {tag}.tex",
        "handout_pdf": f"{base} {tag}.pdf",
        "submission": f"{base} [submission] {tag}.tex",
        "solutions_pdf": f"{base} [solutions] {tag}.pdf",
    }


def make_variants(text: str, search_dirs=None) -> Dict[str, str]:
    """Return the derived .tex contents from the source text.  When
    search_dirs is given, \\input files are inlined first so every variant is
    self-contained."""
    if search_dirs:
        text = transforms.expand_inputs(text, search_dirs)
    masked = texscan.mask_verbatim(text)
    nodes = texscan.parse_nodes(masked)
    meta = parse_metadata(masked)

    has_marker = "%HEADER" in masked
    uses_sty = transforms.USEPACKAGE_HWGENIE_RE.search(text) is not None

    # Handout: no header line, no solutions, cleared tables.  The injected
    # Handout variant tag disables \solnewpage (and shows no badge).
    handout_edits = (
        transforms.header_edits(masked, "", remove=True)
        + transforms.solution_edits(text, nodes, mode="remove")
        + transforms.clear_table_edits(text, nodes)
        + transforms.hwpreview_edits(masked)
    )
    handout = transforms.apply_edits(text, handout_edits)
    handout = transforms.collapse_blank_lines(handout)
    if not has_marker and uses_sty:
        handout = transforms.inject_variant(handout, "Handout")

    # Solutions: SOLUTIONS banner, everything else untouched.
    solutions_edits = (
        transforms.header_edits(masked, transforms.banner("SOLUTIONS"), remove=False)
        + transforms.hwpreview_edits(masked)
    )
    solutions = transforms.apply_edits(text, solutions_edits)
    if not has_marker and uses_sty:
        solutions = transforms.inject_variant(solutions, "Solutions")

    # Submission: SUBMISSION banner, metadata removed, figures removed,
    # solutions blanked, cleared tables.
    submission_edits = (
        transforms.header_edits(masked, transforms.banner("SUBMISSION"), remove=False)
        + [(meta.span[0], meta.span[1], "")]
        + transforms.figure_edits(text, nodes)
        + transforms.solution_edits(text, nodes, mode="blank")
        + transforms.clear_table_edits(text, nodes)
        + transforms.env_removal_edits(text, nodes, ("htmlonly",))
        + transforms.env_unwrap_edits(text, nodes, ("pdfonly",))
        + transforms.foldeq_edits(text, nodes)
        + transforms.metadata_command_edits(masked)
        + transforms.variant_newpage_edits(masked)
        + transforms.hwpreview_edits(masked)
    )
    submission = transforms.apply_edits(text, submission_edits)
    submission = transforms.collapse_blank_lines(submission)
    if not has_marker and uses_sty:
        submission = transforms.inject_variant(submission, "Submission")
    if search_dirs:
        submission = transforms.inline_sty(submission, search_dirs)

    # Solutions-for-web: like solutions but no banner (the HTML template has
    # its own badge) and no %HEADER line.
    solutions_web = transforms.apply_edits(
        text,
        transforms.header_edits(masked, "", remove=True)
        + transforms.hwpreview_edits(masked),
    )

    return {
        "handout": handout,
        "solutions": solutions,
        "submission": submission,
        "solutions_web": solutions_web,
    }


def resolve_out_dir(
    meta: Metadata,
    source_path: Path,
    out_dir: Optional[Path],
    use_metadata_path: bool,
) -> Path:
    if use_metadata_path:
        if not meta.legacy_path:
            raise BuildError(
                "--use-metadata-path given, but the metadata has no 'path' key."
            )
        return Path(meta.legacy_path) / f"Problem Set {meta.number}"
    if out_dir is not None:
        return Path(out_dir)
    return source_path.parent / "build"


def build_html(
    variant_text: str,
    meta: Metadata,
    include_solutions: bool,
    out_path: Path,
    source_dir: Path,
    result: BuildResult,
    nav: str = "",
    image_dir: Optional[Path] = None,
    sb_home: Optional[tuple] = None,   # (href, label) for the sticky bar
    extra_preamble: str = "",
    theme: str = DEFAULT_THEME_CSS,
    image_search: Optional[list] = None,
    custom_css: str = "",
    favicon: str = "",
    banner: str = "",
    tikz_inputs: Optional[Path] = None,
) -> None:
    # An explicit \setcounter{section}{N} wins over the assignment number
    # (some lessons deliberately use a different theorem-numbering base).
    sec_m = re.search(r"\\setcounter\{section\}\{(\d+)\}", variant_text)
    section = sec_m.group(1) if sec_m else meta.number
    tikz_svgs, tikz_warning = tikzsvg.render_document(
        variant_text, workdir=source_dir, extra_inputs=tikz_inputs
    )
    if tikz_warning:
        result.warnings.append(tikz_warning)
    conv = HtmlConverter(variant_text, include_solutions=include_solutions,
                         section=section, extra_preamble=extra_preamble,
                         tikz_svgs=tikz_svgs)
    body = conv.convert()
    result.warnings.extend(sorted(set(conv.warnings)))

    label = {"lesson": f"Lesson {meta.number}",
             "syllabus": "",
             "handout": f"Handout {meta.number}".strip() if meta.number else "",
             }.get(meta.doc_type, f"Problem Set {meta.number}")
    if conv.title_lines:
        course_line = conv.title_lines[0]
        heading = " — ".join(conv.title_lines[1:]) or label
    else:
        course_line = f"{meta.course}, {meta.semester}"
        if label and meta.title:
            heading = f"{label}: {latex_plain(meta.title)}"
        else:
            heading = label or latex_plain(meta.title) or ""

    title = f"{re.sub('<[^>]+>', '', heading)} — {re.sub('<[^>]+>', '', course_line)}"
    title = re.sub(r"\$", "", title)  # <title> is plain text; drop math delimiters
    solutions_page = include_solutions and meta.doc_type == "problemset"
    if solutions_page:
        title += " (Solutions)"

    # Lessons and handouts get a table of contents built from their
    # headings (problem sets navigate by problem instead).
    toc = ""
    if meta.doc_type != "problemset" and len(conv.sections) >= 2:
        toc = toc_html(conv.sections)

    scrollbar = ""
    if sb_home:
        kind = {"lesson": "Lesson", "syllabus": "Syllabus",
                "handout": "Handout"}.get(meta.doc_type, "PS")
        label = f"{kind} {meta.number}".strip()
        if include_solutions and meta.doc_type == "problemset":
            label += " · Solutions"
        scrollbar = scrollbar_html(
            sb_home[0], sb_home[1], label, conv.problem_anchors,
            toc_toggle=bool(toc),
        )

    page = render_page(
        title=title,
        course_line=course_line,
        heading=heading,
        body=body,
        macros=extract_macros(extra_preamble + "\n" + variant_text),
        solutions=solutions_page,
        nav=nav,
        scrollbar=scrollbar,
        theme=theme,
        custom_css=custom_css,
        favicon=favicon,
        banner=banner,
        due="" if solutions_page else latex_plain(meta.due or ""),
        toc=toc,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    img_dest = image_dir if image_dir is not None else out_path.parent
    img_dest.mkdir(parents=True, exist_ok=True)
    search = [source_dir] + list(image_search or [])
    for img in sorted(set(conv.images)):
        src = next((Path(d) / img for d in search if (Path(d) / img).exists()),
                   None)
        if src is not None:
            shutil.copyfile(src, img_dest / Path(img).name)
        else:
            result.warnings.append(f"Image not found, not copied: {img}")


def build(
    source_path: Path,
    out_dir: Optional[Path] = None,
    compile_pdfs: bool = True,
    html: bool = True,
    use_metadata_path: bool = False,
) -> BuildResult:
    source_path = Path(source_path)
    if not source_path.exists() and source_path.suffix != ".tex":
        source_path = source_path.with_suffix(".tex")
    if not source_path.exists():
        raise BuildError(f"Source file not found: {source_path}")

    text = source_path.read_text(encoding="utf-8")
    meta = parse_metadata(texscan.mask_verbatim(text))
    cfg_path = find_course_config(source_path.parent)
    cfg = load_course_config(cfg_path) if cfg_path else {}
    meta.course = meta.course or cfg.get("course")
    meta.semester = meta.semester or cfg.get("semester")
    sty_dir = cfg_path.parent if cfg_path else source_path.parent
    extra_preamble = read_preamble(sty_dir)
    theme = theme_from_config(cfg)
    if meta.doc_type != "problemset":
        raise BuildError(
            f"Document type {meta.doc_type!r} is not supported yet (only 'problemset')."
        )

    result = BuildResult(
        meta=meta,
        out_dir=resolve_out_dir(meta, source_path, out_dir, use_metadata_path),
    )
    result.out_dir.mkdir(parents=True, exist_ok=True)

    variants = make_variants(text, search_dirs=[source_path.parent, sty_dir])
    names = output_names(meta)

    if (
        "%HEADER" not in texscan.mask_verbatim(text)
        and transforms.USEPACKAGE_HWGENIE_RE.search(text) is None
    ):
        result.warnings.append(
            "No %HEADER line found — banners were not inserted anywhere."
        )

    # Source copy + submission tex.
    src_copy = result.out_dir / names["source"]
    if src_copy.resolve() != source_path.resolve():
        shutil.copyfile(source_path, src_copy)
    result.files["source"] = src_copy

    submission_path = result.out_dir / names["submission"]
    submission_path.write_text(variants["submission"], encoding="utf-8")
    result.files["submission"] = submission_path

    # PDFs.
    if compile_pdfs:
        for key, tex_content, pdf_name in (
            ("handout_pdf", variants["handout"], names["handout_pdf"]),
            ("solutions_pdf", variants["solutions"], names["solutions_pdf"]),
        ):
            pdf_path, error = compile_variant_pdf(
                tex_content, result.out_dir, source_path.parent,
                pdf_name, "_hwg_" + key,
                extra_inputs=sty_dir if extra_preamble else None,
            )
            if pdf_path:
                result.files[key] = pdf_path
            if error:
                result.errors.append(error)
    else:
        # Still expose the generated tex for inspection/tests.
        for key in ("handout", "solutions"):
            p = result.out_dir / f"_hwg_{key}.tex"
            p.write_text(variants[key], encoding="utf-8")
            result.files[f"{key}_tex"] = p

    # HTML versions.
    if html:
        html_dir = result.out_dir / "html"
        handout_html = html_dir / f"problem-set-{meta.number}.html"
        solutions_html = html_dir / f"problem-set-{meta.number}-solutions.html"
        build_html(variants["handout"], meta, False, handout_html,
                   source_path.parent, result,
                   extra_preamble=extra_preamble, theme=theme,
                   tikz_inputs=sty_dir if extra_preamble else None)
        build_html(variants["solutions_web"], meta, True, solutions_html,
                   source_path.parent, result,
                   extra_preamble=extra_preamble, theme=theme,
                   tikz_inputs=sty_dir if extra_preamble else None)
        result.files["handout_html"] = handout_html
        result.files["solutions_html"] = solutions_html

    return result
