---
name: papyrus-latex-comments
description: Set up and use a non-destructive COMMENT layer in a LaTeX document — insert margin/inline notes (tagged by type) next to the author's text instead of rewriting it. Use when asked to comment on, review, or annotate a .tex file. For proposing actual replacement text the author can accept/reject, use papyrus-latex-suggestions.
triggers: latex comment, annotate tex, todonotes, margin note, aicomment
---

# LaTeX Comments

Add a visible, attributable, removable layer of NOTES to a LaTeX document —
remarks ABOUT the text, never a rewrite of it. Insert a macro call next to the
relevant text. See `papyrus-writing` for the behaviour rules and the comment
taxonomy. To propose concrete replacement text (accept/reject), use
`papyrus-latex-suggestions`.

## Step 1 — Setup (idempotent)

```bash
kpsewhich todonotes.sty    # non-empty = installed (ships with TeX Live — usually no install)
```

If the document has no comment preamble yet, inject this once at the end of the
preamble, after all existing \usepackage lines (never duplicate it if it is
already there):

```latex
% ==== Papyrus comment layer — safe to leave in; flip to hide ====
% Load todonotes ONLY if the document hasn't already loaded it (avoids an option clash).
\makeatletter
\@ifpackageloaded{todonotes}{}{\usepackage[textsize=scriptsize]{todonotes}}
\makeatother
\newcommand{\aicomment}[1]{\todo[color=cyan!25,bordercolor=cyan]{\textbf{AI:} #1}}   % margin note
% FINAL BUILD: hide every note by adding the `disable` option to todonotes
% (wherever it is loaded): \usepackage[disable]{todonotes}.
% ================================================================
```

Margin space is tight, so `textsize=scriptsize` keeps notes readable in a
one-column margin. Comments live in the **margin only** — a note must never
change the paper's length or reflow the text. In a two-column or narrow-margin
paper, keep each note short rather than pushing it into the text flow.

## Step 2 — Insert comments

Add a macro call next to the relevant text. Do NOT rewrite the prose. Tag every
note with the taxonomy (`[claim] [cite] [clarity] [structure] [contribution]
[rigor] [style] [typo]`).

```latex
The method is fast.\aicomment{[claim] by how much? add a number + CI}
This paragraph reads like Related Work.\aicomment{[structure] consider moving it there}
We imitate the baseline.\aicomment{[clarity] ``imitate'' is imprecise — say exactly what you did to the baseline}
```

A comment flags a problem or asks a question — it never carries the replacement
text; proposing concrete old→new wording is `papyrus-latex-suggestions`.

Do NOT add `\listoftodos` — it inserts a full page and changes the paper's
length. The margin notes themselves are the review list: the author addresses
each one and deletes its macro call.

## Step 3 — Finalize

Hide every note in one edit: switch the package line to
`\usepackage[disable]{todonotes}`. Remove leftover macro calls once a note is
addressed.

## Rules

- Comments live in the **margin** and must not change the paper's length — no `\listoftodos`, no full-width inline blocks.
- Insert macro calls; never rewrite prose the author did not ask you to change.
- Never fabricate a citation to fill a `[cite]` note.
- One sentence per line in the source keeps every suggested diff minimal.
- Never rename or "tidy" the author's macros, labels, or bib keys.
