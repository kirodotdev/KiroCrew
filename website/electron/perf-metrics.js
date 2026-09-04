"use strict";
//
// Debug-only recorder for Electron's own per-process metrics.
//
// Why a recorder and not a query endpoint: `app.getAppMetrics()` is an
// Electron-main API, so it can only be read from inside this process. The
// backend CLI (`kirocrew desktop metrics`) is a separate Python process and a
// second Electron instance cannot read the first one's metrics, so there is no
// way for the CLI to ask the running app for a live sample without introducing
// a listener. Rather than open one, this module samples on an interval and
// writes a bounded artifact next to the gateway log; the CLI reads that file.
//
// The tradeoff is deliberate: the CLI sees recent history rather than a live
// instant. For the question this exists to answer -- which process is burning
// CPU or growing its working set, and since when -- a short rolling history is
// more useful than a single sample anyway, and it survives the app becoming
// unresponsive.
//
// Same contract as pyspy-dump.js: strictly best-effort, never throws, and never
// keeps the app alive. It is OFF unless KIROCREW_DEBUG is set, so a normal user
// install writes nothing and pays no sampling cost.
//
const fs = require("fs");
const os = require("os");
const path = require("path");

const ARTIFACT_NAME = "desktop-metrics.json";
const DEBUG_ENV_VAR = "KIROCREW_DEBUG";

// Kept in sync with kiro_crew.perf_sampler.profiling_enabled: the desktop and
// backend halves of the profiler must agree on what "debug" means, or the CLI
// reads an artifact the app declined to write (or vice versa).
const TRUTHY = new Set(["1", "true", "yes", "on"]);

// 120 samples at the default 5s interval is ten minutes of history for a few
// hundred KB. Bounded on purpose: this writes into the user's log directory on
// an interval, so unbounded growth would be a disk leak in a debug tool.
const DEFAULT_CAPACITY = 120;
const DEFAULT_INTERVAL_MS = 5000;
const MIN_INTERVAL_MS = 500;

/**
 * Whether debug profiling is enabled.
 *
 * Mirrors the backend gate exactly, including treating an explicit "0"/"false"
 * as OFF rather than as "the variable is set, so on".
 */
function profilingEnabled(env = process.env) {
  const raw = env && env[DEBUG_ENV_VAR];
  if (raw === undefined || raw === null) return false;
  return TRUTHY.has(String(raw).trim().toLowerCase());
}

/**
 * Reduce one `app.getAppMetrics()` entry to the fields worth recording.
 *
 * An allowlist rather than a passthrough: the raw entries carry platform-specific
 * extras, and this artifact is meant to be attachable to a bug report, so the
 * shape it can contain should be known rather than whatever the runtime supplies.
 */
function normalizeMetric(entry) {
  if (!entry || typeof entry !== "object") return null;
  const cpu = entry.cpu || {};
  const memory = entry.memory || {};
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const out = {
    pid: num(entry.pid),
    type: typeof entry.type === "string" ? entry.type : null,
    cpuPercent: num(cpu.percentCPUUsage),
    idleWakeupsPerSecond: num(cpu.idleWakeupsPerSecond),
    workingSetKb: num(memory.workingSetSize),
    peakWorkingSetKb: num(memory.peakWorkingSetSize),
  };
  // Only present for utility processes; omitted entirely when absent so the
  // record does not carry a wall of nulls.
  if (typeof entry.name === "string" && entry.name) out.name = entry.name;
  if (typeof entry.serviceName === "string" && entry.serviceName) out.serviceName = entry.serviceName;
  return out;
}

/**
 * Build one timestamped sample from a metrics array.
 */
function buildSample(rawMetrics, now = () => new Date()) {
  const list = Array.isArray(rawMetrics) ? rawMetrics : [];
  const processes = list.map(normalizeMetric).filter((m) => m !== null);
  return {
    at: now().toISOString(),
    processes,
    totalCpuPercent: processes.reduce((a, p) => a + (p.cpuPercent || 0), 0),
    totalWorkingSetKb: processes.reduce((a, p) => a + (p.workingSetKb || 0), 0),
  };
}

/**
 * Serialize the ring to the artifact shape the CLI reads.
 */
