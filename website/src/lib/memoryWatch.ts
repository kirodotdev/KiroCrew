// Samples the renderer's memory trajectory so a cage OOM has a before, not just
// an after.
//
// This replaces `pierrePerf.ts` and `allocWatch.ts`, which were probes on
// suspected allocation PATHS. The renderer dies with `Near V8 cage limit` while
// the object heap reads 0.5% full, so the exhausted resource is the V8
// pointer-compression cage — address space that also holds every
// ArrayBuffer/TypedArray backing store. A path probe can only ever confirm or
// deny its own hypothesis, and there are ~25 ways to put a backing store in the
// cage without calling a JS buffer constructor (`response.arrayBuffer()`,
// `blob.arrayBuffer()`, `FileReader`, `getImageData`, `new ImageData`,
// `createImageBitmap`+`copyTo`, a WebSocket binary frame, `structuredClone`,
// IndexedDB reads, Cache Storage, `crypto.subtle`, `WebAssembly.Memory`,
// `TextEncoder.encode`, fetch-body stream chunks, `AudioBuffer`/`getChannelData`,
// MediaSource/WebCodecs output, `Buffer.alloc` in the preload's own realm, and
// anything at all inside a worker or subframe). So this measures the POOL those
// all drain into, and stops guessing at pipes.
//
// THE NUMBER THAT MATTERS is `externalKB`: V8's own accounting of backing stores
// plus external strings, i.e. the cage residents that are NOT in the object heap.
// Chromium exposes `performance.memory.usedJSHeapSize` as
// `used_heap_size + external_memory` (`memory_info.cc`), and Electron exposes the
// object-heap half alone as `process.getHeapStatistics().usedHeapSize` (KB). The
// difference is external memory. Both readings come from the SAME isolate — with
// contextIsolation the preload gets its own v8::Context but shares the renderer's
// v8::Isolate — so the subtraction is valid rather than comparing two heaps.
//
// THE SELF-CHECK is why this cannot fail the way its predecessors did.
// `performance.memory` is bucketized and cached for 20 minutes unless the
// renderer is locked to a site, so a probe reading it can return a
// plausible-looking constant forever and read as "flat and healthy". The flush
// summary therefore reports `externalMoved=yes|NO-FROZEN-VALUE|unknown`, derived
// from whether the series ever produced two different values. A frozen series is
// named as broken instrumentation instead of being mistaken for a flat trend.
// `--enable-precise-memory-info` (armed in native-logging.js) removes the cause;
// this reports whether that worked.
//
// COST: one sample every 5s carrying four integers. No allocation is made to test
// the instrument — a synthetic 100MB probe buffer would itself add cage pressure
// to the resource under investigation, so the self-check is derived from the
// series that is being collected anyway.
//
// REMOVAL CONDITION — deferred-deletion, not furniture. Once a crash dump shows
// either (a) `externalDelta` climbing into the hundreds of MB, naming
// backing-store growth as the cause, or (b) `externalDelta` flat with
// `externalMoved=yes`, which exonerates committed backing stores and hands the
// question to `cage-trace.js`'s reserved-address-space figure, the trajectory has
// done its job. Delete this module, its IPC channel, its preload entry and the
// main-side ring buffer then, rather than leaving a permanent sampler behind a
// settled question.

/** Report shape sent to the main process. Field names are the log line.
 *  A metric unavailable in this realm is `null`, never 0 or -1: "the channel does
 *  not exist here" and "the channel read zero" lead to opposite conclusions. */
export interface MemorySample {
  /** Which realm produced it — `main`, `worker:<name>`, or `frame:<origin>`. The
   *  crashes abort on a DedicatedWorker thread, so a sample with no realm label
   *  is unattributable and therefore not worth much. */
  realm: string
  /** `performance.memory.usedJSHeapSize` in KB, or null where unavailable. */
  usedHeapKB: number | null
  /** `performance.memory.jsHeapSizeLimit` in KB — the headroom denominator. */
  limitHeapKB: number | null
  /** V8 external memory in KB: backing stores + external strings. This is the
   *  cage-resident figure, and the one the verdict is read from. */
  externalKB: number | null
}

