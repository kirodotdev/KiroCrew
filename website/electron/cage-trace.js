"use strict";
//
// The authoritative cage figure, captured only when the always-on trajectory says
// it is about to matter.
//
// WHY THIS EXISTS SEPARATELY from memory-watch-log.js: the fatal line is `Near V8
// cage limit` at 0.5% object-heap occupancy, so the exhausted resource is
// RESERVED ADDRESS SPACE. Every committed-bytes metric — V8's `external_memory`,
// `performance.memory`, RSS, `privateBytes` — can sit flat while reservation
// climbs, because a wasm32 `WebAssembly.Memory` guard region and a resizable
// `ArrayBuffer`'s `maxByteLength` reserve without committing. So the trajectory
// buffer can be honest and still miss the cause; this channel is the one that
// cannot.
//
// WHAT IT READS: in a Chromium renderer every ArrayBuffer backing store comes
// from ONE place — Blink's dedicated `array_buffer` PartitionAlloc root, which is
// deliberately created inside the V8 cage (`wtf/allocator/partitions.cc`: "When
// the V8 virtual memory cage is enabled, the ArrayBuffer partition must be placed
// inside of it"). memory-infra dumps it as
// `partition_alloc/partitions/array_buffer` with `virtual_size` (reserved) split
// from `virtual_committed_size` and `size` (resident). That single partition is
// why this covers every consumer at once instead of enumerating ~25 allocation
// paths: they all land there, whatever API created them. The same dump also
// carries `v8/*`, `malloc`, `blink_gc` and `skia`, for every process and every
// worker thread, with no debugger, no COOP/COEP and no constructor patching.
//
// WHY IT IS ARMED RATHER THAN ALWAYS ON: a detailed periodic dump is expensive —
// it walks every allocator in every process. Running it continuously would be a
// permanent tax on every user to catch a crash that happens a few times a week.
// So the cheap always-on series decides when to pay: when external memory climbs
// past a threshold, this records a bounded window and stops. The crash is
// activity-driven and preceded by growth, which is exactly the signal that arms
// it.
//
// SAFETY PROPERTIES, since this writes a file and touches a process-wide
// facility:
//   - One window at a time, bounded in duration, with a cooldown, so a sustained
//     climb produces a few traces rather than an unbounded stream of them.
//   - A cap on total traces per app run, so a pathological session cannot fill
//     the disk.
//   - contentTracing is process-wide and a caller who did not start a recording
//     must never stop someone else's, so every stop is guarded by our own
//     `active` flag.
//   - Best-effort throughout: a diagnostic that breaks the app it is diagnosing
//     is worse than no diagnostic, so every call is wrapped and failure only logs.

/** Growth that arms a capture. Chosen well above ordinary dashboard churn
 *  (steady-state readings sat around 600MB of JS heap with external flat) so an
 *  idle session never records, while the run-up to a cage OOM does.
 *
 *  KNOWN LIMITATION, stated here because the trigger is what bounds it: the arming
 *  signal is external memory, which counts COMMITTED bytes. A cage exhausted purely
 *  by RESERVATION -- a wasm32 `WebAssembly.Memory` reserving multi-GB guard regions
 *  is the canonical case -- moves no committed metric at all, so `growth` stays
 *  below this threshold, no capture ever arms, and the `virtual_size` figure that
 *  would name it is never recorded. That failure mode is therefore NOT covered by
 *  this instrument, and a flush summary showing both external and heap flat before
 *  a cage abort is the signature to read as "reservation-only, look elsewhere"
 *  rather than as "buffers exonerated". Closing it needs a reserved-address-space
 *  trigger, which no renderer-visible API currently exposes. */
const DEFAULT_ARM_GROWTH_KB = 256 * 1024; // 256 MB of external growth across the window

/** How long one capture runs. Long enough to contain several periodic dumps at
 *  the 2s interval, short enough that the trace file stays readable. */
const DEFAULT_WINDOW_MS = 60_000;

/** Quiet period after a capture before another may arm, so a sustained climb
 *  does not produce back-to-back traces. */
const DEFAULT_COOLDOWN_MS = 10 * 60_000;

/** Hard ceiling per app run. A diagnostic must not be able to fill a disk. */
const DEFAULT_MAX_CAPTURES = 3;

/** Dump cadence inside a capture. */
const DUMP_INTERVAL_MS = 2000;

/** The trace config. `excluded_categories: ["*"]` keeps this to memory-infra
 *  alone — a full trace would be orders of magnitude larger for no added answer.
 *  `mode: "detailed"` is required: the light mode reports only process totals and
 *  omits the per-allocator breakdown, which is the entire point. */
