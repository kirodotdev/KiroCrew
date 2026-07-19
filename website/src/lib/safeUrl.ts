/**
 * Validate that a URL is a safe HTTP(S) link — reject javascript:, data:, etc.
 * Returns the original URL if valid, null otherwise.
 *
 * R21 F2: also reject URLs carrying HTTP Basic-auth userinfo
 * (https://user:pass@host) — an LLM-controlled URL could otherwise smuggle
 * credentials that get transmitted to the host when the link is opened.
 */
export function safeHttpUrl(url: string): string | null {
  try {
    const u = new URL(url)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    if (u.username || u.password) return null
    return url
  } catch {
    return null
  }
}
