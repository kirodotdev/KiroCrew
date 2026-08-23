"use strict";
//
// In-memory ring buffer for the renderer's Pierre highlight-churn reports,
// flushed to the log ONLY when the renderer dies.
//
// Why a buffer instead of just logging each report: `glog` is a bare
// appendFileSync with no rotation (see gatewayLogPath), so a line every few
// seconds for as long as the app renders code would grow the user's log file
// without bound. That cost is only worth paying at the moment it explains
// something, and the thing it explains is the crash.
//
// So steady state writes NOTHING and keeps a bounded history in memory; the
// render-process-gone path flushes it next to the crash line. The result is the
// property the diagnostics were missing: every future crash carries the minutes
// of highlighter activity that preceded it, on a normal install, with no env var
// set in advance and no disk cost for the sessions that never crash.
//
// KIROCREW_DEBUG still opts into continuous per-window logging, for watching a
// reproduction live rather than reading it post-mortem.
//
// Same contract as the other diagnostics modules: best-effort, never throws,
// holds only plain numbers.
//
// REMOVAL CONDITION -- this is deferred-deletion, not furniture. It exists to
// answer one question. Once a crash dump has reported `peakKeysForOneSurface`, the
// answer is in hand: >1 means a single block was re-tokenized that many times and
// the fix belongs in `contentCacheKey`'s caching, ~1 means the highlighter is not
// the cause and the search moves elsewhere. Either way this module, its IPC
// channel, its preload entry and the renderer's timer should come back out --
// delete them rather than leaving a permanent instrument behind a settled
// question.

/** 24 windows at the renderer's 5s report interval is two minutes of lead-up for
 *  a few hundred bytes. Bounded on purpose: this exists to diagnose unbounded
 *  growth, so it must not become a leak itself. */
const DEFAULT_CAPACITY = 24;

/** Coerces one report into plain finite numbers. The renderer is not trusted to
 *  send well-formed values — preload coerces too, but this module is what
 *  decides what reaches a log line, so it re-checks rather than assuming. */
function normalizeWindow(w) {
  const num = (v) => (Number.isFinite(v) ? v : 0);
  if (!w || typeof w !== "object") return null;
  const calls = num(w.calls);
  if (calls <= 0) return null;
  return {
    calls,
    keys: num(w.keys),
    maxKeysForOneSurface: num(w.maxKeysForOneSurface),
    repeatKeyCalls: num(w.repeatKeyCalls),
    chars: num(w.chars),
    maxLen: num(w.maxLen),
    heapMB: Number.isFinite(w.heapMB) ? w.heapMB : -1,
  };
}

/** `chars / maxLen` -- how much hashing was wasted relative to the largest single
 *  content. Useful as magnitude, but NOT the verdict: five distinct blocks each
 *  rendered once produce the same value as one block re-tokenized five times. */
function ratio(w) {
  return w.maxLen > 0 ? w.chars / w.maxLen : 0;
}

/** One log line for a single window. */
function renderWindow(entry) {
  const w = entry.window;
  return (
    `[pierre-perf] ${entry.at} calls=${w.calls} keys=${w.keys} ` +
    `maxKeysForOneSurface=${w.maxKeysForOneSurface} repeatKeyCalls=${w.repeatKeyCalls} ` +
    `chars=${w.chars} maxLen=${w.maxLen} ` +
    `charsPerMaxLen=${w.maxLen > 0 ? ratio(w).toFixed(1) : "n/a"} mainHeap=${w.heapMB}MB`
  );
}

/** Aggregate across the buffered windows. Put FIRST in the flush so a reader who
 *  only sees the top of the dump still gets the verdict.
 *
 *  `peakKeysForOneSurface` is the number that decides the hypothesis: >1 means a
 *  single block was re-tokenized that many times inside one 5s window, which many
 *  distinct blocks cannot produce. `peakRepeatKeyCalls` separates the other
 *  failure mode (identical content re-hashed, i.e. a memoization miss), so one
 *  dump is not read as evidence for whichever defect the reader expected. */
function renderSummary(entries) {
  let calls = 0;
  let keys = 0;
  let chars = 0;
  let maxLen = 0;
  let peak = 0;
  let peakHeap = -1;
  let peakKeysForOneSurface = 0;
  let peakRepeatKeyCalls = 0;
  for (const e of entries) {
    const w = e.window;
    calls += w.calls;
    keys += w.keys;
    chars += w.chars;
    if (w.maxLen > maxLen) maxLen = w.maxLen;
    const r = ratio(w);
    if (r > peak) peak = r;
    if (w.heapMB > peakHeap) peakHeap = w.heapMB;
    if (w.maxKeysForOneSurface > peakKeysForOneSurface) peakKeysForOneSurface = w.maxKeysForOneSurface;
    if (w.repeatKeyCalls > peakRepeatKeyCalls) peakRepeatKeyCalls = w.repeatKeyCalls;
  }
  return (
    `[pierre-perf] pre-crash summary over ${entries.length} window(s): ` +
    `calls=${calls} keys=${keys} peakKeysForOneSurface=${peakKeysForOneSurface} ` +
    `peakRepeatKeyCalls=${peakRepeatKeyCalls} chars=${chars} largestContent=${maxLen} ` +
    `peakCharsPerMaxLen=${peak.toFixed(1)} peakMainHeap=${peakHeap}MB`
  );
}

/**
 * @param {object} [deps]
 * @param {number} [deps.capacity]      windows retained (default 24)
 * @param {() => string} [deps.now]     timestamp source, injected for tests
 */
function createPierrePerfLog({ capacity = DEFAULT_CAPACITY, now } = {}) {
  const cap = Math.max(1, Number(capacity) || DEFAULT_CAPACITY);
  const stamp = typeof now === "function" ? now : () => new Date().toISOString();
  /** @type {{at: string, window: object}[]} */
  let entries = [];

  return {
    /** Buffers one report. Returns the normalized window, or null when the
     *  report was empty/malformed and nothing was recorded. */
    record(w) {
      const win = normalizeWindow(w);
      if (!win) return null;
      entries.push({ at: stamp(), window: win });
      if (entries.length > cap) entries = entries.slice(entries.length - cap);
      return win;
    },

    /** One line for the most recent window, for KIROCREW_DEBUG live logging. */
    lastLine() {
      if (entries.length === 0) return null;
      return renderWindow(entries[entries.length - 1]);
    },

    /** Drains the buffer into log lines: summary first, then each window oldest
     *  to newest. Returns [] when nothing was buffered, so a crash with no
     *  highlighting activity adds no noise — itself a useful signal, since it
     *  points away from the highlighter. */
    flush() {
      if (entries.length === 0) return [];
      const lines = [renderSummary(entries), ...entries.map(renderWindow)];
      entries = [];
      return lines;
    },

    size() {
      return entries.length;
    },
  };
}

module.exports = {
  createPierrePerfLog,
  normalizeWindow,
  DEFAULT_CAPACITY,
};
