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
import { claimableUrlHost, claimsHost, grantedSet, readHostClaims } from '../appFrame/hosts'
import { MAX_TEXT_PATTERNS, type RawTextPattern } from './textPatterns'

// The host rule lives in appFrame/hosts, shared by every point that names hosts.
export { CC_SECOND_LEVELS, PUBLIC_SUFFIXES, isPublicSuffix, normalizeHost } from '../appFrame/hosts'

/** Mirrors the backend caps. */
const MAX_HOSTS = 64
const ID_RE = /^[a-z][a-z0-9_-]{0,63}$/

/** A text pattern's grant entry. Mirrors `pattern_grant` in `apps/link_preview_grants.py`. */
export function patternGrant(tp: RawTextPattern, index: number): string {
  const flags = [...new Set((tp.flags ?? '').replace(/g/g, ''))].sort().join('')
  return `${tp.id || String(index)}:${flags}:${tp.pattern}`
}

function readTextPatterns(raw: unknown): RawTextPattern[] {
  if (!Array.isArray(raw)) return []
  const out: RawTextPattern[] = []
  raw.slice(0, MAX_TEXT_PATTERNS).forEach((tp, index) => {
    if (!tp || typeof tp !== 'object') return
    const { pattern, flags, id, defaultOff } = tp as Record<string, unknown>
    if (typeof pattern !== 'string') return
    if (flags !== undefined && typeof flags !== 'string') return
    if (id !== undefined && typeof id !== 'string') return
    out.push({
      pattern,
      ...(flags !== undefined ? { flags: flags as string } : {}),
      // An unnamed pattern is named by its declaration index here, before any
      // filter, so dropping an earlier pattern (one waiting for approval) never
      // renames a later one or its off switch.
      id: (id as string | undefined) || String(index),
      ...(defaultOff === true ? { defaultOff: true } : {}),
    })
  })
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
  /** Hand the module the hovered URL whole; by default the query and fragment are dropped. */
  fullUrl: boolean
  /** Which install of the module this is (entry, version, install and update time). A frame
   *  made for another revision runs the old module and is replaced. */
  revision: string
}

interface AppLike {
  name?: string
  enabled?: boolean
  version?: string
  installedAt?: string
  updatedAt?: string
  /** What the owner approved (`apps/link_preview_grants.py`). Absent: nothing to filter. */
  linkPreviewGrant?: { hosts?: unknown; textPatterns?: unknown; fullUrl?: unknown }
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
    // Only what the owner approved: an update that added a host or a pattern
    // waits for them (`linkPreviewGrant`, from the gateway).
    const grant = app.linkPreviewGrant
    const { exact, suffixes } = readHostClaims(p.hosts, MAX_HOSTS, grant ? grantedSet(grant.hosts) : null)
    const grantedPatterns = grant ? grantedSet(grant.textPatterns) : null
    // An unnamed pattern's id is its declaration index. Fix it before the grant
    // filter drops any, so a later pattern keeps its id (and the app's off switch
    // for it) when an earlier one waits for approval.
    const textPatterns = readTextPatterns(p.textPatterns)
      .filter((tp) => !grantedPatterns || grantedPatterns.has(patternGrant(tp, 0)))
    if (!exact.size && !suffixes.length && !textPatterns.length) continue
    const processBacked = Boolean(app.manifest?.backend?.entryPoint)
    out.push({
      key: `${app.name}:${id}`,
      appName: app.name,
      id,
      entry: entry.replace(/^\.?\//, ''),
      exact,
      suffixes,
      routeBase: processBacked ? `/apps/${app.name}/api` : `/api/apps/${app.name}`,
      textPatterns,
      // Whole URLs only once the owner approved them: a query or fragment can
      // carry a signed link's credential.
      fullUrl: p.fullUrl === true && grant?.fullUrl === true,
      // `updatedAt` too: a same-version Sync replaces the files and keeps installedAt.
      revision: JSON.stringify([entry, app.version ?? '', app.installedAt ?? '', app.updatedAt ?? '']),
    })
  }
  return out
}

/** The first provider that claims `href`, or null. https only. */
export function providerFor(href: string, providers: readonly ResolvedLinkPreview[]): ResolvedLinkPreview | null {
  if (!providers.length) return null
  // https only, as before the host rule moved to appFrame/hosts.
  const host = claimableUrlHost(href)
  if (!host) return null
  for (const p of providers) if (claimsHost(p, host)) return p
  return null
}
