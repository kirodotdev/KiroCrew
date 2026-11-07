/**
 * What mermaid's sanitizer strips from a diagram's HTML labels. `securityLevel:
 * 'strict'` stops script but keeps an `<img src="https://…">`, which the browser
 * fetches the moment the diagram renders: the zero-click request remoteMedia.tsx
 * keeps out of markdown images. So every tag and attribute that loads a URL by
 * itself is removed here. `style` stays forbidden as a tag, as mermaid's own
 * default does, and as an attribute too, because a `url(…)` in it is a fetch.
 */
export const MERMAID_DOMPURIFY_CONFIG = {
  FORBID_TAGS: ['style', 'img', 'image', 'picture', 'source', 'video', 'audio', 'iframe', 'object', 'embed', 'input'],
  FORBID_ATTR: ['style', 'src', 'srcset', 'poster', 'background'],
}
