// Positive-allowlist CSS value sanitizer used by the theme system and by
// WidgetFrame when it serializes parent theme vars into a sandboxed iframe.
//
// Only permits characters that appear in legitimate color / shadow / length
// values: hex digits, letters, digits, parentheses (for rgb/hsl/oklch/calc),
// commas, dots, hyphens, spaces, percent signs, and forward-slash (modern
// color syntax). Everything else — semicolons, braces, backslashes, angle
// brackets, quotes, at-signs, colons, etc. — is rejected outright, which
// blocks semicolon-injection, brace-escape, Unicode-escape, @-rule, and
// HTML-escape vectors in a single check.
//
// A small function denylist catches dangerous CSS functions (url(),
// expression(), image(), image-set(), paint(), element()) whose individual
// characters are harmless but whose effect — loading external resources,
// invoking legacy IE XSS, or referencing parent DOM / CSS Houdini worklets —
// is not.
//
// Single source of truth: keeping one sanitizer means any future loosening or
// tightening applies to every iframe surface, not just the one someone
// remembered to update.

export const CSS_VALUE_ALLOWED_RE = /^[a-zA-Z0-9#(),.\- %/]+$/
export const CSS_DANGEROUS_FUNC_RE =
  /url\s*\(|expression\s*\(|image\s*\(|image-set\s*\(|paint\s*\(|element\s*\(/i
export const CSS_VALUE_MAX_LEN = 200

/**
 * Sanitize a CSS value (e.g. a color, length, calc() expression). Returns
 * the trimmed input if it passes the allowlist + function denylist + length
 * cap; returns `''` otherwise.
 *
 * NOT safe for HTML, URL, or JavaScript contexts. The allowlist permits
 * characters like `/`, `(`, `)`, and spaces that are fine inside `rgb(0, 0, 0)`
 * or `calc(100% / 2)` but dangerous in an HTML attribute or URL without
 * context-appropriate encoding. Use a context-specific sanitizer there.
 *
 * @param val - candidate CSS value string (max 200 chars)
 * @returns trimmed sanitized value, or `''` if rejected
 */
export function sanitizeCssValue(val: unknown): string {
  if (typeof val !== 'string' || val.length > CSS_VALUE_MAX_LEN) return ''
  const trimmed = val.trim()
  if (!trimmed) return ''
  if (!CSS_VALUE_ALLOWED_RE.test(trimmed)) return ''
  if (CSS_DANGEROUS_FUNC_RE.test(trimmed)) return ''
  return trimmed
}
