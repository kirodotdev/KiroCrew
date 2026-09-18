// Main-thread client for `diffWorker.ts` — computes a unified patch for a file
// pair off the renderer thread, with cancellation and a small result cache.
//
// Contract: `computePairPatch` never blocks the UI. Cancellation TERMINATES the
// worker when the aborted request is the only one in flight, because jsdiff
// runs synchronously inside the worker — ignoring the result would leave a CPU
// core pinned on a diff nobody wants. The singleton is lazily recreated on the
// next request.
import type { FileContents } from '@pierre/diffs'
import type { PairDiffRequest, PairDiffResponse } from './diffWorker'

interface Pending {
  resolve: (patch: string) => void
  reject: (err: Error) => void
}

let worker: Worker | null = null
let nextId = 1
const pending = new Map<number, Pending>()

/** Tiny LRU: re-opting into the same pair (e.g. after a collapse/expand) must
 *  not recompute a diff that just took seconds. The key is the JSON-serialized
 *  tuple of the four inputs — exact identity, so collisions are impossible by
 *  construction. No JS-level hashing pass runs over the contents; the engine
 *  hashes the key natively on Map access.
 *
 *  Because the key carries both file bodies, one entry costs roughly
 *  old + new + patch chars, so the cache is bounded on chars as well as
 *  entries: eight diffs of large files would otherwise pin hundreds of MB for
 *  the life of the renderer. An entry over the whole budget is not cached. */
const CACHE_MAX = 8
export const CACHE_MAX_CHARS = 4_000_000
const cache = new Map<string, string>()
let cacheChars = 0

const cacheCost = (key: string, patch: string): number => key.length + patch.length

function cacheEvict(key: string) {
  const patch = cache.get(key)
  if (patch === undefined) return
  cache.delete(key)
  cacheChars -= cacheCost(key, patch)
}

function cacheStore(key: string, patch: string) {
  const cost = cacheCost(key, patch)
  if (cost > CACHE_MAX_CHARS) return
  // Two misses for the same pair can be in flight at once (a re-render while
  // the first computation is still pending); both resolve here. Release the
  // earlier entry's cost first so the footprint counts the key once — the Map
  // already holds a single value per key, and the counter must agree with it.
  cacheEvict(key)
  cache.set(key, patch)
  cacheChars += cost
  // Oldest first (Map preserves insertion order) until both bounds hold.
  while (cache.size > CACHE_MAX || cacheChars > CACHE_MAX_CHARS) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cacheEvict(oldest)
  }
}

/** Test seam: the cache's current char footprint. */
export function _diffCacheChars(): number {
  return cacheChars
}

function rejectAllPending(err: Error) {
  for (const p of pending.values()) p.reject(err)
  pending.clear()
}

function ensureWorker(): Worker {
  if (worker) return worker
  worker = new Worker(new URL('./diffWorker.ts', import.meta.url), { type: 'module' })
  worker.onmessage = (e: MessageEvent<PairDiffResponse>) => {
    const p = pending.get(e.data.id)
    if (!p) return // cancelled — result discarded
    pending.delete(e.data.id)
    if (e.data.ok) p.resolve(e.data.patch)
    else p.reject(new Error(e.data.error))
  }
  worker.onerror = () => {
    rejectAllPending(new Error('diff worker crashed'))
    worker?.terminate()
    worker = null
  }
  return worker
}

function teardownWorker() {
  worker?.terminate()
  worker = null
}

/**
 * Compute a unified patch for a file pair off the main thread.
 *
 * Resolves with the patch text; rejects with `AbortError` on cancellation or
 * a plain Error when the worker fails. A null side is treated as an empty
 * file, which yields a pure-add / pure-delete patch.
 */
export function computePairPatch(
  oldFile: FileContents | null,
  newFile: FileContents | null,
  signal?: AbortSignal,
): Promise<string> {
  const name = (newFile ?? oldFile)?.name ?? 'file'
  const inputs = {
    oldName: oldFile?.name ?? name,
    newName: newFile?.name ?? name,
    oldContents: oldFile?.contents ?? '',
    newContents: newFile?.contents ?? '',
  }
  // JSON-serialized tuple — collision-free by construction (a delimiter join
  // collides when an input contains the delimiter). A cache identity, not
  // user-visible copy.
  const key = JSON.stringify([inputs.oldName, inputs.newName, inputs.oldContents, inputs.newContents])
  const cached = cache.get(key)
  if (cached !== undefined) {
    // Refresh LRU position; the footprint is unchanged.
    cache.delete(key)
    cache.set(key, cached)
    return Promise.resolve(cached)
  }
  if (signal?.aborted) return Promise.reject(new DOMException('aborted', 'AbortError'))
  const id = nextId++
  const request: PairDiffRequest = { id, ...inputs }
  return new Promise<string>((resolve, reject) => {
    pending.set(id, {
      resolve: patch => {
        cacheStore(key, patch)
        resolve(patch)
      },
      reject,
    })
    signal?.addEventListener(
      'abort',
      () => {
        if (!pending.has(id)) return
        pending.delete(id)
        // jsdiff is synchronous inside the worker: if nothing else is waiting,
        // terminating is the only way to actually stop the computation.
        if (pending.size === 0) teardownWorker()
        reject(new DOMException('aborted', 'AbortError'))
      },
      { once: true },
    )
    ensureWorker().postMessage(request)
  })
}
