import DOMPurify from 'dompurify'

/**
 * What mermaid's sanitizer strips from a diagram's HTML labels. `securityLevel:
 * 'strict'` stops script but keeps an `<img src="https://…">`, which the browser
 * fetches the moment the diagram renders: the zero-click request remoteMedia.tsx
 * keeps out of markdown images. So every tag and attribute that loads a URL by
 * itself is removed here, including the SVG `feImage` and `use` elements, whose
 * `href` the browser fetches without any attribute this list names. `style`
 * stays forbidden as a tag, as mermaid's own default does.
 *
 * The `style` ATTRIBUTE is kept, because mermaid runs its own icon SVG and its
 * KaTeX label wrapper through this same config and colours them only with inline
 * `style`. Any attribute value that can fetch is dropped instead
 * (`valueCanFetch`): `style`, and the SVG presentation attributes (`mask`,
 * `fill`, `clip-path`, `filter`, `marker-*`) that take a CSS `url()` too. A
 * same-document `url(#id)`, which mermaid uses for its markers and filters, stays.
 *
 * shortcut: this config covers HTML labels only. Mermaid's `img:` node shape
 * loads its URL inside `mermaid.render()` and never reaches this sanitizer;
 * gate that shape before render if diagrams must never fetch on their own.
 */
export const MERMAID_DOMPURIFY_CONFIG = {
  FORBID_TAGS: ['style', 'img', 'image', 'feImage', 'use', 'picture', 'source', 'video', 'audio', 'iframe', 'object', 'embed', 'input'],
  FORBID_ATTR: ['src', 'srcset', 'poster', 'background'],
  // Marks a sanitize call as a mermaid label pass for the hook below. DOMPurify
  // hands hooks a CLONE of the config, so identity cannot tell; a key survives it.
  KIROCREW_MERMAID_LABELS: true,
}

/** Whether an attribute value can make the browser fetch a URL.
 *
 *  `url(` and `image-set(` are the CSS forms that load a resource; a `url(` whose
 *  target is a same-document `#fragment` loads nothing and is allowed. A backslash
 *  is refused too: a CSS escape (`\75 rl(`) spells `url(` without those letters,
 *  and mermaid's own values never carry one. Plain string checks, lowercased, so
 *  `URL(` and `-webkit-image-set(` are caught as well. */
export function valueCanFetch(value: string): boolean {
  const css = value.toLowerCase()
  if (css.includes('image-set(') || css.includes('\\')) return true
  for (let at = css.indexOf('url('); at !== -1; at = css.indexOf('url(', at + 4)) {
    let i = at + 4
    while (i < css.length && ' \t\n\r\f"\''.includes(css[i])) i++
    if (css[i] !== '#') return true
  }
  return false
}

// Registered once, on the shared DOMPurify instance mermaid imports, and acting
// only on a call that carries the marker, so every other sanitize call is untouched.
DOMPurify.addHook('uponSanitizeAttribute', (_node, data, config) => {
  if (!(config as Record<string, unknown> | undefined)?.KIROCREW_MERMAID_LABELS) return
  if (valueCanFetch(data.attrValue)) data.keepAttr = false
})
