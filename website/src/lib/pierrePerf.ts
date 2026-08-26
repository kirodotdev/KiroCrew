// Rolling accounting for Pierre's content-keyed highlight cache.
//
// Why this exists: this window's black-screen crashes are renderer-process V8
// aborts on a DedicatedWorker thread (the Pierre highlight pool), and they are
// activity-driven — they land while agent output is streaming, not on an idle
// timer. `perf-metrics.js` already records what each PROCESS grew to, but that
// cannot say WHY: a renderer at 2GB looks identical whether the bytes came from
// highlight churn or from anything else. This fills that gap with the one number
// the leading hypothesis actually predicts.
//
// The hypothesis: `contentCacheKey` (PierreImpl.tsx) hashes a file's ENTIRE
// contents, and Pierre caches tokens by that key. While a code block streams,
// every chunk changes the content, so every chunk mints a NEW key — a cache miss
// that re-tokenizes the whole block from scratch. Over C chunks reaching final
// length L that is ~L*C/2 characters hashed and re-tokenized instead of ~L, i.e.
// quadratic in the stream length, with a fresh AST allocated per chunk.
//
// So the falsifiable signal is `maxKeysForOneSurface` — how many distinct keys
// ONE rendered surface minted inside a window. The unit is a surface, not a
// filename: a diff's old side and new side share one name yet each legitimately
// tokenizes once, so a per-name count would read every settled diff as 2 — a
// false confirm. Streaming a block in C chunks drives its surface to C;
// rendering C different blocks once each leaves every surface at 1; a settled
// diff is 1 per side. The aggregate ratio `chars / maxLen` deliberately does NOT
// carry the verdict: five distinct blocks rendered once produce the same ratio
// and the same `keys == calls` shape as one block re-tokenized five times, so
// reading confirmation off the ratio alone would false-confirm on an ordinary
// multi-block chat window. The ratio still bounds how much hashing was wasted;
// the per-surface count is what names the cause.
//
// Reading the log line off a crash dump just before a crash is what confirms or
// kills the hypothesis — no heap snapshot, and no need to catch the spike inside a
// manual capture window.
//
// Cost discipline, matching perf-metrics.js and pyspy-dump.js: the counters are
// three integer adds on a path that already walks the whole string, the report
// is at most one IPC per FLUSH_MS, and a window where nothing was highlighted
// sends nothing at all. Accounting stays OFF entirely where no reporter drains it
// (the dashboard in a plain browser), so that surface pays nothing and cannot
// accumulate. The main process BUFFERS these reports in memory and writes them to
// the log only when the renderer dies (see pierre-perf-log.js), so a normal
// install pays no disk cost and every crash still carries the activity that
// preceded it.

/** Report shape sent to the main process. Field names are the log line. */
export interface PierrePerfWindow {
  /** contentCacheKey() calls in this window. */
  calls: number
  /** Characters hashed across those calls — the main-thread O(n)-per-call cost. */
  chars: number
  /** Distinct keys minted across ALL surfaces. Each new key is a forced
   *  re-tokenize, so this is the worker-side task count the pool actually ran. */
  keys: number
  /** Distinct keys minted for the single busiest SURFACE INSTANCE — the churn
   *  verdict.
   *
   *  The attribution unit is a mounted surface instance (each caller prefixes a
   *  React useId), not a filename or a surface kind, because every
   *  content-derived proxy identity admits collisions: a filename conflates a
   *  diff's two sides, and a kind+name pair still conflates two same-named
   *  fences rendered independently. Two instances can never share a useId, and
   *  a streaming block is ONE instance re-rendering — so everything that
   *  renders once stays at 1, only a streamed instance climbs to its chunk
   *  count, and a false confirm is impossible by construction. Confirming the
   *  re-tokenize hypothesis reads THIS field, not the ratio. */
  maxKeysForOneSurface: number
  /** Calls whose key had already been seen for that surface in this window: the
   *  same content re-hashed with no content change. That is a memoization
   *  failure, a DIFFERENT defect from key churn, and separating them keeps one
   *  dump from being read as evidence for the wrong one. */
  repeatKeyCalls: number
  /** Longest single content seen. `chars / maxLen` bounds the wasted hashing, but
   *  it is NOT the churn verdict on its own — see maxKeysForOneSurface. */
  maxLen: number
  /** usedJSHeapSize in MB when the engine exposes it, else -1. Main-thread heap
   *  only — worker isolates are NOT included, which is exactly why the process
   *  totals from perf-metrics.js stay the memory source of truth. */
  heapMB: number
}

/** How often a non-empty window is reported. Long enough that a burst of chunks
 *  aggregates into one line instead of one line per chunk, short enough that the
 *  last line before a crash still describes the seconds that caused it. */
