import { useEffect, useState } from 'react'

/**
 * What a chip-candidate string actually is on disk.
 *
 * `missing` covers every "do not offer an affordance" outcome, not just ENOENT:
 * a forbidden path (denylisted credential store), a malformed path rejected by
 * the endpoint schema, and a network failure all collapse to `missing`. That
 * keeps the probe from becoming an existence oracle — a caller cannot tell
 * "~/.ssh/id_rsa exists" from "~/.ssh/id_rsa does not exist", because the
 * backend answers both identically and we report both as `missing`.
 */
export type PathKind = 'file' | 'dir' | 'missing'

/**
 * Resolved probes, keyed by the raw path text.
 *
 * Module-level, and shared by every surface that asks whether a transcript path
 * exists -- markdown chips, diff-block headers and tool-call lines -- so a path
 * mentioned in all three is asked about once. A transcript re-renders on every
 * stream chunk, so per-component state would re-probe continuously. Deliberately
 * NOT react-query: `MarkdownRenderer` is rendered outside any
 * `QueryClientProvider` in ~30 places (including 9 test files that render it
 * bare), and `useQuery` throws without a provider.
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

/** Most paths one batch request names; mirrors the endpoint's own cap. */
const MAX_BATCH_PATHS = 100

/** Keep a batch body well under the endpoint's 64 KB JSON body bound. */
const MAX_BATCH_BYTES = 48 * 1024

/** Paths waiting for the next batch request, each with the settle function of
 *  the promise its callers share. */
let pending = new Map<string, (kind: PathKind) => void>()
let flushQueued = false

/**
 * Ask the backend what each of `paths` is, in one request. Never rejects.
 *
 * `POST /api/file-kinds` applies the per-path `HEAD /api/file-read` rules to
 * every entry, relative paths resolved against the project as `resolve=1`
 * would. Any answer that is not a well-formed batch reply -- a refusal, a
 * network failure, an unknown kind -- reads as `missing`, so the UI fails
 * closed to "no affordance".
 */
async function probeBatch(paths: string[]): Promise<Record<string, PathKind>> {
  const out: Record<string, PathKind> = {}
  for (const p of paths) out[p] = 'missing'
  try {
    const res = await fetch('/api/file-kinds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: JSON.stringify({ paths }),
    })
    if (!res.ok) return out
    const body = (await res.json()) as { kinds?: Record<string, unknown> }
    const kinds = body && typeof body.kinds === 'object' && body.kinds ? body.kinds : {}
    for (const p of paths) {
      const k = kinds[p]
      if (k === 'file' || k === 'dir') out[p] = k
    }
  } catch { /* every path stays `missing` */ }
  return out
}

/** Split the queued paths into requests that respect both batch bounds. */
function chunk(paths: string[]): string[][] {
  const chunks: string[][] = []
  let cur: string[] = []
  let bytes = 0
  for (const p of paths) {
    const size = JSON.stringify(p).length + 1
    if (cur.length && (cur.length >= MAX_BATCH_PATHS || bytes + size > MAX_BATCH_BYTES)) {
      chunks.push(cur)
      cur = []
      bytes = 0
    }
    cur.push(p)
    bytes += size
  }
  if (cur.length) chunks.push(cur)
  return chunks
}

/**
 * Send everything queued since the last flush. Runs as a microtask, so every
 * consumer whose effect ran in the same React commit -- every chip, diff header
 * and tool line one view mounts -- joins one request.
 */
function flush(): void {
  flushQueued = false
  const batch = pending
  pending = new Map()
  for (const paths of chunk([...batch.keys()])) {
    void probeBatch(paths).then(kinds => {
      for (const p of paths) batch.get(p)?.(kinds[p])
    })
  }
}

function enqueue(path: string): Promise<PathKind> {
  return new Promise<PathKind>(settle => {
    pending.set(path, settle)
    if (!flushQueued) {
      flushQueued = true
      queueMicrotask(flush)
    }
  })
}

/** Shared probe path used by both the hook and its synchronous cache peek. */
function resolveKind(path: string): PathKind | Promise<PathKind> {
  const cached = cachedKind(path)
  if (cached) return cached
  let p = inflight.get(path)
  if (!p) {
    p = enqueue(path).then(kind => {
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
  pending = new Map()
  flushQueued = false
}
