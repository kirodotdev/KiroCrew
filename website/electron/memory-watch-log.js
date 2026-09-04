"use strict";
//
// In-memory ring buffer for the renderer's MEMORY TRAJECTORY, flushed to the log
// only when a renderer dies. One module, one shape — it replaces
// `pierre-perf-log.js` and `big-alloc-log.js`, which were the same ring buffer
// written twice (identical capacity const, normalizer, per-entry renderer,
// summary renderer and factory body) fed by two probes that both measured the
// wrong quantity.
//
// WHY THE PREVIOUS PROBES COULD NOT WORK, so this one is not a fourth guess:
// the renderer dies with `Near V8 cage limit` / `V8 javascript OOM
// (CALL_AND_RETRY_LAST)` while the GC object heap reads `used=21.9MB
// limit=4192.0MB` — 0.5% full. The exhausted resource is therefore the V8
// pointer-compression cage: an ADDRESS-SPACE reservation, not committed bytes.
// Every figure shipped so far (`usedJSHeapSize`, a constructor's requested
// byte count, OS `workingSetSize`) is a committed-bytes figure, so all of them
// can sit flat while the cage fills. That is a unit mismatch, not a coverage
// gap, and no amount of adding another allocation-path probe fixes it.
//
// WHAT THIS MEASURES INSTEAD: the pool, not the pipes. `externalKB` is V8's own
// accounting of ArrayBuffer/SharedArrayBuffer backing stores plus external
// strings — the bytes that live in the cage but NOT in the object heap. Every
// consumer lands there regardless of how it was created, so a host-API backing
// store (`response.arrayBuffer()`, `blob.arrayBuffer()`, `getImageData`, a
// WebSocket binary frame, `structuredClone`, IndexedDB, `TextEncoder.encode`,
// `AudioBuffer`) shows up the same as a hand-written `new ArrayBuffer(n)`. None
// of those call a JS buffer constructor, which is why wrapping the constructors
// flushed empty across 11 consecutive crashes.
//
// WHAT IT STILL CANNOT SEE, stated plainly so the next reader does not
// over-read an empty result: `externalKB` counts committed backing-store bytes,
// so a cage exhausted purely by RESERVATION — a wasm32 `WebAssembly.Memory`
// guard region, or a resizable `ArrayBuffer`'s `maxByteLength` (V8 accounts only
// its `accounting_length`) — can still grow invisibly here. That case is what
// `cage-trace.js` exists for: it reads the `array_buffer` PartitionAlloc
// partition's `virtual_size`, which is reserved address space. This buffer is
// the always-on trajectory; that trace is the authoritative cage figure.
//
// Same discipline as the modules it replaces: `glog` is a bare appendFileSync
// with no rotation, so steady state writes NOTHING and the flush happens once,
// on death, next to the crash line. An empty flush is informative. KIROCREW_DEBUG
// additionally logs each sample as it arrives, for watching a live reproduction.

/** 60 samples at the renderer's 5s cadence is five minutes of lead-up for a few
 *  KB. Longer than the 24 windows the old buffers kept, because the question is
 *  now "was this a slow climb or an instant spike" and two minutes could not
 *  distinguish them. Still bounded: this exists to diagnose unbounded growth, so
 *  it must not become unbounded itself. */
const DEFAULT_CAPACITY = 60;

/** Realm labels are renderer-supplied. Cap them: the main process writes them
 *  into a log line, so a renderer bug must not be able to put an unbounded
 *  string there. Control characters are flattened for the same reason a forged
 *  newline could otherwise inject a fake log line. */
const MAX_REALM_LEN = 60;

function clampRealm(v) {
  if (typeof v !== "string" || v === "") return "?";
  const flattened = v.replace(/[\u0000-\u001f\u007f]+/g, " ");
  return flattened.length > MAX_REALM_LEN ? flattened.slice(0, MAX_REALM_LEN) : flattened;
}

/** Coerces one sample into plain finite numbers. The renderer is not trusted to
 *  send well-formed values — preload coerces too, but this module decides what
 *  reaches a log line, so it re-checks rather than assuming.
 *
 *  A missing metric is carried as `null`, never as 0 or -1. The distinction is
 *  load-bearing: "this channel is unavailable in this realm" and "this channel
 *  read zero" lead to opposite conclusions, and the previous instrument's
 *  `Number(w.heapMB) || -1` collapsed a genuine 0 into the sentinel. */
function num(v) {
  return Number.isFinite(v) ? v : null;
}

function normalizeSample(s) {
  if (!s || typeof s !== "object") return null;
  const usedHeapKB = num(s.usedHeapKB);
  const externalKB = num(s.externalKB);
  // A sample carrying neither of the two figures the verdict is read from says
  // nothing, so it is not worth a ring slot.
  if (usedHeapKB === null && externalKB === null) return null;
  return {
    realm: clampRealm(s.realm),
    usedHeapKB,
    limitHeapKB: num(s.limitHeapKB),
    externalKB,
  };
}

function mb(kb) {
  return kb === null ? "n/a" : (kb / 1024).toFixed(1);
}

/** One log line for a single sample. */
function renderSample(entry) {
  const s = entry.sample;
  return (
    `[mem] ${entry.at} realm=${s.realm} ` +
    `external=${mb(s.externalKB)}MB jsHeap=${mb(s.usedHeapKB)}MB limit=${mb(s.limitHeapKB)}MB`
  );
}

