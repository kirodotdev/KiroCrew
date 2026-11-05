/**
 * App-contributed link opening (`contributes.linkOpen`): discovery, matching and
 * the remembered choice.
 *
 * An app declares `{ id, entry, hosts, fullUrl? }`. The manifest is validated
 * server-side; this module re-checks only what it needs to match safely, and keeps
 * only hosts the owner approved (`linkOpenGrant`, from the gateway).
 *
 * "Always" is remembered per entry and per host: the answer the user gave for
 * `tickets.example.com` in one app says nothing about any other host that app
 * claims, nor about another app claiming the same host.
 *
 * @module linkOpen/linkOpenRegistry
 */
import { appDisplayName } from '../components/appstore/appManifest'
import { POINT_ID_RE } from '../appFrame/sandboxFrame'
// The host rule every host-claiming point shares (`apps/app_hosts.py`).
import {
  claimableUrlHost as sharedUrlHost,
  claimsHost,
  grantedSet,
  readHostClaims,
  type HostClaims,
} from '../appFrame/hosts'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** Mirrors `_MAX_LINK_OPEN_HOSTS` in `apps/manifest_contributes.py`. */
const MAX_HOSTS = 64

/** The host of a link this point may claim, or null. The shared rule takes
 *  `https:` only; this point also refuses a user name or password and an explicit
 *  port, because the app is handed the link: neither belongs to the site the
 *  owner approved, and a user name can carry a credential. */
export function claimableUrlHost(href: string): string | null {
  let u: URL
  try {
    u = new URL(href)
  } catch {
    return null
  }
  if (u.username || u.password || u.port) return null
  return sharedUrlHost(href)
}

export interface ResolvedLinkOpener extends HostClaims {
  /** `${appName}:${id}`. */
  key: string
  appName: string
  /** The name the user knows the app by. */
  label: string
  id: string
  /** ESM path relative to the app's `ui/` dir. */
  entry: string
  /** Hand the module the clicked URL whole; by default the query and fragment are dropped. */
  fullUrl: boolean
}

interface AppLike {
  name?: string
  displayName?: string
  origin?: string
  enabled?: boolean
  /** What the owner approved (`apps/point_grants.py`). Absent: nothing to filter. */
  linkOpenGrant?: { hosts?: unknown; fullUrl?: unknown }
  manifest?: { displayName?: string; contributes?: { linkOpen?: unknown } }
}

/** Resolve every enabled app's link-open entry, in app-list order. */
export function resolveLinkOpeners(apps: readonly AppLike[]): ResolvedLinkOpener[] {
  const out: ResolvedLinkOpener[] = []
  for (const app of apps) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    if (app.enabled === false) continue
    const declared = app.manifest?.contributes?.linkOpen
    if (!Array.isArray(declared)) continue
    // One per app, as the backend caps it.
    const p = declared[0] as Record<string, unknown> | undefined
    if (!p || typeof p !== 'object') continue
    const id = typeof p.id === 'string' ? p.id : ''
    const entry = typeof p.entry === 'string' ? p.entry : ''
    if (!POINT_ID_RE.test(id) || !entry || entry.includes('..') || entry.startsWith('/')) continue
    // A row without a grant is an app list from before grants were reported: it
    // gets nothing, because a click hands the app a URL.
    const grant = app.linkOpenGrant
    if (!grant) continue
    const { exact, suffixes } = readHostClaims(p.hosts, MAX_HOSTS, grantedSet(grant.hosts) ?? new Set())
    if (!exact.size && !suffixes.length) continue
    out.push({
      key: `${app.name}:${id}`,
      appName: app.name,
      label: appDisplayName({ ...app, displayName: app.displayName || app.manifest?.displayName }) || app.name,
      id,
      entry: entry.replace(/^\.?\//, ''),
      exact,
      suffixes,
      fullUrl: p.fullUrl === true && grant.fullUrl === true,
    })
  }
  return out
}

/** The first entry that claims `href`, with the link's host, or null. */
export function openerFor(
  href: string,
  openers: readonly ResolvedLinkOpener[],
): { opener: ResolvedLinkOpener; host: string } | null {
  if (!openers.length) return null
  const host = claimableUrlHost(href)
  if (!host) return null
  const opener = openers.find((o) => claimsHost(o, host))
  return opener ? { opener, host } : null
}

/** What "Always" remembered for an entry and a host. */
export type LinkOpenChoice = 'app' | 'browser'

const STORE_KEY = 'kirocrew:link-open-always'
const MAX_REMEMBERED = 200

function readStore(): Record<string, LinkOpenChoice> {
  try {
    const raw = JSON.parse(safeGetItem(STORE_KEY) || '{}') as unknown
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, LinkOpenChoice> = {}
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      if (v === 'app' || v === 'browser') out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

const storeKey = (openerKey: string, host: string) => `${openerKey}|${host}`

/** The remembered choice for this entry and this one host, or null. */
export function rememberedChoice(openerKey: string, host: string): LinkOpenChoice | null {
  return readStore()[storeKey(openerKey, host)] ?? null
}

/** Remember a choice for this entry and this one host only. */
export function rememberChoice(openerKey: string, host: string, choice: LinkOpenChoice): void {
  const store = readStore()
  const k = storeKey(openerKey, host)
  delete store[k]
  store[k] = choice
  const keys = Object.keys(store)
  // Oldest first, as insertion order: a bounded store drops the oldest answer.
  for (const old of keys.slice(0, Math.max(0, keys.length - MAX_REMEMBERED))) delete store[old]
  safeSetItem(STORE_KEY, JSON.stringify(store))
}

/** Every remembered choice of one app, as `{ host, choice }`. */
export function rememberedChoices(appName: string): { key: string; host: string; choice: LinkOpenChoice }[] {
  const out: { key: string; host: string; choice: LinkOpenChoice }[] = []
  for (const [k, choice] of Object.entries(readStore())) {
    const at = k.lastIndexOf('|')
    const key = k.slice(0, at)
    if (at > 0 && key.slice(0, key.indexOf(':')) === appName) out.push({ key, host: k.slice(at + 1), choice })
  }
  return out
}

/** Forget one remembered choice. */
export function forgetChoice(openerKey: string, host: string): void {
  const store = readStore()
  delete store[storeKey(openerKey, host)]
  safeSetItem(STORE_KEY, JSON.stringify(store))
}
