/**
 * The composer's per-file upload requests share one small limit of requests in
 * flight. A browser opens at most six connections to one host and queues the
 * rest itself: a large set would hold every connection to the gateway, and a
 * file the browser holds back reports its start at zero bytes, so its chip
 * would read as stalled though nothing is wrong. A file waiting here has sent
 * nothing and reported no progress, so it reads as not yet sending.
 */

/** Upload requests in flight at once, across every composer in this window. */
export const UPLOAD_CONCURRENCY = 3

let running = 0
/** Bumped by the test reset, so a request started before it frees no slot after. */
let generation = 0
const waiting: Array<() => void> = []

function abortError(): DOMException {
  return new DOMException('The upload was cancelled before it started.', 'AbortError')
}

function startNext() {
  while (running < UPLOAD_CONCURRENCY && waiting.length) waiting.shift()!()
}

/** Run `start` once a slot is free. A `signal` aborted while the file waits
 *  takes it out of the line: `start` never runs and the promise rejects with
 *  an `AbortError`, as a cancelled request would. */
export function runQueuedUpload<T>(signal: AbortSignal, start: () => Promise<T>): Promise<T> {
  if (signal.aborted) return Promise.reject(abortError())
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      const at = waiting.indexOf(run)
      if (at >= 0) waiting.splice(at, 1)
      reject(abortError())
    }
    function run() {
      signal.removeEventListener('abort', onAbort)
      running++
      const startedIn = generation
      let request: Promise<T>
      try { request = start() } catch (err) { request = Promise.reject(err) }
      request.then(resolve, reject).finally(() => {
        if (startedIn !== generation) return
        running--
        startNext()
      })
    }
    signal.addEventListener('abort', onAbort, { once: true })
    waiting.push(run)
    startNext()
  })
}

/** Forget every waiting file and running count between tests. */
export function __resetUploadQueueForTests() {
  generation++
  running = 0
  waiting.length = 0
}
