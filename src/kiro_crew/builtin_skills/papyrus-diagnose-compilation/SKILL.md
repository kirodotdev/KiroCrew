---
name: papyrus-diagnose-compilation
description: Diagnose and fix LaTeX compilation failures. Use when the document does not compile, pdflatex/bibtex/biber/tectonic errors out, the PDF is stale, or the author reports a build/compile error in the Papyrus editor.
triggers: pdflatex error, tectonic error, bibtex, biber, latex compile, undefined control sequence, missing $, stale pdf, .tex will not compile
---

# Diagnose LaTeX Compilation

Turn a failed build into a located, classified, minimally-fixed error. Never
mass-rewrite the source to "make it compile" — find the one thing that broke.

In Papyrus the app owns compilation (Cmd+S / Ctrl+S), so you normally read the
error from the app's clickable diagnostics list rather than compiling yourself.
The steps below apply whether you are reading that list or, outside the app,
running the compiler directly.

## Step 1 — Read the FIRST real error
Work from the first error, not the last: LaTeX errors cascade, and the tail of
the log is usually damage from the head. In the app, the diagnostics list is
already ordered — start at the top. If you do have a raw log, the first
`file:line: message` (or `! …`) line is the one to fix.

`-file-line-error` makes a compiler print `file:line: message`. If citations/refs
are wrong (not the compile itself), the full cycle is `pdflatex → bibtex`/`biber`
`→ pdflatex → pdflatex`; Tectonic runs that cycle itself.

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
  (don't remove the `\cite`), then run bibtex/biber and two more pdflatex passes.
- `There's no line here to end` → a `\\` on an otherwise-empty line.
- `Overfull \hbox`, `Underfull`, most `Warning:` lines are WARNINGS, not errors —
  do NOT "fix" them by hacking spacing (see `papyrus-writing`: never hack
  margins).

Apply the smallest fix at the identified line, recompile (ask the author to press
Cmd+S, or rerun the compiler), and confirm THAT error is gone before moving on.
Report what broke and exactly what you changed.
