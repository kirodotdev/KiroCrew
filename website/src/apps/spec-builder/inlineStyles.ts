// CSS injected through <style> tags, kept out of the components so the i18n lint
// does not read stylesheet text as user-visible copy (this module is ignored by path
// in website/eslint.i18n.config.js).

/** Keeps images and code blocks inside the document column. */
export const DOC_CSS =
  '.sb-doc img, .sb-doc svg { max-width: 100%; height: auto } .sb-doc pre { overflow-x: auto }'

/** Row and field hover tints, mixed from the current accent so themes carry through. */
export const PICKER_CSS =
  '.sb-row:hover{background:color-mix(in srgb, var(--accent) 14%, transparent)}'
  + ' .sb-field:hover{border-color:var(--accent)}'

/** Theme accent, and the selection / active tints mixed from it. Translucent so
 *  they read correctly on light AND dark backgrounds. CSS values, not copy --
 *  they live here so the i18n lint does not read a stylesheet as user text. */
export const ACCENT = 'var(--accent)'
export const SEL_BG = 'color-mix(in srgb, var(--accent) 14%, transparent)'
export const SEL_BORDER = 'color-mix(in srgb, var(--accent) 45%, transparent)'
