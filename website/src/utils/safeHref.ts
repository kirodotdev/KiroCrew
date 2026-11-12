/**
 * Return *raw* as an href ONLY if it is a scheme we can hand a user, else `null`.
 *
 * For any URL that reaches the page from a remote source. Parsed with the URL
 * constructor rather than a regex: a prefix check is defeated by
 * `\tjavascript:`, `JaVaScRiPt:`, and percent/entity encodings that the browser
 * still resolves, whereas `new URL().protocol` is the browser's own normalized
 * answer. Everything outside the allowlist — `javascript:`, `data:`, `blob:`,
 * `file:` — is refused, and a value the parser rejects outright is refused too.
 */
export function safeHref(raw: string): string | null {
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null
  } catch {
    return null
  }
}
