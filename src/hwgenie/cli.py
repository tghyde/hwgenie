"""Command-line interface: hwgenie build <file.tex>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import BuildError, build
from .metadata import MetadataError


def run_build_site(args) -> int:
    from datetime import date

    from .site import build_site

    try:
        today = date.fromisoformat(args.date) if args.date else None
    except ValueError:
        print(f"error: invalid --date {args.date!r}", file=sys.stderr)
        return 1
    try:
        result = build_site(
            Path(args.root),
            out_dir=args.out,
            compile_pdfs=not args.no_pdf,
            today=today,
        )
    except (BuildError, MetadataError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Site built in: {result.out_dir}")
    for a in result.assignments:
        kind = {"lesson": "Lesson", "syllabus": "Syllabus"}.get(
            a.meta.doc_type, "Problem Set")
        label = f"{kind} {a.meta.number}".strip()
        status = "released" if a.released else "solutions hidden"
        print(f"  {label} ({status}) -> {a.rel_url}")
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in result.errors:
        print(f"\nerror: {e}", file=sys.stderr)
    return 0 if result.ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="hwgenie",
        description="Generate handout/submission/solutions files from a single "
        "LaTeX homework source.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build all derivative files.")
    p_build.add_argument("source", help="Source .tex file (extension optional).")
    p_build.add_argument(
        "--out", type=Path, default=None,
        help="Output directory (default: a 'build' folder next to the source).",
    )
    p_build.add_argument(
        "--no-pdf", action="store_true",
        help="Skip PDF compilation; write the intermediate .tex files instead.",
    )
    p_build.add_argument(
        "--no-html", action="store_true",
        help="Skip HTML generation.",
    )
    p_build.add_argument(
        "--use-metadata-path", action="store_true",
        help="Write outputs to the legacy 'path' folder from the metadata block "
        "(old hw_gen.py behavior).",
    )

    p_site = sub.add_parser(
        "build-site", help="Build the whole course website from a repo."
    )
    p_site.add_argument(
        "root", nargs="?", default=".",
        help="Repo root containing course.yml and source/ (default: cwd).",
    )
    p_site.add_argument(
        "--out", type=Path, default=None,
        help="Output directory (default: <root>/site).",
    )
    p_site.add_argument("--no-pdf", action="store_true", help="Skip PDFs.")
    p_site.add_argument(
        "--date", default=None,
        help="Override today's date (YYYY-MM-DD) for release gating.",
    )

    from .new_course import add_parser as add_new_course_parser
    add_new_course_parser(sub)
    from .sync_template import add_parser as add_sync_parser
    add_sync_parser(sub)
    from .collect import add_parser as add_collect_parser
    add_collect_parser(sub)
    from .grade import add_parser as add_grade_parser
    add_grade_parser(sub)

    args = parser.parse_args(argv)

    if args.command == "build-site":
        return run_build_site(args)
    if args.command == "new-course":
        from .new_course import run_new_course
        return run_new_course(args)
    if args.command == "sync-template":
        from .sync_template import run_sync_template
        return run_sync_template(args)
    if args.command == "collect":
        from .collect import run_collect
        return run_collect(args)
    if args.command == "grade":
        from .grade import run_grade
        return run_grade(args)

    try:
        result = build(
            Path(args.source),
            out_dir=args.out,
            compile_pdfs=not args.no_pdf,
            html=not args.no_html,
            use_metadata_path=args.use_metadata_path,
        )
    except (BuildError, MetadataError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Problem Set {result.meta.number} "
          f"({result.meta.course} {result.meta.semester})")
    print(f"Output folder: {result.out_dir}")
    for key, path in result.files.items():
        print(f"  [{key}] {path.name}")
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in result.errors:
        print(f"\nerror: {e}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
