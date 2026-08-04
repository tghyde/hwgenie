# hwgenie

Generates all derivative homework files from a single LaTeX source. Phase 1
(this version) produces:

| Output | Contents |
|---|---|
| `Problem Set N (Course Sem).pdf` | **Handout** — problems + figures, no solutions |
| `Problem Set N [submission] (Course Sem).tex` | **Submission template** — blank `solution` environments, figures removed, metadata removed |
| `Problem Set N [solutions] (Course Sem).pdf` | **Solutions** — everything, with a SOLUTIONS banner |
| `Problem Set N [source] (Course Sem).tex` | Copy of the source |
| `html/problem-set-N.html` | **Handout (HTML)** — responsive, KaTeX math, light/dark |
| `html/problem-set-N-solutions.html` | **Solutions (HTML)** — same, with collapsible solutions |

Referenced images are copied next to the HTML pages. GitHub-Actions
automation arrives in Phase 3.

### HTML notes

- Math is rendered client-side by KaTeX (CDN); `\def`/`\newcommand`/
  `\DeclareMathOperator` macros are extracted from the preamble automatically.
- Problems become numbered cards (`Problem N.k`); solutions are collapsible
  `<details>` blocks, open by default.
- `enumerate` custom labels (`\item[2.]`, `\item[(a)]`) map to proper `<ol>`
  numbering; `lstlisting` becomes a syntax-class-tagged code block; `tabular`
  becomes a real HTML table (horizontally scrollable on small screens).
- `align`/`gather`/`equation` are wrapped for KaTeX (as `aligned`/`gathered`;
  equation numbers are not preserved).
- Preview locally: any static server, e.g.
  `python3 -m http.server -d build/html`.

## Usage

```
hwgenie build "Problem Set 3 [source] (Math 261 Fall 2025).tex"
```

Outputs go to a `build/` folder next to the source. Options:

- `--out DIR` — choose the output folder.
- `--no-pdf` — skip compilation, write intermediate `.tex` files instead.
- `--use-metadata-path` — write to the legacy `path` folder from the metadata
  block (old `hw_gen.py` behavior).

Local development install:

```
cd hwgenie && python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/hwgenie build ...
```

## Source file conventions

**Metadata block** (new format — the old `%Problem Set Data` block still works):

```latex
%===hwgenie===
% type      = problemset
% number    = 3
% title     = Digits and Sage
% course    = Math 261
% semester  = Fall 2025
% solutions = 2025-10-15     <- release date, or "manual", or "released"
%=============
```

`course`/`semester` will eventually move to a per-repo `course.yml`; for now
they live in the file. A bare numeric `course` (e.g. `261`) is displayed as
`Math 261`.

**`%HEADER` marker** — put a `%HEADER` line after the title. It is removed in
the handout and replaced with a SOLUTIONS / SUBMISSION banner in those versions.

**Solutions** — wrap in `\begin{solution}...\end{solution}`. Removed from the
handout; replaced by an empty environment (indentation preserved) in the
submission template.

**Images** — either `figure` environments or `center` environments containing
an `\includegraphics` are removed from the submission template (both forms are
detected; the old script only handled `figure`).

**Answer tables** — put `%CLEAR` at the start of a `tabular` body
(e.g. `\begin{tabular}{|c|c|} %CLEAR`) to keep the header row and first column
but blank all other cells in the handout and submission versions. Cells
containing nested `&`/`\\` (matrices etc.) are handled correctly.

## Robustness notes (vs. the old hw_gen.py)

- Real LaTeX parsing (pylatexenc) instead of regexes: nested environments,
  comments containing `\end{solution}`, and code listings with `%`/`#` cannot
  corrupt the output.
- Verbatim-like environments (`lstlisting`, `verbatim`) are masked before
  parsing, so nothing inside them is ever interpreted.
- All edits are span-based on the original text — untouched parts of the file
  come out byte-identical.
- `latexmk` drives compilation (automatic rerun handling; no `\eqref` grep).
- The generated submission file is verified to compile on its own.

## Development

```
.venv/bin/pytest tests/
```
