"use strict";
//
// Always-on capture of the app's NATIVE diagnostic output (Chromium + V8 +
// renderer), so a crash explains itself without the user having relaunched
// under a debugger first.
//
// The problem this solves: everything Chromium and V8 print goes to the
// process's raw stderr, and a GUI launch (Dock, Finder, Start menu) discards
// stderr entirely. It is not in the macOS unified log either — verified against
// a real renderer abort: `log show --last 12h` filtered to the Electron
// framework returned zero fatal lines. So the single most useful sentence about
// a renderer death — V8's own `Fatal error in ... / Reached heap limit /
// invalid size` — was being thrown away, leaving only a `.ips` crash report
// whose `asi` field is null and whose every frame symbol is a
// nearest-neighbour mismatch. `renderer-recovery.js` could say THAT the
// renderer died and reload it; nothing could say WHY.
//
// This is the same correction already applied to the gateway child process,
// whose spawn used `stdio:"ignore"` until a silent Gatekeeper SIGKILL proved
// that a discarded stream is a discarded bug report (see the comment above
// `gatewayLogPath` in main.js). The app's own native output is the last stream
// still going nowhere.
//
// Two channels, because they carry different things and neither subsumes the
// other:
//
//   1. Chromium's log file (`--enable-logging=file --log-file=`). Carries
//      Chromium's own `LOG()` output, renderer console errors, and GPU /
//      network / sandbox failures. Set from here rather than asked of the user,
//      because a switch the user must remember to pass is a switch that is
//      never set on the launch that actually crashed.
//   2. A local minidump via `crashReporter`. Carries the abort context for a
//      renderer that dies without printing anything at all.
//
// Both are bounded by keeping exactly two generations of the log file (see
// `rotateNativeLog`): the run being debugged is almost never the run that is
// running, so the previous session has to survive the relaunch that
// investigates it.
//
// Deliberately NOT attempted: redirecting the main process's own fd 2 to a
// file. Node exposes no `dup2`, so the only ways to do it are a native addon or
// re-spawning the app with `stdio` set — a double launch that would break the
// single-instance lock, Dock activation, and the updater. A terminal launch
// (`Contents/MacOS/<name> > log 2>&1`) remains the way to capture true raw
// stderr, and that stays a deliberate debugging step rather than something the
// app does to itself on every boot.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so the decisions have to be testable without a live `app`
// (same pattern as renderer-recovery.js / perf-metrics.js).
//

const path = require("path");
// The SAME debug opt-in the desktop profiler uses, rather than a second one:
// `enable-precise-memory-info` below is a profiling switch, and one gate for
// all of them keeps `KIROCREW_DEBUG=1` the single answer to "turn profiling on".
const { profilingEnabled } = require("./perf-metrics");

/** Log file name, alongside gateway-launch.log in the app's logs directory. */
const NATIVE_LOG_BASENAME = "chromium.log";

/** The retained previous session. Named, not numbered, so a user handing logs
 *  over can tell which file is the run that went wrong. */
const NATIVE_LOG_PREVIOUS_BASENAME = "chromium.previous.log";

/**
 * Absolute path of the Chromium log file inside `logsDir`.
 */
function nativeLogPath(logsDir) {
  return path.join(String(logsDir || ""), NATIVE_LOG_BASENAME);
}

/**
 * Absolute path of the retained previous-session log, beside `logPath`.
 */
function previousNativeLogPath(logPath) {
  return path.join(path.dirname(String(logPath || "")), NATIVE_LOG_PREVIOUS_BASENAME);
}

/**
 * The Chromium switches that route native logging to `logPath`.
 *
 * Returned as data rather than applied inline so a test can assert the exact
 * switch names: these are Chromium's spelling, not Electron's, and a typo here
 * fails silently (an unknown switch is ignored, logging simply stays off).
 *
 * @param {object} [env] Environment consulted for the debug opt-in.
 * @returns {Array<[string, string]>} `[name, value]` pairs for appendSwitch.
 */
