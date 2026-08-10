/**
 * Link-unfurl metadata client.
 *
 * `GET /api/link-meta?url=…` resolves an http(s) URL the model emitted into
 * favicon + title + description so the transcript can render a chip or a card
 * instead of a bare URL. The feature is opt-in (`cfg.dashboard.link_previews`,
 * default OFF) and every call site passes that flag down as `enabled` — this
 * module NEVER issues a request when it is false, so a user who left the
 * feature off cannot be made to fetch a link by anything the model writes.
 *
 * Caching lives here, at module level, rather than in React Query: a paragraph
 * routinely contains the same URL twice, and a streaming transcript re-renders
 * the same block dozens of times. The cache plus per-URL inflight dedup makes
 * N renders of one URL cost exactly one request.
 */
import { useEffect, useState } from 'react'
import { safeHttpUrl } from './safeUrl'

export interface LinkMeta {
  url: string
  title: string
  description: string
  siteName: string
  domain: string
  icon: string
  /** The site's `prefers-color-scheme: dark` icon, or `''` when it ships one
   *  icon for every surface. The backend sends both because the choice is the
   *  client's: the theme switches at runtime, while a cached payload does not. */
  iconDark: string
  fetchedAt: number
}

/** `null` = known-unavailable; the caller renders a plain anchor. */
type CacheEntry = LinkMeta | null

/** Bound the cache so a long transcript cannot grow it without limit. Mirrors
 * the backend's own 500-entry ceiling; eviction is insertion-order (Map keeps
 * it), which is close enough to LRU for a per-tab render cache. */
const MAX_ENTRIES = 500

/** How long a FAILURE stays cached, matching the backend's negative-cache TTL.
 *
 * Successes are kept for the tab's lifetime — a page title does not change often
 * enough to be worth re-fetching, and the backend has its own 6 h TTL behind us.
 * A failure is different: offline, an aborted navigation, or a site that was
 * briefly down would otherwise pin `null` for that URL until reload, so the link
 * could never preview again even after the backend's own negative entry expired
 * and the site recovered. Expiring failures on the same 10 min clock keeps the
 * two layers from disagreeing about whether a retry is allowed. */
const FAILURE_TTL_MS = 10 * 60 * 1000

const cache = new Map<string, CacheEntry>()
/** When a cached entry is `null`, the moment it may be retried. */
const failedAt = new Map<string, number>()
const inflight = new Map<string, Promise<CacheEntry>>()

/** True when `url` has a usable cached answer — a success, or a failure whose
 *  retry window has not opened yet. Drops an expired failure as it goes, so the
 *  next render requests it again. */
function isCached(url: string): boolean {
  if (!cache.has(url)) return false
  if (cache.get(url) !== null) return true
  const since = failedAt.get(url) ?? 0
  if (Date.now() - since < FAILURE_TTL_MS) return true
  cache.delete(url)
  failedAt.delete(url)
  return false
}

/** Snake_case wire shape. Every field optional: an older or partially-failed
 * backend must degrade to "unavailable", never throw inside a render path. */
