// Main-thread client for the highlight Web Worker. Code blocks call
// highlightAsync(); the worker does the (potentially backtracking) hljs work on
// its own thread and posts back HTML, which we sanitize and hand to the caller.
// The main thread never runs hljs, so a pathological regex can no longer freeze
// scrolling or interaction.
import DOMPurify from 'dompurify'

interface HighlightResponse {
  id: number
  html: string
}
type Resolver = (html: string) => void

let worker: Worker | null = null
let workerFailed = false
let nextId = 1
const pending = new Map<number, Resolver>()

// Upper bound on how long we wait for a worker reply. The whole reason this
// runs off-thread is that hljs can catastrophically backtrack on some inputs
// (profiled at 327ms, effectively unbounded). A crashed worker clears pending
// via onerror, but a worker that is ALIVE but wedged in a pathological regex
// posts no reply and fires no error — without a timeout that request's promise
// and Map entry would leak forever and later requests would queue behind it.
const HIGHLIGHT_TIMEOUT_MS = 3000

function getWorker(): Worker | null {
  if (workerFailed || typeof Worker === 'undefined') return null
  if (worker) return worker
  try {
    worker = new Worker(new URL('./hljsWorker.ts', import.meta.url), { type: 'module' })
    worker.onmessage = (e: MessageEvent<HighlightResponse>) => {
      const resolve = pending.get(e.data.id)
      if (resolve) {
        pending.delete(e.data.id)
        resolve(e.data.html)
      }
    }
    worker.onerror = () => {
      // Worker couldn't load/run (e.g. CSP blocks worker-src, or a bundle/load
      // error). Give up on the worker for the rest of the session and resolve
      // everything to plain text so callers fall back gracefully. Log in dev so
      // a silent "no code is ever highlighted" isn't a mystery.
      if (import.meta.env.DEV) {
        // Intentional dev-only diagnostic so a silent "no code highlighted"
        // failure isn't a mystery; stripped from production builds by the guard.
        // eslint-disable-next-line no-console
        console.warn('[highlightClient] worker disabled; falling back to plain text')
      }
      workerFailed = true
      worker = null
      for (const resolve of pending.values()) resolve('')
      pending.clear()
    }
  } catch {
    workerFailed = true
    worker = null
  }
  return worker
}

/**
 * Highlight `code` in the worker. Resolves to sanitized HTML, or '' when
 * highlighting is unavailable (no Worker support, e.g. tests, or a worker load
 * failure) so the caller keeps rendering plain text. Regex backtracking is
 * absorbed by the worker thread — the main thread never blocks.
 */
export function highlightAsync(code: string, lang: string | undefined): Promise<string> {
  const w = getWorker()
  if (!w) return Promise.resolve('')
  return new Promise<string>((resolve) => {
    const id = nextId++
    const timer = setTimeout(() => {
      // Worker is alive but wedged. Stop waiting: resolve to plain text, drop
      // the pending entry, and recycle the worker so one bad input can't block
      // every request queued behind it on the single worker thread.
      //
      // Only recycle if `w` is STILL the active worker. A sibling request that
      // shared this worker may have already timed out and replaced it; by now a
      // fresh worker could be serving other requests, and terminating the
      // module-level `worker` blindly would kill that healthy instance. Guard
      // on identity and terminate the captured `w`, not the current `worker`.
      if (pending.delete(id)) {
        if (worker === w) {
          try { w.terminate() } catch { /* ignore */ }
          worker = null
        }
        resolve('')
      }
    }, HIGHLIGHT_TIMEOUT_MS)
    pending.set(id, (html) => {
      clearTimeout(timer)
      resolve(html ? DOMPurify.sanitize(html) : '')
    })
    w.postMessage({ id, code, lang })
  })
}