/** Aggregate across the buffered samples. Put FIRST in the flush so a reader who
 *  only sees the top of the dump still gets the verdict.
 *
 *  `externalDelta` is the number that answers the question: cage pressure from
 *  backing stores shows as external memory climbing while `jsHeap` stays flat,
 *  which is exactly the shape the fatal line reported.
 *
 *  `moved` is the SELF-CHECK, and it is the reason this instrument cannot fail
 *  the way its predecessors did. `performance.memory` is bucketized and cached
 *  for 20 minutes unless the renderer is locked to a site, so a probe reading it
 *  can return a plausible-looking constant forever. If every external sample in
 *  the window is byte-identical, the series is not measuring anything and the
 *  flush says so instead of letting a reader mistake a frozen number for a flat
 *  trend. `--enable-precise-memory-info` (armed in native-logging.js) is what
 *  removes the cause; this reports whether it worked. */
function renderSummary(entries) {
  let peakExternal = null;
  let firstExternal = null;
  let lastExternal = null;
  let peakHeap = null;
  const externalSeen = new Set();
  const realms = new Set();

  for (const e of entries) {
    const s = e.sample;
    realms.add(s.realm);
    if (s.usedHeapKB !== null && (peakHeap === null || s.usedHeapKB > peakHeap)) peakHeap = s.usedHeapKB;
    if (s.externalKB === null) continue;
    externalSeen.add(s.externalKB);
    if (firstExternal === null) firstExternal = s.externalKB;
    lastExternal = s.externalKB;
    if (peakExternal === null || s.externalKB > peakExternal) peakExternal = s.externalKB;
  }

  const delta = firstExternal !== null && lastExternal !== null ? lastExternal - firstExternal : null;
  // "unknown" when no realm ever reported the channel at all -- distinct from
  // "the channel reported one repeated value", which is the frozen case.
  const moved = externalSeen.size === 0 ? "unknown" : externalSeen.size > 1 ? "yes" : "NO-FROZEN-VALUE";

  // The second self-check, and it covers a failure `moved` cannot see. If the two
  // readings the subtraction uses ever describe the SAME quantity, the difference
  // is near zero forever -- but it still jitters, so `moved` reads "yes" and a
  // dead metric passes as healthy. The signature is external pinned near zero
  // while the JS heap is demonstrably large and moving, so that is what this
  // names. Measured basis for the threshold: `usedJSHeapSize` moved 199.6MB for a
  // 200MB ArrayBuffer, so a live external channel tracks backing stores closely
  // and cannot sit under 1MB while the heap reports hundreds.
  const heapIsSubstantial = peakHeap !== null && peakHeap > 64 * 1024;
  const externalPinnedNearZero = peakExternal !== null && peakExternal < 1024;
  const cancelled = heapIsSubstantial && externalPinnedNearZero ? " externalSuspect=CANCELLED-SUBTRACTION" : "";

  return (
    `[mem] pre-crash summary over ${entries.length} sample(s) across ${realms.size} realm(s): ` +
    `peakExternal=${mb(peakExternal)}MB externalDelta=${delta === null ? "n/a" : (delta / 1024).toFixed(1) + "MB"} ` +
    `peakJsHeap=${mb(peakHeap)}MB externalMoved=${moved}${cancelled} realms=${[...realms].join(",") || "none"}`
  );
}

/**
 * @param {object} [deps]
 * @param {number} [deps.capacity]      samples retained (default 60)
 * @param {() => string} [deps.now]     timestamp source, injected for tests
 */
function createMemoryWatchLog({ capacity = DEFAULT_CAPACITY, now } = {}) {
  const cap = Math.max(1, Number(capacity) || DEFAULT_CAPACITY);
  const stamp = typeof now === "function" ? now : () => new Date().toISOString();
  /** @type {{at: string, sample: object}[]} */
  let entries = [];

  return {
    /** Buffers one sample. Returns the normalized sample, or null when the report
     *  carried no usable figure and nothing was recorded. */
    record(s) {
      const sample = normalizeSample(s);
      if (!sample) return null;
      entries.push({ at: stamp(), sample });
      if (entries.length > cap) entries = entries.slice(entries.length - cap);
      return sample;
    },

    /** One line for the most recent sample, for KIROCREW_DEBUG live logging. */
    lastLine() {
      if (entries.length === 0) return null;
      return renderSample(entries[entries.length - 1]);
    },

    /** Most recent external reading in KB, or null. Read by the cage-trace arming
     *  check, which needs the current value rather than a rendered line. */
    latestExternalKB() {
      for (let i = entries.length - 1; i >= 0; i--) {
        const v = entries[i].sample.externalKB;
        if (v !== null) return v;
      }
      return null;
    },

    /** Oldest retained external reading in KB, or null. Paired with
     *  `latestExternalKB()` this gives the growth the trace arms on. */
    oldestExternalKB() {
      for (let i = 0; i < entries.length; i++) {
        const v = entries[i].sample.externalKB;
        if (v !== null) return v;
      }
      return null;
    },

    /** Drains the buffer into log lines: summary first, then each sample oldest
     *  to newest. Returns [] when nothing was buffered, so a crash with no
     *  samples adds no noise — itself a signal that the renderer died before the
     *  sampler ever reported, which points at startup rather than at growth. */
    flush() {
      if (entries.length === 0) return [];
      const lines = [renderSummary(entries), ...entries.map(renderSample)];
      entries = [];
      return lines;
    },

    size() {
      return entries.length;
    },
  };
}

module.exports = {
  createMemoryWatchLog,
  normalizeSample,
  DEFAULT_CAPACITY,
};