function nativeLoggingSwitches(logPath, env = process.env) {
  const switches = [
    // `=file` is what sends output to --log-file instead of stderr, which the
    // GUI launch we are compensating for would throw away again.
    ["enable-logging", "file"],
    ["log-file", String(logPath)],
  ];
  // Makes `performance.memory` exact and uncached. Without it Chromium
  // BUCKETIZES those values and caches them for 20 MINUTES unless the renderer
  // happens to be locked to a site -- so a memory probe reading it can return a
  // plausible-looking constant forever and be misread as "flat and healthy".
  // The renderer-memory trajectory (src/lib/memoryWatch.ts) derives V8 external
  // memory from that reading, so this switch is what makes its series real; its
  // flush reports `externalMoved=NO-FROZEN-VALUE` if the number never changes,
  // which is the check that this switch actually took effect. Value-less switch,
  // so the empty string is the whole argument.
  //
  // DEBUG-ONLY, unlike the two above: the bucketization it removes is a
  // Chromium PRIVACY control, and it is removed per-PROCESS for every renderer
  // -- including the browser-panel renderers that load UNTRUSTED pages, where
  // exact heap sizes are the side channel the bucketing exists to blunt. The
  // logging switches serve a user debugging their own crash; this one widens
  // what an arbitrary page can measure, so it stays behind the same
  // KIROCREW_DEBUG opt-in as the rest of the profiling surface and is OFF on a
  // normal install. Turning it on is what a memory investigation already does.
  if (profilingEnabled(env)) switches.push(["enable-precise-memory-info", ""]);
  return switches;
}

/**
 * Start-of-boot rotation, which is what bounds this file's size.
 *
 * Neither Chromium's log file nor this app's `glog` has any rotation (glog is a
 * bare appendFileSync), so an always-on stream that only ever appends would
 * grow without limit on a long-lived install. But truncating to nothing is the
 * opposite mistake: it destroys the previous session at the exact moment a
 * developer relaunches to investigate it. A main-process crash, a hard quit, or
 * simply "change the code and restart to reproduce" all end the session that
 * holds the evidence, and the next launch would wipe it before anyone read it.
 * (Only a RENDERER death is healed in-process, and that is the narrow case —
 * not the general one this capture exists for.)
 *
 * So: keep one generation. The current file becomes `chromium.previous.log` and
 * Chromium creates a fresh one, leaving the last bad run readable from inside
 * the run that is debugging it. Renaming rather than copying also means this
 * works whether Chromium opens its log in append or truncate mode — the path it
 * opens is simply absent, so it starts clean either way.
 *
 * The bound is therefore two sessions. A single session is not itself capped,
 * because Chromium owns that file handle and nothing on this side can cap it;
 * the size that matters in practice is one session's worth of Chromium logging,
 * which is small unless something is looping — and something looping is the
 * thing we want recorded.
 *
 * Returns which generations exist afterwards, and whether a rotation that was
 * NEEDED could not be performed. A failure is reported, never thrown: losing
 * rotation is worth a log line, not a failed launch. The caller distinguishes
 * `blocked` from an ordinary first launch, because the two want opposite
 * handling — nothing to preserve is safe, failing to preserve is not.
 */
function rotateNativeLog(logPath, { fs, log = () => {} } = {}) {
  const previousPath = previousNativeLogPath(logPath);
  try {
    if (!fs.existsSync(logPath)) {
      // First launch on this install, or the file was cleaned up. Nothing to
      // preserve and nothing to do — Chromium will create it.
      return { rotated: false, blocked: false, previousPath: null };
    }
    // Overwrites any older generation, which is the point: two files, not N.
    // `renameSync` replaces an existing destination on Windows too (libuv
    // passes MOVEFILE_REPLACE_EXISTING), which is what `perf-metrics.js`
    // already relies on for its rolling artifact — so the destination existing
    // is not itself a failure mode. What DOES fail on Windows is a sharing
    // violation when any handle is open on either path (an AV or
    // Search-indexer touch is enough); see `replace_with_retry` in
    // `src/kiro_crew/atomic_write.py`.
    fs.renameSync(logPath, previousPath);
    return { rotated: true, blocked: false, previousPath };
  } catch (e) {
    // A read-only directory or a Windows sharing violation reaches here. The
    // live log is still on disk and still holds the session we were trying to
    // preserve, so this is `blocked`, not merely "not rotated".
    log(`native log rotate failed at ${logPath}: ${e && e.message}`);
    return { rotated: false, blocked: true, previousPath: null };
  }
}