function traceConfig() {
  return {
    included_categories: ["disabled-by-default-memory-infra"],
    excluded_categories: ["*"],
    memory_dump_config: {
      triggers: [{ mode: "detailed", periodic_interval_ms: DUMP_INTERVAL_MS }],
    },
  };
}

/**
 * @param {object} deps
 * @param {{startRecording: Function, stopRecording: Function}} deps.contentTracing
 * @param {(slot: number) => string} deps.tracePath  builds the output path for one
 *   capture, keyed by its 1-based ordinal within this run. Naming by ordinal
 *   rather than by timestamp is what bounds disk: `maxCaptures` slots are reused
 *   on every launch, so the traces on disk can never exceed that many files no
 *   matter how many times the app runs and captures.
 * @param {(msg: string) => void} [deps.log]
 * @param {number} [deps.armGrowthKB]
 * @param {number} [deps.windowMs]
 * @param {number} [deps.cooldownMs]
 * @param {number} [deps.maxCaptures]
 * @param {() => number} [deps.nowMs]     clock, injected for tests
 * @param {(fn: () => void, ms: number) => any} [deps.setTimer]
 * @param {(handle: any) => void} [deps.clearTimer]
 */
function createCageTrace({
  contentTracing,
  tracePath,
  log = () => {},
  armGrowthKB = DEFAULT_ARM_GROWTH_KB,
  windowMs = DEFAULT_WINDOW_MS,
  cooldownMs = DEFAULT_COOLDOWN_MS,
  maxCaptures = DEFAULT_MAX_CAPTURES,
  nowMs = () => Date.now(),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (h) => clearTimeout(h),
} = {}) {
  let active = false;
  let captures = 0;
  let lastEndedAt = 0;
  let timer = null;

  function canArm() {
    if (active) return false;
    if (captures >= maxCaptures) return false;
    // lastEndedAt of 0 means nothing has run yet, so the cooldown does not apply.
    if (lastEndedAt !== 0 && nowMs() - lastEndedAt < cooldownMs) return false;
    return true;
  }

  async function stop(reason) {
    // Guarded by our own flag: contentTracing is process-wide, so stopping a
    // recording we did not start would silently steal someone else's trace.
    if (!active) return null;
    active = false;
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
    lastEndedAt = nowMs();
    try {
      // Pass the destination explicitly. `stopRecording()` with no argument
      // writes to an OS temporary file, which is both unfindable by an operator
      // and liable to be reaped -- the trace has to land beside the crash log it
      // explains, or it may as well not exist.
      const file = await contentTracing.stopRecording(tracePath(captures));
      log(`[cage-trace] capture ${captures} written (${reason}): ${file}`);
      return file;
    } catch (err) {
      log(`[cage-trace] capture ${captures} failed to write (${reason}): ${err && err.message ? err.message : err}`);
      return null;
    }
  }

  return {
    /**
     * Called with the trajectory buffer's oldest and newest external readings.
     * Arms a capture when growth crosses the threshold. Returns true when this
     * call started one.
     */
    async considerArming(oldestExternalKB, latestExternalKB) {
      if (oldestExternalKB === null || latestExternalKB === null) return false;
      const growth = latestExternalKB - oldestExternalKB;
      if (growth < armGrowthKB) return false;
      if (!canArm()) return false;

      captures += 1;
      active = true;
      try {
        await contentTracing.startRecording(traceConfig());
      } catch (err) {
        // Never leave the flag set on a failed start, or the guarded stop above
        // would later try to stop a recording that never began.
        active = false;
        lastEndedAt = nowMs();
        log(`[cage-trace] could not start capture: ${err && err.message ? err.message : err}`);
        return false;
      }
      log(
        `[cage-trace] armed: external grew ${(growth / 1024).toFixed(1)}MB across the window ` +
          `(threshold ${(armGrowthKB / 1024).toFixed(0)}MB) — recording ${windowMs}ms of detailed memory dumps ` +
          `to read partition_alloc/partitions/array_buffer virtual_size`
      );
      timer = setTimer(() => {
        void stop("window elapsed");
      }, windowMs);
      if (timer && typeof timer.unref === "function") timer.unref();
      return true;
    },

    /** Stops an in-flight capture so the file lands. Called from
     *  render-process-gone: the renderer dying mid-capture is the most valuable
     *  trace there is, and an unstopped recording is never written at all. */
    async stopForCrash() {
      return stop("renderer died");
    },

    /** Stops on quit so a capture in flight is not lost. */
    async stopForQuit() {
      return stop("app quitting");
    },

    isActive() {
      return active;
    },

    captureCount() {
      return captures;
    },
  };
}

module.exports = {
  createCageTrace,
  traceConfig,
  DEFAULT_ARM_GROWTH_KB,
  DEFAULT_WINDOW_MS,
  DEFAULT_COOLDOWN_MS,
  DEFAULT_MAX_CAPTURES,
};
