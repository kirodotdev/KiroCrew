/**
 * Content an agent or a file controls, drawn in the main document (rendered
 * markdown, an SVG artifact): never a guide target.
 *
 * A guide finds its controls by DOM markers (`data-ui-location`,
 * `data-guide-pick`, `data-guide-pick-of`, ...) and by accessible name. Both
 * are plain strings, so markup inside such content could pose as a product
 * control ("Uninstall" on the Command Bar card). Two layers keep it out:
 *
 * - the renderer's container carries `data-guide-untrusted`, and nothing
 *   inside one is ever displayed to the guide (`isDisplayed`), so neither a
 *   marker nor a name inside it resolves;
 * - sanitized SVG and markdown drop every `data-ui-*` / `data-guide-*` /
 *   `data-setting-*` attribute (`isGuideMarkerAttr`), so the markers never
 *   reach the DOM.
 */

export const GUIDE_UNTRUSTED_ATTR = 'data-guide-untrusted'

/** Spread on a renderer's container: `<div {...guideUntrusted}>`. */
export const guideUntrusted = { [GUIDE_UNTRUSTED_ATTR]: '' } as const

/** Whether *el* sits inside content no guide may point at. */
export function inUntrustedContent(el: Element): boolean {
  return el.closest(`[${GUIDE_UNTRUSTED_ATTR}]`) !== null
}

/**
 * Whether an attribute name is one of the guide's own markers, whatever its
 * casing or dashing (the HTML parser lowercases names and hast camelCases
 * `data-*`, so `data-ui-location` may arrive as `dataUiLocation`).
 */
export function isGuideMarkerAttr(name: string): boolean {
  const k = name.toLowerCase().replace(/-/g, '')
  return k.startsWith('dataui') || k.startsWith('dataguide') || k.startsWith('datasetting')
}