/**
 * Arm both native-capture channels. Never throws.
 *
 * Must run BEFORE the app is ready: Chromium reads its logging switches during
 * initialization, so appending them later is accepted and then ignored.
 *
 * @param {object} deps
 * @param {string} deps.logsDir              Directory for the log file.
 * @param {(name: string, value: string) => void} deps.appendSwitch
 * @param {(opts: object) => void} [deps.startCrashReporter]
 * @param {object} [deps.fs]                 Injected for the rotate step.
 * @param {(msg: string) => void} [deps.log]
 * @param {object} [deps.env]                Environment for the debug opt-in.
 * @returns {{logPath: string, previousPath: string|null, rotated: boolean, blocked: boolean, switches: string[], crashReporter: boolean}}
 */
function initNativeLogging({
  logsDir,
  appendSwitch,
  startCrashReporter,
  fs,
  log = () => {},
  env = process.env,
} = {}) {
  const logPath = nativeLogPath(logsDir);
  const applied = [];
  let rotated = false;
  let blocked = false;
  let previousPath = null;

  // Before the switches: Chromium opens this path during initialization, so the
  // previous generation has to be moved aside first or it is appended to (or
  // clobbered) instead of preserved.
  if (fs) ({ rotated, blocked, previousPath } = rotateNativeLog(logPath, { fs, log }));

  // Fail SAFE, not fail open. A blocked rotation means the un-rotated live log
  // still holds the session we were trying to preserve — and Chromium's own
  // open mode for `--log-file` is not something this side can pin down, so
  // arming the sink anyway risks it truncating exactly that evidence. Giving up
  // this boot's logging is the cheap loss; destroying the retained crash log to
  // start a fresh one is the expensive one. The minidump channel is unaffected
  // and still armed below, so a crash this boot is not left undocumented.
  if (blocked) {
    log(
      `native logging NOT armed: ${logPath} could not be rotated, so the file ` +
        `sink is skipped this launch rather than risk overwriting it`
    );
  } else {
    for (const [name, value] of nativeLoggingSwitches(logPath, env)) {
      try {
        appendSwitch(name, value);
        applied.push(name);
      } catch (e) {
        // One rejected switch must not cost us the other, nor the boot.
        log(`native logging switch --${name} failed: ${e && e.message}`);
      }
    }
  }

  let crashReporter = false;
  if (typeof startCrashReporter === "function") {
    try {
      startCrashReporter({
        // Mandatory, and the reason this is safe to ship on by default:
        // Kiro Crew does not phone home (website/src/rum.ts is a no-op in the
        // public build), so a dump that left the machine would be a new
        // egress path, not a diagnostic. Dumps stay in the app's own
        // crashDumps directory for the user to hand over deliberately.
        uploadToServer: false,
        compress: false,
      });
      crashReporter = true;
    } catch (e) {
      log(`crashReporter.start failed: ${e && e.message}`);
    }
  }

  log(
    `native logging armed: file=${blocked ? "skipped" : logPath} ` +
      `previous=${previousPath || "none"} ` +
      `switches=${applied.join(",") || "none"} minidumps=${crashReporter}`
  );
  return { logPath, previousPath, rotated, blocked, switches: applied, crashReporter };
}

module.exports = {
  initNativeLogging,
  nativeLogPath,
  previousNativeLogPath,
  nativeLoggingSwitches,
  rotateNativeLog,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
};
