"use strict";
//
// In-memory ring buffer for the renderer's large-allocation reports (see
// src/lib/allocWatch.ts), flushed to the log ONLY when the renderer dies.
//
// Same contract and rationale as pierre-perf-log.js: `glog` is a bare
// appendFileSync with no rotation, so writing a line per large allocation would
// grow the user's log without bound. Steady state writes NOTHING and keeps a
// bounded history in memory; the render-process-gone path flushes it next to the
// crash line, so every future OOM carries the binary allocations that preceded
// it — on a normal install, with no env var armed ahead of time.
//
// The thing this answers: the native log shows a V8 cage OOM with a near-empty
// JS heap, which means a large ArrayBuffer/TypedArray backing store, but V8
// could not capture the allocation's stack. The last entries in this buffer are
// that stack.
//
// KIROCREW_DEBUG opts into logging each event as it arrives, for watching a
// reproduction live rather than reading it post-mortem.
//
// Best-effort, never throws, holds only plain numbers and short strings.
//
// REMOVAL CONDITION — deferred-deletion. Once a crash dump has reported the
// allocation kind + size + stack behind the cage OOM, delete this module, its
// IPC channel, its preload entry and the renderer's watcher rather than leaving
// a permanent instrument behind a settled question.

/** 32 large allocations of lead-up is plenty to name a culprit, and bounded on
 *  purpose: this exists to diagnose runaway allocation, so it must not become a
 *  leak itself. */
const DEFAULT_CAPACITY = 32;

/** The renderer is not trusted to send well-formed values — preload coerces too,
 *  but this module decides what reaches a log line, so it re-checks. */
const MAX_KIND_LEN = 40;
const MAX_STACK_LEN = 4000;
const MAX_ERROR_LEN = 200;

function clampStr(v, max) {
  if (typeof v !== "string") return "";
  // Flatten control characters (newlines, tabs, DEL, ...) to a single space
  // BEFORE length-bounding. A stack/error/kind carrying "\n" would otherwise be
  // written verbatim into glog via renderEvent/renderSummary, letting a malformed
  // report forge additional "[big-alloc] ..." lines and corrupt the very
  // post-mortem this instrument exists to produce. This module is the
  // authoritative trust boundary for what reaches a log line, so the strip lives
  // here (preload only length-slices).
  const flattened = v.replace(/[\u0000-\u001f\u007f]+/g, " ");
  return flattened.length > max ? flattened.slice(0, max) : flattened;
}

/** Coerces one event into plain finite values, or null when it carries no real
 *  allocation (non-positive/NaN bytes). */
function normalizeEvent(ev) {
  if (!ev || typeof ev !== "object") return null;
  const bytes = Number(ev.bytes);
  if (!Number.isFinite(bytes) || bytes <= 0) return null;
  const outcome = ev.outcome === "failed" ? "failed" : "requested";
  return {
    kind: clampStr(ev.kind, MAX_KIND_LEN) || "?",
    bytes,
    outcome,
    stack: clampStr(ev.stack, MAX_STACK_LEN),
    error: outcome === "failed" ? clampStr(ev.error, MAX_ERROR_LEN) : "",
  };
}

function mib(bytes) {
  return (bytes / (1024 * 1024)).toFixed(1);
}

/** One log line for a single event. The stack is on the same line, arrow-joined
 *  by the renderer, so one grep hit carries the allocation site. */
function renderEvent(entry) {
  const e = entry.event;
  const err = e.outcome === "failed" ? ` error=${e.error}` : "";
  const from = e.stack ? ` from=${e.stack}` : "";
  return (
    `[big-alloc] ${entry.at} kind=${e.kind} bytes=${e.bytes} (${mib(e.bytes)}MB) ` +
    `outcome=${e.outcome}${err}${from}`
  );
}

/** Aggregate across the buffered events. Put FIRST in the flush so a reader who
 *  only sees the top of the dump still gets the verdict: the peak single
 *  allocation is the prime cage-OOM suspect. */
function renderSummary(entries) {
  let peakBytes = 0;
  let totalBytes = 0;
  let failed = 0;
  let peakKind = "?";
  for (const e of entries) {
    const ev = e.event;
    totalBytes += ev.bytes;
    if (ev.bytes > peakBytes) {
      peakBytes = ev.bytes;
      peakKind = ev.kind;
    }
    if (ev.outcome === "failed") failed += 1;
  }
  return (
    `[big-alloc] pre-crash summary over ${entries.length} event(s): ` +
    `peakBytes=${peakBytes} (${mib(peakBytes)}MB) peakKind=${peakKind} ` +
    `totalBytes=${totalBytes} (${mib(totalBytes)}MB) failed=${failed}`
  );
}

/**
 * @param {object} [deps]
 * @param {number} [deps.capacity]      events retained (default 32)
 * @param {() => string} [deps.now]     timestamp source, injected for tests
 */
function createBigAllocLog({ capacity = DEFAULT_CAPACITY, now } = {}) {
  const cap = Math.max(1, Number(capacity) || DEFAULT_CAPACITY);
  const stamp = typeof now === "function" ? now : () => new Date().toISOString();
  /** @type {{at: string, event: object}[]} */
  let entries = [];

  return {
    /** Buffers one event. Returns the normalized event, or null when the report
     *  was empty/malformed and nothing was recorded. */
    record(ev) {
      const event = normalizeEvent(ev);
      if (!event) return null;
      entries.push({ at: stamp(), event });
      if (entries.length > cap) entries = entries.slice(entries.length - cap);
      return event;
    },

    /** One line for the most recent event, for KIROCREW_DEBUG live logging. */
    lastLine() {
      if (entries.length === 0) return null;
      return renderEvent(entries[entries.length - 1]);
    },

    /** Drains the buffer into log lines: summary first, then each event oldest to
     *  newest. Returns [] when nothing was buffered, so a crash with no large
     *  allocations adds no noise. An empty flush rules out only large
     *  JS-constructed buffers in the main frame — the renderer's watcher sees JS
     *  constructor calls at or above its threshold in the top-level document,
     *  not host-API backing stores, cumulative sub-threshold buffers,
     *  resizable-ArrayBuffer growth, or allocations made off the main frame
     *  (workers, subframes, WASM memory). */
    flush() {
      if (entries.length === 0) return [];
      const lines = [renderSummary(entries), ...entries.map(renderEvent)];
      entries = [];
      return lines;
    },

    size() {
      return entries.length;
    },
  };
}

module.exports = {
  createBigAllocLog,
  normalizeEvent,
  DEFAULT_CAPACITY,
};
