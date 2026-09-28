import { useEffect, useState } from 'react'
import { fileReadUrl } from '../utils/fileReadUrl'

/**
 * What a chip-candidate string actually is on disk.
 *
 * `missing` covers every "do not offer an affordance" outcome, not just ENOENT:
 * a forbidden path (denylisted credential store), a malformed path rejected by
 * the endpoint schema, and a network failure all collapse to `missing`. That
 * keeps the probe from becoming an existence oracle — a caller cannot tell
 * "~/.ssh/id_rsa exists" from "~/.ssh/id_rsa does not exist", because the
 * backend 400s both identically and we report both as `missing`.
 */
export type PathKind = 'file' | 'dir' | 'missing'

/**
 * Resolved probes, keyed by the raw chip text.
 *
 * Module-level for two reasons. A transcript re-renders on every stream chunk,
 * so per-component state would re-probe continuously; and the same path is
 * usually mentioned many times across a conversation, so one probe should serve
 * every chip. Deliberately NOT react-query: `MarkdownRenderer` is rendered
 * outside any `QueryClientProvider` in ~30 places (including 9 test files that
 * render it bare), and `useQuery` throws without a provider. `DiffBlock`'s HEAD
 * probe sets the same precedent with a plain fetch.
 */
const kindCache = new Map<string, { kind: PathKind; at: number }>()
/** In-flight probes, so N chips for one path issue exactly one request. */
const inflight = new Map<string, Promise<PathKind>>()

/** Bound the cache so a long-lived session cannot grow it without limit. Map
 *  iterates in insertion order, so the first key is the oldest. */
const MAX_CACHE = 500

/**
 * How long a `missing` verdict is trusted, in ms.
 *
 * `file` and `dir` are cached for the session: a path that exists rarely stops
 * existing mid-conversation, and re-probing on every re-render of a long
 * transcript is the cost this cache exists to avoid. `missing` is the verdict
 * that legitimately flips — the agent writes the file a moment after mentioning
 * it — so caching it forever would leave the chip permanently inert. Matches the
 * 10s `staleTime` the dashboard already uses for `['file-read', path]`.
 */
const MISSING_TTL_MS = 10_000

function cachedKind(path: string): PathKind | undefined {
  const hit = kindCache.get(path)
  if (!hit) return undefined
  if (hit.kind === 'missing' && Date.now() - hit.at > MISSING_TTL_MS) {
    kindCache.delete(path)
    return undefined
  }
  return hit.kind
}

function remember(path: string, kind: PathKind): void {
  if (kindCache.size >= MAX_CACHE) {
    const oldest = kindCache.keys().next().value
    if (oldest !== undefined) kindCache.delete(oldest)
  }
  kindCache.set(path, { kind, at: Date.now() })
}

/**
 * Ask the backend what `path` is. Resolves, never rejects.
 *
 * Uses HEAD so a 500KB file is not transferred just to classify it. The
 * endpoint reports the answer in `X-Path-Kind`; a response without the header
 * (older backend, proxy that strips it, any non-2xx we did not anticipate) is
 * treated as `missing` so the UI fails closed to "no affordance".
 */
async function probe(path: string): Promise<PathKind> {
  try {
    const res = await fetch(fileReadUrl(path), { method: 'HEAD' })
    const header = res.headers.get('X-Path-Kind')
    if (header === 'file' || header === 'dir') return header
    // A 200 without the header still means a readable regular file — that is
    // what `file-read` returns content for.
    return res.ok ? 'file' : 'missing'
  } catch {
    return 'missing'
  }
}

/** Shared probe path used by both the hook and its synchronous cache peek. */
function resolveKind(path: string): PathKind | Promise<PathKind> {
  const cached = cachedKind(path)
  if (cached) return cached
  let p = inflight.get(path)
  if (!p) {
    p = probe(path).then(kind => {
      remember(path, kind)
      inflight.delete(path)
      return kind
    })
    inflight.set(path, p)
  }
  return p
}

/**
 * Classify `path` against the filesystem, or return `undefined` while unknown.
 *
 * Pass `null` to skip probing entirely (non-candidate text, or a block that is
 * still streaming). `undefined` means "not yet known" and callers MUST treat it
 * as not-actionable: rendering an affordance optimistically is what made a
 * directory look like a missing file in the first place.
 *
 * The verdict is keyed to the path it was measured for and re-derived during
 * render, so a consumer whose `path` CHANGES sees `undefined` (or a cache hit) on
 * that very render rather than the previous path's answer. Callers now gate an
 * affordance on `undefined` meaning "still deciding" — `MarkdownRenderer` withholds
 * a chip until every probe for it has reported — and carrying a stale `file` across
 * a path change would punch a hole through that barrier, briefly offering to open
 * a path the text no longer names.
 */
export function usePathKind(path: string | null): PathKind | undefined {
  const [entry, setEntry] = useState<{ path: string | null; kind: PathKind | undefined }>(
    () => ({ path, kind: path ? cachedKind(path) : undefined }),
  )

  useEffect(() => {
    if (!path) { setEntry({ path, kind: undefined }); return }
    const resolved = resolveKind(path)
    if (typeof resolved === 'string') { setEntry({ path, kind: resolved }); return }
    let live = true
    resolved.then(k => { if (live) setEntry({ path, kind: k }) })
    // No AbortController: the in-flight promise is shared by every chip for
    // this path, so aborting on one unmount would cancel the others' probe.
    // The `live` flag drops the result for this consumer only.
    return () => { live = false }
  }, [path])

  // State measured for a PREVIOUS path is not an answer about this one. The cache
  // is consulted synchronously so an already-known path stays instant.
  if (entry.path !== path) return path ? cachedKind(path) : undefined
  return entry.kind
}

/** Test seam — drops all cached and in-flight probes. */
export function __resetPathKindCache(): void {
  kindCache.clear()
  inflight.clear()
}