/** Matches the cadence the replaced instrument used, so the log's time resolution
 *  is unchanged while the quantity measured is not. */
const SAMPLE_MS = 5000

interface Bridge {
  reportMemorySample?: (s: MemorySample) => void
  /** Electron's `process.getHeapStatistics()` surfaced through the preload. The
   *  main world has no `process` under contextIsolation, so the object-heap half
   *  of the subtraction has to be handed over the bridge. */
  heapStatisticsKB?: () => { usedHeapKB: number | null } | null
}

let timer: ReturnType<typeof setInterval> | null = null
let realmLabel = 'main'

function bridge(): Bridge | undefined {
  return (globalThis as unknown as { electronAPI?: Bridge }).electronAPI
}

/** Reads `performance.memory` defensively. It is a non-standard Chromium
 *  extension: absent in workers on some builds and absent entirely off Chromium,
 *  so a throwing or missing property must degrade to null rather than throw into
 *  the sampling timer. */
function perfMemoryKB(): { usedHeapKB: number | null; limitHeapKB: number | null } {
  try {
    const mem = (performance as unknown as {
      memory?: { usedJSHeapSize?: number; jsHeapSizeLimit?: number }
    }).memory
    if (!mem) return { usedHeapKB: null, limitHeapKB: null }
    const used = typeof mem.usedJSHeapSize === 'number' ? Math.round(mem.usedJSHeapSize / 1024) : null
    const limit = typeof mem.jsHeapSizeLimit === 'number' ? Math.round(mem.jsHeapSizeLimit / 1024) : null
    return { usedHeapKB: used, limitHeapKB: limit }
  } catch {
    return { usedHeapKB: null, limitHeapKB: null }
  }
}

/** Builds one sample. `externalKB` is only derivable where BOTH halves are
 *  readable; where the bridge cannot supply the object-heap figure (a worker, or
 *  a plain browser) the field stays null and the flush reports the channel as
 *  unavailable for that realm rather than inventing a number. */
export function takeSample(): MemorySample {
  const { usedHeapKB, limitHeapKB } = perfMemoryKB()
  let externalKB: number | null = null
  try {
    const stats = bridge()?.heapStatisticsKB?.()
    const objectHeapKB = stats && typeof stats.usedHeapKB === 'number' ? stats.usedHeapKB : null
    if (usedHeapKB !== null && objectHeapKB !== null) {
      // usedJSHeapSize = used_heap_size + external_memory, so the difference is
      // external. Clamp at zero: the two readings are taken microseconds apart
      // and a GC in between can make the subtraction transiently negative, which
      // would otherwise read as a nonsense metric rather than as sampling jitter.
      externalKB = Math.max(0, usedHeapKB - objectHeapKB)
    }
  } catch {
    externalKB = null
  }
  return { realm: realmLabel, usedHeapKB, limitHeapKB, externalKB }
}

/**
 * Starts sampling. Idempotent, and a no-op where no reporter exists (a plain
 * browser dashboard), so that surface pays nothing.
 *
 * @param realm label for this realm; pass `worker:<name>` from a worker and
 *              `frame:<origin>` from a subframe so the post-mortem can attribute
 *              growth to the thread that caused it.
 */
export function startMemoryWatch(realm = 'main'): void {
  if (timer) return
  const report = bridge()?.reportMemorySample
  if (typeof report !== 'function') return
  realmLabel = realm
  timer = setInterval(() => {
    try {
      report(takeSample())
    } catch {
      // Never let a diagnostic break the renderer it is observing.
    }
  }, SAMPLE_MS)
  // Do not hold the event loop / keep a test runner alive.
  if (typeof (timer as unknown as { unref?: () => void }).unref === 'function') {
    ;(timer as unknown as { unref: () => void }).unref()
  }
}

/** Test seam: stops the interval and resets the realm label. */
export function stopMemoryWatch(): void {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  realmLabel = 'main'
}
