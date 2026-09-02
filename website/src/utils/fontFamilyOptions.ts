import type { FontFamily } from '../hooks/useZoom'

/**
 * Bundled-font constants and Font Family picker rows.
 *
 * This module is the SINGLE i18n-exempt home for font-name literals shipped
 * with the dashboard. The exempt scope in `eslint.i18n.config.js` covers only
 * this file — the useZoom / useTerminalFont hooks import from here rather than
 * carry their own string literals, so the exemption stays tight. Same pattern
 * as `src/utils/monoFontCandidates.ts` for terminal-font candidates.
 *
 * `label` on FONT_FAMILY_OPTIONS carries hardcoded proper nouns (built-in
 * family stack names 'Sans' / 'Mono' / 'System' that CSS looks up by exact
 * value); `labelKey` carries an i18n catalog key resolved at the render site.
 *
 * OpenDyslexic is routed through the catalog (labelKey) so the render-time
 * i18n gate sees the label as a resolved translation rather than a Latin
 * literal appearing in the en-XA pseudolocale render. The catalog value in
 * every locale is 'OpenDyslexic' — the font's own name is a proper noun and
 * does not translate — but going through the catalog is what tells the gate
 * it is intentional copy.
 */
export interface FontFamilyOption {
  readonly value: FontFamily
  readonly label?: string
  readonly labelKey?: string
}

export const FONT_FAMILY_OPTIONS: FontFamilyOption[] = [
  { value: 'sans', label: 'Sans' },
  { value: 'mono', label: 'Mono' },
  { value: 'system', label: 'System' },
  { value: 'opendyslexic', labelKey: 'pages.settings.displayPanel.font_family_option_opendyslexic' },
]

/**
 * CSS font-family stack strings applied to `--font-body` / `--mono` when the
 * user selects OpenDyslexic. Lifted out of useZoom's map/table shapes so the
 * ESLint i18n exemption only needs to cover this one file rather than the
 * whole useZoom hook (which contains legitimate user copy patterns). The
 * strings are CSS values — they hunt through @font-face names and generic
 * fallbacks by identity, so translating them would break the lookup.
 */
export const OPENDYSLEXIC_BODY_STACK =
  "'OpenDyslexic',var(--script-fallbacks),sans-serif"
export const OPENDYSLEXIC_MONO_STACK =
  "'OpenDyslexicMono',ui-monospace,SFMono-Regular,monospace"

/**
 * Bundled monospace family name — matched by value against the @font-face
 * declaration in index.css and offered as a preselected row in the terminal
 * font picker. A single constant rather than a container: today there is one
 * bundled mono face, and the terminal picker has one consumer of the row.
 * A future bundled mono face becomes a second constant + a second inlined row.
 */
export const OPENDYSLEXIC_MONO_FAMILY_NAME = 'OpenDyslexicMono'