function renderArtifact(samples, meta = {}) {
  return JSON.stringify(
    {
      version: 1,
      // The reader keys off this, so a future shape change is detectable rather
      // than being silently misparsed as v1.
      platform: meta.platform || process.platform,
      electron: meta.electron || null,
      intervalMs: meta.intervalMs || null,
      samples,
    },
    null,
    2
  );
}

/**
 * Create the interval recorder.
 *
 * Returns `{start, stop, sampleOnce, artifactPath, enabled}`. When profiling is
 * disabled, `start` is a no-op and nothing is ever written -- callers do not need
 * to check the gate themselves.
 *
 * Every dependency is injectable because this runs inside Electron main, which is
 * not exercised by the unit-test runner.
 */
function createMetricsRecorder({
  dir,
  getAppMetrics,
  intervalMs = DEFAULT_INTERVAL_MS,
  capacity = DEFAULT_CAPACITY,
  env = process.env,
  log = () => {},
  now = () => new Date(),
  writeFileSync = fs.writeFileSync,
  renameSync = fs.renameSync,
  mkdirSync = fs.mkdirSync,
  unlinkSync = fs.unlinkSync,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  meta = {},
} = {}) {
  const enabled = profilingEnabled(env);
  const artifactPath = dir ? path.join(dir, ARTIFACT_NAME) : null;
  // Guard the interval floor so a bad value cannot turn a debug aid into a busy
  // loop competing with the app it is measuring.
  const effectiveInterval = Math.max(MIN_INTERVAL_MS, Number(intervalMs) || DEFAULT_INTERVAL_MS);
  const cap = Math.max(1, Number(capacity) || DEFAULT_CAPACITY);
  const samples = [];
  let handle = null;

  function flush() {
    if (!artifactPath) return false;
    // Write to a sibling temp file and rename: the CLI may read this at any
    // moment, and a partially written JSON file would fail to parse. rename is
    // atomic within a directory, so a reader sees either the old or the new file.
    const tmp = `${artifactPath}.${process.pid}.tmp`;
    try {
      mkdirSync(path.dirname(artifactPath), { recursive: true });
      writeFileSync(tmp, renderArtifact(samples, { ...meta, intervalMs: effectiveInterval }), {
        mode: 0o600,
      });
      renameSync(tmp, artifactPath);
      return true;
    } catch (e) {
      log(`desktop metrics write failed: ${e && e.message}`);
      try {
        unlinkSync(tmp);
      } catch {
        /* the temp file may not exist; nothing to clean up */
      }
      return false;
    }
  }

  function sampleOnce() {
    if (!enabled) return false;
    let raw;
    try {
      raw = getAppMetrics ? getAppMetrics() : [];
    } catch (e) {
      // A debug recorder must never destabilize the app it measures.
      log(`desktop metrics sample failed: ${e && e.message}`);
      return false;
    }
    samples.push(buildSample(raw, now));
    // Ring: drop oldest first so the artifact is the most recent window.
    while (samples.length > cap) samples.shift();
    return flush();
  }

  function start() {
    if (!enabled) {
      return false;
    }
    if (handle !== null) return true; // idempotent; a second start must not double-sample
    sampleOnce(); // write immediately so the artifact exists before the first tick
    handle = setIntervalFn(sampleOnce, effectiveInterval);
    // unref so a debug sampler can never hold the app open at quit. Electron's
    // timers are Node timers, so this is available; guarded for injected fakes.
    if (handle && typeof handle.unref === "function") handle.unref();
    log(`desktop metrics recording every ${effectiveInterval}ms to ${artifactPath}`);
    return true;
  }

  function stop() {
    if (handle !== null) {
      clearIntervalFn(handle);
      handle = null;
    }
    // Final flush so the last window survives a clean quit.
    return enabled ? flush() : false;
  }

  return {
    start,
    stop,
    sampleOnce,
    artifactPath,
    enabled,
    get sampleCount() {
      return samples.length;
    },
  };
}

module.exports = {
  createMetricsRecorder,
  profilingEnabled,
  normalizeMetric,
  buildSample,
  renderArtifact,
  ARTIFACT_NAME,
  DEBUG_ENV_VAR,
  DEFAULT_INTERVAL_MS,
  DEFAULT_CAPACITY,
  MIN_INTERVAL_MS,
};
