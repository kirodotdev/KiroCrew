import DOMPurify from 'dompurify'

// Declarations that make the browser draw characters the markup's text does not
// contain, which the backend's text projection of the card therefore never reads.
const GENERATED_TEXT = [
  'content', 'list-style', 'list-style-type', 'quotes', 'hyphenate-character',
  'text-emphasis', 'text-emphasis-style', '-webkit-text-emphasis', '-webkit-text-emphasis-style', 'text-overflow',
]
// Attributes the browser shows as text (a broken image's alt, a hover tooltip,
// a list marker's number) that the backend's tag-stripping projection drops.
const DISPLAYED_ATTRIBUTES = new Set(['alt', 'title', 'start', 'value'])

function removeGeneratedText(style: CSSStyleDeclaration): void {
  for (const name of GENERATED_TEXT) style.removeProperty(name)
}

function inspectCardRule(rule: CSSRule): boolean {
  // A font remaps the glyphs shown for the scanned text, so the scan no
  // longer reads what the card displays.
  if (rule.type === CSSRule.FONT_FACE_RULE) return false
  let inspected = false
  if ('style' in rule) {
    removeGeneratedText((rule as CSSStyleRule).style)
    inspected = true
  }
  if ('cssRules' in rule) {
    const group = rule as CSSGroupingRule
    for (let i = group.cssRules.length - 1; i >= 0; i--) {
      if (!inspectCardRule(group.cssRules[i])) {
        if (rule.type === CSSRule.KEYFRAMES_RULE) {
          (rule as CSSKeyframesRule).deleteRule((group.cssRules[i] as CSSKeyframeRule).keyText)
        } else group.deleteRule(i)
      }
    }
    inspected = true
  }
  // Includes @counter-style and rules this browser cannot safely inspect.
  return inspected
}

function restrictCardStyles(doc: Document): void {
  for (const element of doc.querySelectorAll('style, [style]')) {
    const block = element.localName === 'style'
    try {
      // Constructed sheets never join the host document, and ignore @import.
      // No parsing fallback may attach untrusted CSS to the live host.
      const sheet = new CSSStyleSheet()
      if (block) {
        sheet.replaceSync(element.textContent || '')
        for (let i = sheet.cssRules.length - 1; i >= 0; i--) {
          if (!inspectCardRule(sheet.cssRules[i])) sheet.deleteRule(i)
        }
        const css = Array.from(sheet.cssRules, rule => rule.cssText).join('\n')
        // CSSOM can decode escapes into an HTML raw-text closing delimiter.
        if (/<\/style[\s/>]/i.test(css)) throw new Error('Unsafe CSS serialization')
        element.textContent = css
      }
      if (element.hasAttribute('style')) {
        sheet.replaceSync('') // require the same isolated parser for both paths
        const style = doc.createElement('span').style
        style.cssText = element.getAttribute('style') || ''
        removeGeneratedText(style)
        element.setAttribute('style', style.cssText)
      }
    } catch {
      if (block) element.remove()
      else element.removeAttribute('style')
    }
  }
}

/** Task text may be private. An opaque origin stops host reads, not outbound
 * navigation or script-created DNS lookups. Keep model layout, never model code.
 * Native HTML (details, anchors, CSS and SVG) remains freely authorable; new
 * evidence arrives as a new artifact revision, not an in-frame network fetch. */
export function dashboardDocument(html: string, themeVars: Record<string, string>, mode: 'dark' | 'light', data?: Record<string, string>): string {
  const sanitized = DOMPurify.sanitize(html, {
    WHOLE_DOCUMENT: true,
    ADD_TAGS: ['style'],
    FORBID_TAGS: ['script', 'link', 'meta', 'base', 'iframe', 'frame', 'object', 'embed', 'applet', 'template', 'noscript', 'form', 'input', 'button', 'textarea', 'select', 'animate', 'set'],
  })
  const doc = new DOMParser().parseFromString(sanitized, 'text/html')
  // Only automatic cards pass data (including {}). Saved views keep their CSS.
  const card = data !== undefined
  if (card) restrictCardStyles(doc)
  if (data) for (const element of doc.body.querySelectorAll('[data-dashboard-field]')) {
    if (element.tagName.toLowerCase() === 'style') continue
    const field = element.getAttribute('data-dashboard-field') || ''
    element.textContent = Object.hasOwn(data, field) ? data[field] : ''
  }
  // CSP doesn't govern navigation. Retain only in-document links and remove
  // resource-hint markup before any model bytes reach a live browsing context.
  for (const element of doc.querySelectorAll('*')) {
    // Inspect actual attributes: Chromium's selector matching can miss SVG's
    // namespaced xlink:href. Empty href also navigates (reloads the document).
    for (const attr of Array.from(element.attributes)) {
      if (attr.localName === 'href' && !attr.value.startsWith('#')) element.removeAttributeNode(attr)
      // The markup comes from a model reading an untrusted transcript, and the
      // backend scans it as text. A data: image is bytes the browser decodes
      // for display, which the text scan cannot read the same way
      // (percent-encoding, base64). Both surfaces are layout over text; inline
      // SVG stays authorable, an image load is not.
      if (attr.localName === 'src' || attr.localName === 'srcset') element.removeAttributeNode(attr)
      else if (card && DISPLAYED_ATTRIBUTES.has(attr.localName)) element.removeAttributeNode(attr)
    }
  }
  const csp = doc.createElement('meta')
  csp.httpEquiv = 'Content-Security-Policy'
  csp.content = `default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src ${card ? "'none'" : 'data:'}; connect-src 'none'; form-action 'none'; base-uri 'none';`
  doc.head.prepend(csp)
  const style = doc.createElement('style')
  // readThemeVars already sanitizes the host's computed CSS values.
  style.textContent = `:root{${Object.entries(themeVars).map(([key, value]) => `${key}:${value}`).join(';')};color-scheme:${mode}}body{margin:0;padding:16px;background:var(--bg);color:var(--text);font-family:system-ui,sans-serif}`
  csp.after(style)
  return '<!doctype html>' + doc.documentElement.outerHTML
}
