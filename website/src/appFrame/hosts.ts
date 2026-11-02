/**
 * Host claims every app point that names hosts shares: one normalization, one
 * refusal rule, one matcher and one URL policy.
 *
 * Mirrors `apps/app_hosts.py`. A point reads its manifest `hosts` through
 * `readHostClaims` (normalized, refused entries dropped, cut to the owner's grant),
 * finds a link's host with `claimableUrlHost`, matches it with `claimsHost`, and
 * hands the app `withoutQuery(url)` unless its whole-URL grant is approved.
 *
 * @module appFrame/hosts
 */

/** Mirrors `HOST_RE` in `apps/app_hosts.py`: exact host, or a leading dot for the
 *  domain and every subdomain, at least two labels. */
export const HOST_RE = /^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/

/** Mirrors `PUBLIC_SUFFIXES` and `_CC_SECOND_LEVELS` in `apps/app_hosts.py`: a host
 *  claiming one would reach every site under it. */
export const PUBLIC_SUFFIXES: ReadonlySet<string> = new Set([
  'appspot.com', 'azurewebsites.net', 'blogspot.com', 'cloudfront.net', 'firebaseapp.com',
  'github.io', 'gitlab.io', 'herokuapp.com', 'netlify.app', 'ngrok.io', 'pages.dev',
  'vercel.app', 'web.app', 'workers.dev',
])
export const CC_SECOND_LEVELS: ReadonlySet<string> = new Set([
  'ac', 'co', 'com', 'edu', 'gob', 'go', 'gov', 'ltd', 'mil', 'ne', 'net', 'nic', 'or', 'org', 'plc', 'sch',
])

/** Lowercase, no trailing dot, IDNA (punycode) form; a leading dot kept. Null when
 *  it is not a hostname. Mirrors `normalize_host` in `apps/app_hosts.py`. */
export function normalizeHost(raw: string): string | null {
  let s = String(raw).trim().toLowerCase()
  const lead = s.startsWith('.')
  if (lead) s = s.slice(1)
  if (s.endsWith('.')) s = s.slice(0, -1)
  if (!s) return null
  let host: string
  try {
    host = new URL(`https://${s}/`).hostname
  } catch {
    return null
  }
  if (host.endsWith('.')) host = host.slice(0, -1)
  return (lead ? '.' : '') + host
}

/** A public suffix or a bare top-level domain (normalized, no leading dot). */
export function isPublicSuffix(host: string): boolean {
  const labels = host.split('.')
  if (labels.length < 2) return true
  if (PUBLIC_SUFFIXES.has(host)) return true
  return labels.length === 2 && labels[1].length === 2 && CC_SECOND_LEVELS.has(labels[0])
}

/** Whether a normalized host may be claimed. Mirrors `host_refusal`: a hostname,
 *  not an IP literal (a numeric last label), not `localhost` or a name under it,
 *  and not a public suffix or a bare TLD. */
export function isClaimableHost(host: string): boolean {
  if (!HOST_RE.test(host)) return false
  const bare = host.startsWith('.') ? host.slice(1) : host
  const last = bare.slice(bare.lastIndexOf('.') + 1)
  if (/^\d+$/.test(last) || last === 'localhost') return false
  return !isPublicSuffix(bare)
}

export interface HostClaims {
  /** Exact hostnames. */
  exact: ReadonlySet<string>
  /** Domain suffixes, each starting with a dot (`.example.dev`). */
  suffixes: readonly string[]
}

/** Read a manifest `hosts` value: normalized, refused entries dropped, at most
 *  `max`, and, when `granted` is given, only what the owner approved. */
export function readHostClaims(raw: unknown, max: number, granted?: ReadonlySet<string> | null): HostClaims {
  const exact = new Set<string>()
  const suffixes: string[] = []
  for (const h of Array.isArray(raw) ? raw.slice(0, max) : []) {
    if (typeof h !== 'string') continue
    const host = normalizeHost(h)
    if (!host || !isClaimableHost(host)) continue
    if (granted && !granted.has(host)) continue
    if (host.startsWith('.')) suffixes.push(host)
    else exact.add(host)
  }
  return { exact, suffixes }
}

/** The approved entries of a grant list, or null when the row carries none. */
export function grantedSet(list: unknown): Set<string> | null {
  return Array.isArray(list) ? new Set(list.filter((x): x is string => typeof x === 'string')) : null
}

/** The normalized host of an `https:` URL a claim can match, or null for any
 *  other scheme. Exactly the link-preview rule; a point that must also refuse
 *  userinfo or a port checks those itself. */
export function claimableUrlHost(href: string): string | null {
  let u: URL
  try {
    u = new URL(href)
  } catch {
    return null
  }
  if (u.protocol !== 'https:') return null
  // `example.com.` is the same host as `example.com`.
  return u.hostname.toLowerCase().replace(/\.$/, '')
}

/** Whether `claims` covers `host` (already from `claimableUrlHost`). */
export function claimsHost(claims: HostClaims, host: string): boolean {
  if (claims.exact.has(host)) return true
  // `.example.dev` matches `example.dev` and `a.example.dev`.
  return claims.suffixes.some((s) => host === s.slice(1) || host.endsWith(s))
}

/** The URL a point is handed by default: no query and no fragment, which can carry
 *  signed tokens or other secrets. An entry's approved whole-URL grant opts out. */
export function withoutQuery(href: string): string {
  try {
    const u = new URL(href)
    return `${u.origin}${u.pathname}`
  } catch {
    return ''
  }
}