interface LinkMetaWire {
  url?: unknown
  title?: unknown
  description?: unknown
  site_name?: unknown
  domain?: unknown
  icon?: unknown
  icon_dark?: unknown
  fetched_at?: unknown
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

/**
 * Only a `data:` image URI is accepted for the favicon.
 *
 * The contract has the backend inline the icon bytes precisely so there is no
 * second `?url=`-driven asset endpoint to abuse as an open fetch proxy. If a
 * remote URL ever reached us here we would re-introduce that hole from the
 * client side — an `<img src>` pointing at an attacker-chosen host is a
 * tracking beacon at best. `svg+xml` is rejected for the same reason the
 * backend rejects it: active content in an image slot.
 */
function safeIcon(icon: string): string {
  if (!/^data:image\/(png|jpeg|gif|webp|x-icon|vnd\.microsoft\.icon);/i.test(icon)) return ''
  return icon
}

/**
 * Resolve one URL, or `null` when no preview can be shown.
 *
 * Every non-2xx body carries a machine-readable `code` (`invalid_url`,
 * `blocked_url`, `link_previews_disabled`, `fetch_failed`) rather than English
 * prose. All four mean the same thing to this layer — there is nothing to
 * unfurl, render the anchor as-is — so the code is deliberately not surfaced:
 * an unfurl that quietly stays a link is correct behaviour, not an error worth
 * an error message. That also keeps us out of the `friendlyErrText` path, whose
 * job is to show a human a failure they asked for.
 */
async function requestLinkMeta(url: string): Promise<CacheEntry> {
  // X-Session-Key so the server-side ephemeral gate always runs (see client.ts).
  const r = await fetch(`/api/link-meta?url=${encodeURIComponent(url)}`, {
    headers: { 'X-Session-Key': 'dashboard:ui' },
  })
  if (!r.ok) return null
  const d = (await r.json()) as LinkMetaWire
  const title = str(d.title).trim()
  const domain = str(d.domain)
  // A preview with neither a title nor a domain has no accessible name to
  // offer, and a chip whose label is empty is an invisible link. Fall back to
  // the plain anchor instead.
  if (!title && !domain) return null
  return {
    url: str(d.url) || url,
    title,
    description: str(d.description).trim(),
    siteName: str(d.site_name),
    domain,
    icon: safeIcon(str(d.icon)),
    // Held to the same data:-only rule as `icon`: a second icon field is a
    // second `<img src>`, so it is exactly as attractive a place to smuggle a
    // remote URL or active content.
    iconDark: safeIcon(str(d.icon_dark)),
    fetchedAt: typeof d.fetched_at === 'number' ? d.fetched_at : 0,
  }
}

/** Fetch once per URL: concurrent callers share one promise, later callers read
 * the cache. A rejected request (offline, aborted navigation) caches `null` so
 * a dead link is not retried on every re-render of a streaming block. */
function load(url: string): Promise<CacheEntry> {
  const pending = inflight.get(url)
  if (pending) return pending
  const p = requestLinkMeta(url)
    .catch(() => null)
    .then((v) => {
      if (cache.size >= MAX_ENTRIES) {
        const oldest = cache.keys().next()
        if (!oldest.done) cache.delete(oldest.value)
      }
      cache.set(url, v)
      if (v === null) failedAt.set(url, Date.now())
      else failedAt.delete(url)
      inflight.delete(url)
      return v
    })
  inflight.set(url, p)
  return p
}

/** Test-only cache reset, mirroring `__resetRefreshOnceForTests` in api/. */
export function __resetLinkMetaForTests(): void {
  cache.clear()
  failedAt.clear()
  inflight.clear()
}

/**
 * `undefined` = still loading / not requested. `null` = known-unavailable.
 *
 * The cache is read synchronously on every render, so a URL another component
 * already resolved renders its chip on the first paint with no request and no
 * loading flash.
 */
export function useLinkMeta(
  url: string | undefined,
  enabled: boolean,
): LinkMeta | null | undefined {
  // Non-http(s) URLs (artifact:, vscode:, javascript:) are never unfurled, and
  // a URL still being streamed in is not requested at all — `enabled` carries
  // the caller's `!live` for exactly that reason.
  const key = enabled && url ? safeHttpUrl(url) : null
  const [, bump] = useState(0)

  useEffect(() => {
    if (!key || isCached(key)) return
    let alive = true
    void load(key).then(() => {
      // Unmount safety: the shared inflight promise belongs to the cache, not
      // to this component, so it is deliberately NOT aborted — another mounted
      // consumer of the same URL is very likely still waiting on it. What must
      // not happen is a resolved fetch re-rendering a dead component, and this
      // flag is what prevents it.
      if (alive) bump((n) => n + 1)
    })
    return () => {
      alive = false
    }
  }, [key])

  if (!key) return undefined
  return cache.has(key) ? cache.get(key) : undefined
}
