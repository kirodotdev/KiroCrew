/**
 * The shadow root an SVG artifact is drawn in (components/SvgShadow.tsx), found
 * under *root*: queries on the page do not reach into it, so a test reads the
 * drawn SVG through this.
 */
export function svgShadow(root: ParentNode = document): ShadowRoot | null {
  return Array.from(root.querySelectorAll('[data-guide-untrusted]')).find(el => el.shadowRoot)?.shadowRoot ?? null
}
