/**
 * Scoped CSS for the Notes app.
 *
 * Geometry and colour live in inline styles (which cannot be purged and need no
 * build step); this string carries only what inline styles cannot express —
 * hover and focus.
 */
export const MDNB_CSS = `
.mdnb-row:hover{background:var(--bg-hover);color:var(--text)}
.mdnb-blk:hover{background:var(--bg-hover)}
.mdnb-search::placeholder{color:color-mix(in srgb,var(--muted) 50%,transparent)}
.mdnb-search{transition:border-color .2s,box-shadow .2s}
.mdnb-search:focus{outline:none;border-color:var(--ring);
  box-shadow:0 0 0 3px var(--accent-subtle),0 0 20px color-mix(in srgb,var(--accent) 8%,transparent)}
.mdnb-vault-trigger:hover{background:var(--bg-hover)}
.mdnb-vault-trigger:hover span{color:var(--text)}
.mdnb-collapse{color:var(--muted)}
.mdnb-collapse:hover{color:var(--text)}
`
