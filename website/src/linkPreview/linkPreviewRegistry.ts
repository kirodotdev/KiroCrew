/**
 * App-contributed link previews: discovery and matching.
 *
 * Apps declare a provider in `app.json` under `contributes.linkPreviews[]`
 * (`{ id, entry, hosts, textPatterns? }`). The manifest is validated server-side; this
 * module re-checks only what it needs to match safely, because the list it reads
 * may be stale or hand-edited.
 *
 * @module linkPreview/linkPreviewRegistry
 */
import { MAX_TEXT_PATTERNS, type RawTextPattern } from './textPatterns'

/** Mirrors `_HOST_RE` in `apps/manifest_contributes.py`: exact host, or a
 *  leading dot for the domain and every subdomain, at least two labels. */
const HOST_RE = /^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/
/** Mirrors the backend caps. */
const MAX_HOSTS = 64
const ID_RE = /^[a-z][a-z0-9_-]{0,63}$/

function readTextPatterns(raw: unknown): RawTextPattern[] {
  if (!Array.isArray(raw)) return []
  const out: RawTextPattern[] = []
  for (const tp of raw.slice(0, MAX_TEXT_PATTERNS)) {
    if (!tp || typeof tp !== 'object') continue
    const { pattern, flags, id, defaultOff } = tp as Record<string, unknown>
    if (typeof pattern !== 'string') continue
    if (flags !== undefined && typeof flags !== 'string') continue
    if (id !== undefined && typeof id !== 'string') continue
    out.push({
      pattern,
      ...(flags !== undefined ? { flags: flags as string } : {}),
      ...(id !== undefined ? { id: id as string } : {}),
      ...(defaultOff === true ? { defaultOff: true } : {}),
    })
  }
  return out
}

export interface ResolvedLinkPreview {
  /** `${appName}:${id}`. */
  key: string
  appName: string
  id: string
  /** ESM path relative to the app's `ui/` dir. */
  entry: string
  /** Exact hostnames. */
  exact: ReadonlySet<string>
  /** Domain suffixes, each starting with a dot (`.example.dev`). */
  suffixes: readonly string[]
  /** Route base of the app's own backend: `/api/apps/<app>` or `/apps/<app>/api`. */
  routeBase: string
  /** Plain-text patterns, shape-checked only; `textPatterns.ts` compiles and gates them. */
  textPatterns: RawTextPattern[]
}

interface AppLike {
  name?: string
  enabled?: boolean
  manifest?: {
    backend?: { entryPoint?: string }
    contributes?: { linkPreviews?: unknown }
  }
}

/** Resolve every enabled app's provider, in app-list order. */
export function resolveLinkPreviews(apps: readonly AppLike[]): ResolvedLinkPreview[] {
  const out: ResolvedLinkPreview[] = []
  for (const app of apps) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    // `enabled === false` is explicit; older payloads omit the field (AppHost's guard).
    if (app.enabled === false) continue
    const declared = app.manifest?.contributes?.linkPreviews
    if (!Array.isArray(declared)) continue
    // One per app, as the backend caps it.
    const p = declared[0] as Record<string, unknown> | undefined
    if (!p || typeof p !== 'object') continue
    const id = typeof p.id === 'string' ? p.id : ''
    const entry = typeof p.entry === 'string' ? p.entry : ''
    if (!ID_RE.test(id) || !entry || entry.includes('..') || entry.startsWith('/')) continue
    const hosts = Array.isArray(p.hosts) ? p.hosts.slice(0, MAX_HOSTS) : []
    const exact = new Set<string>()
    const suffixes: string[] = []
    for (const h of hosts) {
      if (typeof h !== 'string') continue
      const host = h.toLowerCase()
      if (!HOST_RE.test(host)) continue
      if (host.startsWith('.')) suffixes.push(host)
      else exact.add(host)
    }
    if (!exact.size && !suffixes.length) continue
    const processBacked = Boolean(app.manifest?.backend?.entryPoint)
    out.push({
      key: `${app.name}:${id}`,
      appName: app.name,
      id,
      entry: entry.replace(/^\.?\//, ''),
      exact,
      suffixes,
      routeBase: processBacked ? `/apps/${app.name}/api` : `/api/apps/${app.name}`,
      textPatterns: readTextPatterns(p.textPatterns),
    })
  }
  return out
}

/** The first provider that claims `href`, or null. https only. */
export function providerFor(href: string, providers: readonly ResolvedLinkPreview[]): ResolvedLinkPreview | null {
  if (!providers.length) return null
  let u: URL
  try {
    u = new URL(href)
  } catch {
    return null
  }
  if (u.protocol !== 'https:') return null
  const host = u.hostname.toLowerCase()
  for (const p of providers) {
    if (p.exact.has(host)) return p
    // `.example.dev` matches `example.dev` and `a.example.dev`.
    if (p.suffixes.some((s) => host === s.slice(1) || host.endsWith(s))) return p
  }
  return null
}
