---
name: papyrus-latex-suggestions
description: Propose actual inline replacement text (old → new) as accept/reject suggestions — LaTeX "suggesting mode" / track changes — that render directly in the PDF. Use when the author wants concrete edit proposals they can see and accept or reject. For remarks ABOUT the text (notes, not replacements), use papyrus-latex-comments.
triggers: tracked change, track changes, suggesting mode, ulem, aisuggest, suggested edit, latex suggestion
---

# LaTeX Suggestions (inline tracked edits)

Propose concrete replacement text the author sees as struck-through old + colored
new, and can accept or reject — the original stays visible until they decide.
Never silently overwrite. It renders as normal typeset text (via `ulem`), so it
shows in any PDF viewer including the Papyrus preview — no package to install, no
PDF annotations. See `papyrus-writing` for behaviour. Pair every suggestion with
a `papyrus-latex-comments` `\aicomment` saying WHY.

## Step 1 — Setup (idempotent, self-contained)

`ulem` and `xcolor` ship with TeX Live — no `tlmgr install`. If the document has
no suggestion preamble yet, inject this once at the end of the preamble, after all existing \usepackage lines (never
duplicate it if it is already there):

```latex
% Load ulem/xcolor ONLY if the document hasn't already loaded them (avoids an option clash).
\makeatletter
\@ifpackageloaded{ulem}{}{\usepackage[normalem]{ulem}}   % \sout (strike) + \uline (underline); normalem keeps \emph italic
\@ifpackageloaded{xcolor}{}{\usepackage{xcolor}}         % \color for the strike/insert colors
\makeatother
% ==== Papyrus suggestion layer — self-contained ====
% Flip to \showeditsfalse to render the FINAL text only (visually accepts every change).
\newif\ifshowedits \showeditstrue
\newcommand{\aisuggest}[3][]{\ifshowedits{\color{red}\sout{#2}}\,{\color{blue}#3}\else#3\fi}
\newcommand{\txadd}[2][]{\ifshowedits{\color{blue}\uline{#2}}\else#2\fi}
\newcommand{\txremove}[2][]{\ifshowedits{\color{red}\sout{#2}}\else\fi}
% =====================================================================================
```

The optional `[...]` first argument carries metadata (`author=papyrus, id=R12,
status=pending`): it documents the change, but it is not rendered.

## Step 2 — Propose edits (argument order is always old → new)

Edit the source in place; the old text stays visible (struck red) next to the new
(blue). Tag the paired comment with the taxonomy.

```latex
The retrieval system \aisuggest[author=papyrus, id=R1, status=pending]{returns}{surfaces} dozens of candidates.\aicomment{[style] ``surfaces'' fits the paper's voice better than ``returns''.}
We evaluate on a benchmark\txadd[id=R2]{, which we release publicly}.\aicomment{[contribution] state the release explicitly if true.}
It is \txremove[id=R3]{very }fast.\aicomment{[style] hedge/intensifier adds nothing.}
```

**Always place BOTH:** one inline suggestion in the text AND one `\aicomment{[tag]
why}` in the margin (see `papyrus-latex-comments`), together — never a suggestion
with no reason, never a comment where you meant to suggest a fix. Keep each edit
sentence-local and minimal; never reflow the surrounding text.

## Step 3 — Accept / reject

- **Accept one:** delete the macro, keep the new text (for `\aisuggest`, keep the
  `{new}` argument; for `\txadd`, keep its text; for `\txremove`, delete the text).
- **Reject one:** delete the macro, keep the old text (for `\aisuggest`, keep the
  `{old}` argument; for `\txadd`, delete its text; for `\txremove`, keep the text).
- **Accept ALL at once (final render):** flip `\showeditstrue` → `\showeditsfalse`
  in the preamble — every suggestion renders as its final text with no markup, in
  one edit.
- **Rework one:** don't edit a live suggestion in place — it is fiddly and
  error-prone. Reject it (remove the macro, keep the old text) and propose a
  fresh `\aisuggest` instead.

## Step 4 — Version diff (optional)

To show everything that changed between two saved versions: `latexdiff old.tex
new.tex > diff.tex` (or `git-latexdiff` across two revisions).

## Rules

- Propose replacement text; never overwrite silently — the old text stays visible
  until the author accepts.
- Never change meaning: a suggestion improves wording, not the
  claim/number/result/citation.
- Always pair the suggestion with an `\aicomment` giving the reason.
- One sentence per line keeps each suggested diff minimal.
- Never rename the author's macros, labels, or bib keys.
