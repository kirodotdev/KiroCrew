---
name: papyrus-diagnose-compilation
description: Diagnose and fix LaTeX compilation failures. Use when the document does not compile, pdflatex/bibtex/biber/tectonic errors out, the PDF is stale, or the author reports a build/compile error in the Papyrus editor.
triggers: pdflatex error, tectonic error, bibtex, biber, latex compile, undefined control sequence, missing $, stale pdf, .tex will not compile
---

# Diagnose LaTeX Compilation

Turn a failed build into a located, classified, minimally-fixed error. Never
mass-rewrite the source to "make it compile" — find the one thing that broke.

In Papyrus the app owns compilation (Cmd+S / Ctrl+S) and shows the AUTHOR a
clickable diagnostics list. You cannot see that list: read the compiler log the
app leaves in the project's top-level directory (`<main>.log`, named after the
main `.tex`), or ask the author to paste the error. Do not run the compiler
yourself in Papyrus (see `papyrus-writing`). Running the compiler directly
applies only outside the app.

## Step 1 — Read the FIRST real error
Work from the first error, not the last: LaTeX errors cascade, and the tail of
the log is usually damage from the head. The app's list is grouped by kind
(`file:line` errors, then `!` errors, then warnings, then boxes), not log order,
and is parsed from only the last 20,000 characters of the final pass, so its top
row may not be the first error; the `.log` on disk is complete. In a raw log, the first `file:line: message` (or `! …`) line is the
one to fix.

`-file-line-error` makes a compiler print `file:line: message`. If citations/refs
are wrong (not the compile itself), the full cycle is `pdflatex → bibtex`/`biber`
`→ pdflatex → pdflatex`; Tectonic runs that cycle itself. In Papyrus, when the
app compiles with `pdflatex` it runs the `bibtex` cycle for you if the document
cites anything, but never `biber`: a `biblatex` document on the biber backend
will not resolve its citations there — tell the author rather than editing
around it.

## Step 2 — Classify and fix at the source line
Common cause → fix:
- Unescaped special char (`_ % & # $` in text) → escape it (`\_`, `\%`, …).
- Unbalanced brace / missing `}` or `\end{env}` → match the environment.
- `Undefined control sequence` → missing `\usepackage`, or a typo'd/renamed
  macro.
- `File 'x.sty' not found` → package not installed in this TeX distribution;
  prefer a package the document already loads over telling the author to install
  one.
- `File not found` (graphic/`\input`) → wrong path or extension; confirm the file
  exists.
- `Missing $ inserted` → math symbol (`_`, `^`, `\alpha`) used outside math mode;
  wrap in `$…$` or an equation env.
- `Missing \begin{document}` → a stray character before the preamble ended.
- Bib: `Citation 'key' undefined` / stale `.bbl` → add the entry to the `.bib`
  (don't remove the `\cite`), then recompile (in Papyrus the app's compile runs
  bibtex and the extra passes; outside it, run bibtex/biber and two more pdflatex
  passes).
- `There's no line here to end` → a `\\` on an otherwise-empty line.
- `Overfull \hbox`, `Underfull`, most `Warning:` lines are WARNINGS, not errors —
  do NOT "fix" them by hacking spacing (see `papyrus-writing`: never hack
  margins).

Apply the smallest fix at the identified line, recompile (in Papyrus the app
recompiles when your turn ends, so check the new `.log` on the next turn;
outside it, rerun the compiler), and confirm THAT error is gone before moving on.
Report what broke and exactly what you changed.
