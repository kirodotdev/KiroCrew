// Learned natural dimensions for image artifacts, so an image card reserves
// its final contain-fit box BEFORE the bytes arrive.
//
// The save-time header sniff records width/height into the artifact's image
// metadata, and ImageThumb passes those straight through as <img> attributes.
// Artifacts saved before the sniff existed have no dimensions, so their cards
// mount ~16px tall and grow by up to ~280px when the lazy load lands — inside
// the virtualized gallery that late growth shoves everything below it
// mid-scroll, on EVERY pass: the virtualizer's persisted row-height cache
// sizes placeholders and spacers, but a remounted card's own <img> box starts
// empty again regardless.
//
// This cache closes that gap client-side: the first successful load records
// the image's natural size under its slug, and every later mount reserves from
// it. The first-ever view of a legacy image still grows once (nothing knows
// its size yet); every pass after that is stable. Entries are keyed by slug —
// a re-uploaded image (new bytes, same slug) overwrites on its next load, so a
// stale entry self-corrects after one render.
import { safeSetItem } from './safeStorage'

// localStorage key — a storage identifier, never rendered.
const CACHE_KEY = 'mc-image-dims'
// Entries retained across a persist. Bounds the serialized blob.
const MAX_ENTRIES = 300
// Bursts of loads (a scroll mounting several image cards) coalesce into at
// most one synchronous localStorage write per window.
const PERSIST_DEBOUNCE_MS = 1000

export interface ImageDims {
  w: number
  h: number
}

// The one endpoint whose query parameters this cache understands. Scoping the
// allowlist to it matters: `path` and `v` are OUR parameter names, so on a
// third-party URL they carry no meaning to us and could carry anything —
// an external image proxy taking `?path=<signed blob>` would otherwise have its
// credential preserved by the very allowlist meant to strip credentials. Off
// this endpoint the whole query goes.
const FIRST_PARTY_IMAGE_ENDPOINT = '/api/file-raw'
// Query parameters allowed to survive into a persisted cache key, and only on
// the endpoint above. Everything else is dropped, because this key is written to
// localStorage and outlives the session that produced it.
//
// The chat transcript keys entries by an image's resolved URL, and a URL is a
// place credentials live: a message body can carry a presigned link whose
// signature (`X-Amz-Signature`, `sig`, `token`, …) sits in the query string, so
// persisting the URL verbatim would leave that credential on disk after the
// session was deleted. An allowlist rather than a denylist — a new provider's
// parameter name must not silently become persistable.
//
// `path` identifies a first-party image and is the only thing that
// distinguishes one local image from another; `v` is the rewrite counter (see
// ImageVersionCtx) and must stay, or a rewritten file reserves its
// predecessor's box — the exact shift this cache exists to prevent. Neither is
// a secret. A slug key (the artifacts gallery's caller) has no query string at
// all and passes through untouched.
const KEY_PARAM_ALLOWLIST = ['path', 'v']

/** The cache key for `ref` — a slug, or a URL stripped of every part that
 *  could carry a credential. Applied inside both accessors so no caller can
 *  persist a raw URL by forgetting to sanitize it. */
export function cacheKeyFor(ref: string): string {
  const hashIdx = ref.indexOf('#')
  const withoutHash = hashIdx < 0 ? ref : ref.slice(0, hashIdx)
  const qIdx = withoutHash.indexOf('?')
  if (qIdx < 0) return withoutHash
  const base = withoutHash.slice(0, qIdx)
  if (base !== FIRST_PARTY_IMAGE_ENDPOINT) return base
  // Parse rather than regex: `?a=1&path=x` and `?path=x` must yield the same
  // key for the same image regardless of parameter order.
  const params = new URLSearchParams(withoutHash.slice(qIdx + 1))
  const kept = KEY_PARAM_ALLOWLIST
    .filter((name) => params.get(name) !== null)
    .map((name) => `${name}=${encodeURIComponent(params.get(name) as string)}`)
  return kept.length === 0 ? base : `${base}?${kept.join('&')}`
}

