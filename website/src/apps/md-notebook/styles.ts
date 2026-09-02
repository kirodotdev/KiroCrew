/**
 * Scoped CSS for the Notes app.
 *
 * Geometry and colour live in inline styles (which cannot be purged and need no
 * build step); this string carries only what inline styles cannot express —
 * hover and focus.
 *
 * The one exception is the heading accent VALUES: the `color-mix()` expressions
 * that derive the rule and rail colours from `--accent`. They sit here as
 * custom properties on `.mdnb-note` rather than as module constants because the
 * strict i18n gate (`eslint.i18n.strict.config.js`) reads a string literal under
 * an ALL-CAPS declarator as user-visible copy, and a `color-mix()` expression
 * carries enough words and spaces to look like prose to it. Declaring them as
 * CSS keeps them in the module that is exempt precisely because it holds
 * declarations rather than copy, while the geometry that uses them stays inline
 * in `Preview.tsx` as the convention above requires.
 */
export const MDNB_CSS = `
.mdnb-note{
  --mdnb-heading-rule-strong:color-mix(in srgb,var(--accent) 45%,transparent);
  --mdnb-heading-rule-soft:color-mix(in srgb,var(--accent) 30%,transparent);
  --mdnb-heading-rail:color-mix(in srgb,var(--accent) 55%,transparent)}
.mdnb-row:hover{background:var(--bg-hover);color:var(--text)}
.mdnb-row-actions{position:absolute;top:50%;transform:translateY(-50%);right:6px;
  display:flex;align-items:center;gap:2px;padding:3px;border-radius:8px;
  background:var(--card);border:1px solid var(--border);
  box-shadow:0 1px 3px rgba(0,0,0,0.18);
  opacity:0;pointer-events:none;transition:opacity .12s}
.mdnb-row:hover .mdnb-row-actions,
.mdnb-row-actions:has(:focus-visible){opacity:1;pointer-events:auto}
.mdnb-act:hover{background:var(--bg-hover);color:var(--text)}
.mdnb-act-danger:hover{background:var(--danger-subtle);color:var(--danger)}
.mdnb-dlg-cancel:hover{background:var(--bg-hover)}
.mdnb-dlg-danger:hover{filter:brightness(1.1)}
.mdnb-dlg-cancel:focus-visible,.mdnb-dlg-danger:focus-visible{outline:2px solid var(--ring);outline-offset:2px}
.mdnb-blk:hover{background:var(--bg-hover)}
.mdnb-search::placeholder{color:color-mix(in srgb,var(--muted) 50%,transparent)}
.mdnb-search{transition:border-color .2s,box-shadow .2s}
.mdnb-search:focus{outline:none;border-color:var(--ring);
  box-shadow:0 0 0 3px var(--accent-subtle),0 0 20px color-mix(in srgb,var(--accent) 8%,transparent)}
.mdnb-vault-trigger:hover{background:var(--bg-hover)}
.mdnb-vault-trigger:hover span{color:var(--text)}
.mdnb-collapse{color:var(--muted)}
.mdnb-collapse:hover{color:var(--text);background:var(--bg-hover)}
`
