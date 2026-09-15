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
 *  not recompute a diff that just took seconds. The key is the four inputs
 *  joined on NUL — exact identity, so collisions are impossible by
 *  construction. No JS-level hashing pass runs over the contents; the engine
 *  hashes the key natively on Map access. */
const CACHE_MAX = 8
const cache = new Map<string, string>()

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
    // Refresh LRU position.
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
        cache.set(key, patch)
        if (cache.size > CACHE_MAX) {
          const oldest = cache.keys().next().value
          if (oldest !== undefined) cache.delete(oldest)
        }
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