// Which keys may be written to disk. This is the load-bearing security rule of
// the module, and it is a property of the key's ORIGIN, not of its contents.
//
// Sanitizing an arbitrary URL is unwinnable: a credential can sit in the query
// (`?X-Amz-Signature=…`), in the fragment, in a path segment
// (`/t/<bearer>/img.png`), or in userinfo (`https://user:pass@host/…`), and
// nothing in the string says which parts are secret. So the persisted set is
// restricted by provenance instead: only a reference the APP ITSELF constructed
// is eligible, which means a relative one — a bare slug (the artifacts gallery)
// or the `/api/file-raw?path=…` URL this module's own caller builds.
//
// Anything absolute came from message content, which is untrusted, so it stays
// MEMORY-ONLY: an external image still reserves its box for the rest of the
// session, it just does not survive a reload. That costs one first-load shift
// per session for externally-hosted images and keeps every attachment — the
// overwhelmingly common case, and the one this PR was reported against —
// exactly as stable as before.
//
// Applied on hydrate as well as on write, so an entry persisted by an earlier
// build is dropped on read rather than trusted, and the next write replaces the
// stored blob with the clean set.
function isPersistable(key: string): boolean {
  // Any scheme (http, https, data, blob, file, …) or a protocol-relative URL.
  return !/^[a-z][a-z0-9+.-]*:/i.test(key) && !key.startsWith('//')
}

const cache: Map<string, ImageDims> = (() => {
  try {
    const stored = localStorage.getItem(CACHE_KEY)
    if (!stored) return new Map<string, ImageDims>()
    const entries = JSON.parse(stored) as [string, ImageDims][]
    const clean = entries.filter(([k]) => isPersistable(k))
    if (clean.length !== entries.length) {
      // A build before this rule persisted raw URLs, so the stored blob may
      // hold a credential right now. Rewrite it on READ rather than waiting
      // for the next image to load — a session that only scrolls would
      // otherwise leave the secret on disk indefinitely.
      safeSetItem(CACHE_KEY, JSON.stringify(clean))
    }
    return new Map<string, ImageDims>(clean)
  } catch {
    return new Map<string, ImageDims>()
  }
})()

let persistTimer: ReturnType<typeof setTimeout> | null = null

function persist(): void {
  try {
    const persistable = [...cache.entries()].filter(([k]) => isPersistable(k))
    safeSetItem(CACHE_KEY, JSON.stringify(persistable.slice(-MAX_ENTRIES)))
  } catch (e) {
    // Best-effort (quota / private mode / serialize failure). The next report
    // retries the write. Surfaced in dev so a persistent failure is visible.
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    if (import.meta.env.DEV) console.warn('image dims cache persist failed', e)
  }
}

function schedulePersist(): void {
  if (persistTimer !== null) return
  persistTimer = setTimeout(() => {
    persistTimer = null
    persist()
  }, PERSIST_DEBOUNCE_MS)
}

/** Dimensions learned from a prior load of this slug's image, if any. */
export function getImageDims(ref: string): ImageDims | undefined {
  return cache.get(cacheKeyFor(ref))
}

/** Record an image's natural size after a successful load. Zero/negative
 * dimensions are refused — jsdom and a decode-failed image both report 0. */
export function rememberImageDims(ref: string, w: number, h: number): void {
  if (!(w > 0) || !(h > 0)) return
  const key = cacheKeyFor(ref)
  const prev = cache.get(key)
  if (prev && prev.w === w && prev.h === h) return
  // Re-insert so Map order stays LRU-ish and the persist slice keeps the
  // most recently seen entries.
  cache.delete(key)
  cache.set(key, { w, h })
  // A memory-only entry changes nothing on disk, so it must not schedule a
  // write — otherwise a transcript of external images rewrites the stored blob
  // once a second for no benefit.
  if (isPersistable(key)) schedulePersist()
}