const FLUSH_MS = 5000

let calls = 0
let chars = 0
let maxLen = 0
let repeatKeyCalls = 0
// Accounting is OFF until a reporter is found, and that gate is load-bearing
// rather than an optimization. The counters are only ever drained by the
// reporting interval, so in a surface with no reporter -- the dashboard opened in
// a plain browser, where `window.electronAPI` does not exist -- nothing would ever
// empty `keysBySurface` and it would retain every key for the life of the session.
// That is the exact failure this module exists to diagnose, so it must not be the
// failure the module introduces. No reporter means no accumulation at all.
let enabled = false
// Keys are grouped BY RENDERED SURFACE (the caller passes a surface-qualified
// identity), because the number that answers the question is "how many times was
// ONE surface re-tokenized", not "how many keys existed". Discarded every window,
// so it cannot grow without bound the way a session-lifetime registry would.
let keysBySurface = new Map<string, Set<string>>()
let timer: ReturnType<typeof setInterval> | null = null

// Joins a surface kind and a file name into one attribution identity. NUL is
// the separator because a file name may itself contain any printable character;
// built with fromCharCode rather than a string literal so the i18n scanner has
// no literal to misread as user-visible copy (this identity never renders).
const SURFACE_SEP = String.fromCharCode(0)

/** Records one cache-key computation. Called from contentCacheKey, which has
 *  already paid the O(n) walk — this adds only the accounting, and nothing at all
 *  when no reporter is draining it.
 *
 *  `surface` names the rendered surface (diff side, editor base, standalone
 *  block) and `name` the file; the pair is the attribution identity, because a
 *  bare filename conflates a diff's two sides into false churn. */
export function recordCacheKey(surface: string, name: string, key: string, length: number): void {
  if (!enabled) return
  calls += 1
  chars += length
  if (length > maxLen) maxLen = length
  const identity = surface + SURFACE_SEP + name
  let seen = keysBySurface.get(identity)
  if (!seen) {
    seen = new Set<string>()
    keysBySurface.set(identity, seen)
  }
  if (seen.has(key)) repeatKeyCalls += 1
  seen.add(key)
}

function heapMB(): number {
  // performance.memory is a non-standard Chromium extension; absent elsewhere
  // and in tests, so it is read defensively and reported as -1 when missing
  // rather than omitted (a missing field would look like a zero heap).
  try {
    const mem = (performance as unknown as { memory?: { usedJSHeapSize?: number } }).memory
    const used = mem && mem.usedJSHeapSize
    return typeof used === 'number' ? Math.round(used / (1024 * 1024)) : -1
  } catch {
    return -1
  }
}

/** Drains the window. Returns null when nothing was highlighted, so an idle
 *  session reports nothing at all. */
export function drainWindow(): PierrePerfWindow | null {
  if (calls === 0) return null
  let keys = 0
  let maxKeysForOneSurface = 0
  for (const seen of keysBySurface.values()) {
    keys += seen.size
    if (seen.size > maxKeysForOneSurface) maxKeysForOneSurface = seen.size
  }
  const snapshot: PierrePerfWindow = {
    calls,
    chars,
    keys,
    maxKeysForOneSurface,
    repeatKeyCalls,
    maxLen,
    heapMB: heapMB(),
  }
  calls = 0
  chars = 0
  maxLen = 0
  repeatKeyCalls = 0
  keysBySurface = new Map<string, Set<string>>()
  return snapshot
}

/** Starts the reporting interval. Idempotent, and a no-op outside Electron —
 *  the browser dashboard has no main process to log to, so there is no reason
 *  to run a timer there. */
export function startPierrePerfReporting(): void {
  if (timer) return
  const api = (window as unknown as {
    electronAPI?: { reportPierrePerf?: (w: PierrePerfWindow) => void }
  }).electronAPI
  const report = api && api.reportPierrePerf
  if (typeof report !== 'function') return
  enabled = true
  timer = setInterval(() => {
    const w = drainWindow()
    if (!w) return
    try {
      report(w)
    } catch {
      // Never let diagnostics break rendering.
    }
  }, FLUSH_MS)
  // Do not hold the event loop / keep a test runner alive.
  if (typeof (timer as unknown as { unref?: () => void }).unref === 'function') {
    ;(timer as unknown as { unref: () => void }).unref()
  }
}

/** Test seam: stops the interval, disables accounting, and clears counters. */
export function stopPierrePerfReporting(): void {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  enabled = false
  calls = 0
  chars = 0
  maxLen = 0
  repeatKeyCalls = 0
  keysBySurface = new Map<string, Set<string>>()
}
