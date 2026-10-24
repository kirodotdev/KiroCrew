/**
 * What a pasted or chosen cookie export would sign the agent in to, read in the
 * browser BEFORE the user presses Import.
 *
 * The server stays the only authority: it parses the export again, enforces the
 * size and count limits and refuses anything malformed. This reader exists only
 * so the dialog can name the sites first, and it answers `null` for anything it
 * cannot read cleanly, so the dialog shows no preview rather than a wrong one.
 * It accepts the same three shapes the server does (a Playwright storageState
 * object, a bare JSON array of cookie objects, a Netscape cookies.txt body),
 * drops cookies whose expiry is already past, and reports domains the way the
 * status chip does: distinct, sorted, leading dot removed. It never reads or
 * returns a cookie value.
 */

export interface CookieExportPreview {
  cookieCount: number
  domains: string[]
}

/** Mirrors the server's `MAX_IMPORT_BYTES`: past it the import is refused, so
 * there is nothing to preview. Measured in UTF-16 units, a lower bound on bytes. */
const MAX_PREVIEW_CHARS = 2 * 1024 * 1024
/** Mirrors the server's `MAX_COOKIES`. */
const MAX_PREVIEW_COOKIES = 5000

function expiryOf(raw: unknown): number {
  if (typeof raw === 'number') return Number.isFinite(raw) && raw > 0 ? raw : -1
  if (typeof raw === 'string' && raw.trim() !== '') {
    const n = Number(raw)
    return Number.isFinite(n) && n > 0 ? n : -1
  }
  return -1
}

function fromJson(text: string): Array<{ domain: string; expires: number }> | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  let list: unknown
  if (Array.isArray(parsed)) list = parsed
  else if (parsed && typeof parsed === 'object') list = (parsed as { cookies?: unknown }).cookies
  if (!Array.isArray(list)) return null
  const out: Array<{ domain: string; expires: number }> = []
  for (const item of list) {
    if (!item || typeof item !== 'object') return null
    const c = item as Record<string, unknown>
    if (typeof c.name !== 'string' || c.name === '') return null
    if (typeof c.domain !== 'string' || c.domain === '') return null
    out.push({ domain: c.domain, expires: expiryOf('expirationDate' in c ? c.expirationDate : c.expires) })
  }
  return out
}

function fromNetscape(text: string): Array<{ domain: string; expires: number }> {
  const out: Array<{ domain: string; expires: number }> = []
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue
    let row = line.replace(/^ +/, '')
    if (row.startsWith('#HttpOnly_')) row = row.slice('#HttpOnly_'.length)
    else if (row.startsWith('#')) continue
    const parts = row.split('\t')
    if (parts.length < 7 || parts[0] === '' || parts[5] === '') continue
    out.push({ domain: parts[0], expires: expiryOf(parts[4]) })
  }
  return out
}

/** The cookie count and distinct domains an export would import, or `null`
 * when the text is blank, too large, or not a shape the import accepts. */
export function previewCookieExport(text: string, nowSeconds: number = Date.now() / 1000): CookieExportPreview | null {
  if (!text.trim() || text.length > MAX_PREVIEW_CHARS) return null
  const first = text.trimStart()[0]
  const raw = first === '{' || first === '[' ? fromJson(text) : fromNetscape(text)
  // Past the server's `MAX_COOKIES` the import is refused whole.
  if (!raw || raw.length > MAX_PREVIEW_COOKIES) return null
  const live = raw.filter((c) => !(c.expires > 0 && c.expires < nowSeconds))
  if (live.length === 0) return null
  const domains = [...new Set(live.map((c) => (c.domain.startsWith('.') ? c.domain.slice(1) : c.domain)))].sort()
  return { cookieCount: live.length, domains }
}
