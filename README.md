# Screenshots: inline-code theme colour

Evidence for the PR that gives plain inline `code` a theme-aware colour in every
theme. Kept on this orphan branch (never opened as a PR) so no binaries land in
the main branch history.

- `monokai-dark-before.png` — before the fix: inline code uses the body text
  colour, hard to tell apart from prose.
- `monokai-dark-after.png` — after the fix: inline code uses the theme accent,
  clearly distinct.
- `kiro-dark-before.png` / `kiro-dark-after.png` — the Kiro theme is unchanged
  (its own inline-code colour wins at a higher specificity); the two frames are
  identical.
