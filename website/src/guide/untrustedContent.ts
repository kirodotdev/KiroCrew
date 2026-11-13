/**
 * Installs the guide-marker strip on the shared DOMPurify instance (see
 * `./guideMarkers.ts`): importing this module is enough.
 */
import DOMPurify from 'dompurify'
import { isGuideMarkerAttr } from './guideMarkers'

export { GUIDE_UNTRUSTED_ATTR, guideUntrusted, inUntrustedContent, isGuideMarkerAttr } from './guideMarkers'

let installed = false

/** Drop guide markers from everything DOMPurify sanitizes. Idempotent. */
export function installGuideMarkerStrip(purify: typeof DOMPurify = DOMPurify): void {
  if (installed && purify === DOMPurify) return
  if (purify === DOMPurify) installed = true
  purify.addHook('uponSanitizeAttribute', (_node, data) => {
    if (isGuideMarkerAttr(data.attrName)) data.keepAttr = false
  })
}

installGuideMarkerStrip()

/**
 * Remove every guide marker from the elements under *root* (already sanitized).
 * A pass of its own, beside the hook: it does not depend on how the sanitizer
 * walks namespaced (SVG) attributes.
 */
export function stripGuideMarkers(root: ParentNode): void {
  for (const el of Array.from(root.querySelectorAll('*'))) {
    for (const name of el.getAttributeNames()) if (isGuideMarkerAttr(name)) el.removeAttribute(name)
  }
}
