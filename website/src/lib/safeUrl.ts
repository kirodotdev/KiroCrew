/**
 * Validate that a URL is a safe HTTP(S) link — reject javascript:, data:, etc.
 * Returns the original URL if valid, null otherwise.
 *
 * Also reject URLs carrying HTTP Basic-auth userinfo
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

/**
 * Gate for embedding a deployed web app as a live iframe preview.
 *
 * Much stricter than safeHttpUrl: only https URLs on a first-party CloudFront
 * distribution domain (`<dist-id>.cloudfront.net`) qualify — that is the only
 * host shape the artifact-deploy contract produces. Everything else (custom
 * domains, http, userinfo, lookalike hosts like `evil-cloudfront.net` or
 * `cloudfront.net.evil.com`) falls back to the non-embedding preview.
 *
 * This mirrors the server CSP (`frame-src ... https://*.cloudfront.net`):
 * the FE gate keeps the UI honest, the CSP enforces it even if a crafted
 * webapp_metadata slips a different URL through.
 */
export function framablePreviewUrl(url: string): string | null {
  try {
    const u = new URL(url)
    if (u.protocol !== 'https:') return null
    if (u.username || u.password) return null
    if (!/^[a-z0-9]+\.cloudfront\.net$/i.test(u.hostname)) return null
    return url
  } catch {
    return null
  }
}
