/**
 * Pure helpers behind the "share message as card" flow: sensitive-content
 * scanning, social intent URLs, adjacent-question lookup, and the
 * clipboard/download fallbacks. Kept free of React so every piece is unit
 * testable without a DOM.
 */

/** Public repo link baked into the default caption and the card footer. */
export const SHARE_REPO_URL = 'https://github.com/kirodotdev/KiroCrew'

/** X's hard post limit; the caption editor shows "n / 280" against it. */
export const X_POST_LIMIT = 280

/**
 * Excerpt cap for the card body. A share card is a glanceable image, not a
 * transcript: past ~600 chars the text shrinks below phone-readable size.
 */
export const CARD_EXCERPT_LIMIT = 600

export type SensitiveKind = 'aws_key' | 'token' | 'private_key' | 'local_path' | 'internal_url'

/**
 * High-precision patterns only: this is a pre-share nudge, not a DLP gate.
 * Each pattern must be specific enough that a hit is worth interrupting the
 * user for — broad heuristics (entropy, long hex) would cry wolf on ordinary
 * technical prose and teach users to ignore the banner.
 */
const SENSITIVE_PATTERNS: ReadonlyArray<readonly [SensitiveKind, RegExp]> = [
  ['aws_key', /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/],
  ['token', /\b(?:gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b/],
  ['token', /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}/],
  ['private_key', /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ['local_path', /(?:\/(?:home|Users)\/|[A-Za-z]:\\Users\\)[\w.-]+/],
  ['internal_url', /https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})(?::\d+)?/i],
]

/** Scan text about to leave the machine; returns deduped kinds in match order. */
export function scanSensitive(text: string): SensitiveKind[] {
  const found: SensitiveKind[] = []
  for (const [kind, re] of SENSITIVE_PATTERNS) {
    if (!found.includes(kind) && re.test(text)) found.push(kind)
  }
  return found
}

/** Intent URLs carry TEXT only — no platform lets a URL attach an image, so the
 *  modal's copy action is what carries the card and this link carries the words. */
export function buildIntentUrl(platform: 'x' | 'linkedin', text: string): string {
  const t = encodeURIComponent(text)
  return platform === 'x'
    ? `https://x.com/intent/post?text=${t}`
    : `https://www.linkedin.com/feed/?shareActive=true&text=${t}`
}

/** Reduce markdown to card prose. Assistant replies arrive as markdown, and a
 *  raw `**bold**`, ` ```fence``` ` or `![img](path)` reads as line noise on a
 *  social image. Images and code fences cannot render on the card at all, so
 *  they collapse to nothing and to their bare content respectively; links keep
 *  their text and drop the URL. */
function stripMarkdown(text: string): string {
  return text
    .replace(/```[^\n]*\n([\s\S]*?)```/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '· ')
    .replace(/^\s*>\s?/gm, '')
    .replace(/(\*\*|__)(.+?)\1/g, '$2')
    .replace(/(^|[^*\w])[*_]([^*_\n]+)[*_](?=[^*\w]|$)/g, '$1$2')
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/^\s*\|.*\|\s*$/gm, m => m.replace(/\s*\|\s*/g, '  ').trim())
    .replace(/^\s*[-:| ]+\s*$/gm, '')
}

/** Trim a message down to card-sized prose: strip markdown syntax, collapse
 *  blank runs, cap length. */
export function clampExcerpt(text: string, limit: number = CARD_EXCERPT_LIMIT): string {
  const cleaned = stripMarkdown(text).trim().replace(/\n{3,}/g, '\n\n')
  if (cleaned.length <= limit) return cleaned
  let slice = cleaned.slice(0, limit)
  // A UTF-16 cut can land inside a surrogate pair (emoji and other astral
  // code points); a dangling high surrogate renders as a corrupt glyph in
  // the exported card, so drop it and cut on a whole code point.
  const last = slice.charCodeAt(slice.length - 1)
  if (last >= 0xd800 && last <= 0xdbff) slice = slice.slice(0, -1)
  // Cut on a whitespace boundary where one exists near the cap so the ellipsis
  // never splits a word (CJK text has no spaces and simply cuts at the cap).
  const lastBreak = slice.lastIndexOf(' ')
  return (lastBreak > limit - 80 ? slice.slice(0, lastBreak) : slice).trimEnd() + '…'
}

/** The user question a reply answers: nearest preceding non-empty user row. */
export function prevUserTextFor(
  messages: ReadonlyArray<{ role: string; content: string }>,
  index: number,
): string | undefined {
  for (let i = Math.min(index, messages.length) - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'user' && m.content.trim()) return m.content
  }
  return undefined
}

/**
 * Copy the card + caption in one clipboard item so a single paste into a post
 * composer attaches the image (composers prefer the image representation).
 * Firefox rejects ClipboardItem writes and Safari is picky about multi-type
 * items, so retry image-only before reporting failure — the caller falls back
 * to a download, never a dead button.
 */
export async function copyImageWithText(blob: Blob, text: string): Promise<boolean> {
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) return false
  try {
    await navigator.clipboard.write([
      new ClipboardItem({ 'image/png': blob, 'text/plain': new Blob([text], { type: 'text/plain' }) }),
    ])
    return true
  } catch {
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
      return true
    } catch {
      return false
    }
  }
}

/** Object-URL download; revoke deferred a tick so the click can consume it. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
