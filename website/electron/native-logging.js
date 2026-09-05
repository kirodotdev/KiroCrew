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

/** Mode for a file that may contain a bearer credential: owner read/write only. */
const SECRET_FILE_MODE = 0o600;

/**
 * Redaction cap. Reading the file to rewrite it costs its size in memory, and
 * this runs during boot on a host that may already be under pressure. One
 * session of Chromium logging is small (see `rotateNativeLog`) — a file past
 * this bound means something looped, and the honest trade is to keep the
 * evidence unredacted rather than risk an out-of-memory at launch.
 */
const MAX_REDACT_BYTES = 32 * 1024 * 1024;

/**
 * Query-string bearer tokens, as Chromium writes them.
 *
 * Chromium's `INFO:CONSOLE` lines append the document URL, and the desktop app
 * loads the dashboard as `?token=<jwt>` — so every renderer console message from
 * that document records the session token verbatim. The value ends at the next
 * URL/log delimiter; `&` matters because the real URL continues with `&sid=`.
 *
 * Deliberately narrow: only the query parameter shape that was observed. A
 * broad "anything JWT-shaped" pattern would start eating legitimate log content.
 */
const TOKEN_QUERY_RE = /([?&](?:token|access_token)=)[^&\s"'`)\]]+/gi;

/**
 * Replace query-string token values with a marker, preserving everything else.
 *
 * Pure so the pattern is testable without touching a filesystem. The marker is
 * left in place of the value rather than removing the parameter, so a log reader
 * can still see that a tokened URL was involved.
 */
function redactTokensInText(text) {
  return String(text == null ? "" : text).replace(TOKEN_QUERY_RE, "$1[REDACTED]");
}

/**
 * Best-effort `chmod` to {@link SECRET_FILE_MODE}. Never throws.
 *
 * Separate from creation because it applies to BOTH generations: the file this
 * boot creates and the one the previous boot left behind at an inherited 0644.
 * On Windows the POSIX mode is largely advisory — the call is still made rather
 * than platform-gated, because it is harmless there and gating it would be one
 * more branch that only ever runs on one OS.
 */
function tightenLogMode(filePath, { fs, log = () => {} } = {}) {
  if (!fs || typeof fs.chmodSync !== "function") return false;
  try {
    fs.chmodSync(filePath, SECRET_FILE_MODE);
    return true;
  } catch (e) {
    log(`native log chmod failed at ${filePath}: ${e && e.message}`);
    return false;
  }
}

/**
 * Strip credentials from a log file in place, and tighten its mode.
 *
 * Only ever called on the ROTATED generation, which this process owns outright
 * after `renameSync` — the live file belongs to Chromium's own file handle and
 * rewriting under it would race the writer. That split is why mode bits alone
 * are not enough: they stop another local account reading the file, but the
 * retained log is also the artifact users are asked to attach to a bug report,
 * and that copy has to be clean on its own.
 *
 * Fail-soft in every direction, matching this module's posture: losing a
 * redaction pass is worth a log line, never a failed launch. An `fs` double
 * without the read/write/stat members simply opts out.
 */
function redactNativeLogSecrets(filePath, { fs, log = () => {} } = {}) {
  if (!fs || typeof fs.readFileSync !== "function" || typeof fs.writeFileSync !== "function") {
    return { scanned: false, redacted: false, skipped: "unsupported-fs" };
  }
  try {
    if (typeof fs.statSync === "function") {
      const size = Number(fs.statSync(filePath).size);
      if (Number.isFinite(size) && size > MAX_REDACT_BYTES) {
        log(`native log redaction skipped at ${filePath}: ${size} bytes over cap`);
        return { scanned: false, redacted: false, skipped: "too-large" };
      }
    }
    const raw = fs.readFileSync(filePath, "utf8");
    const cleaned = redactTokensInText(raw);
    if (cleaned === raw) return { scanned: true, redacted: false, skipped: null };
    // Atomic replace, NOT an in-place rewrite. `writeFileSync` truncates before
    // it writes, so a write that fails partway (ENOSPC, EIO, a Windows sharing
    // violation) would leave the retained log partial or empty — destroying the
    // one generation this module exists to preserve. An unredacted log is a
    // hygiene problem; a truncated one is the loss of the crash evidence, which
    // is strictly worse. So: write a sibling at the tight mode, then rename over
    // the original only once the write returned. `renameSync` carries the temp
    // file's mode across, so the replacement is owner-only by construction.
    // Mirrors `src/kiro_crew/atomic_write.py`, already cited by the rotation
    // comment above for the same class of failure.
    // The temp path is predictable, so the write has to be exclusive-create:
    // `writeFileSync`'s default `w` follows a symlink planted at that path and
    // would land this log's contents on the link's target. `wx` (O_EXCL) refuses
    // any pre-existing entry, symlink included, which is the same reason
    // `createTightLogFile` below opens with `wx` rather than `w`. A stale temp
    // from a crashed earlier pass would otherwise disable redaction forever, so
    // EEXIST gets exactly one retry: unlink removes the entry itself — never
    // whatever it points at — and the retry is still exclusive, so an attacker
    // re-planting inside that window loses the race into the skip path below
    // instead of winning a write.
    const tmpPath = `${filePath}.redact.tmp`;
    try {
      try {
        fs.writeFileSync(tmpPath, cleaned, { mode: SECRET_FILE_MODE, flag: "wx" });
      } catch (e) {
        if (!e || e.code !== "EEXIST" || typeof fs.unlinkSync !== "function") throw e;
        fs.unlinkSync(tmpPath);
        fs.writeFileSync(tmpPath, cleaned, { mode: SECRET_FILE_MODE, flag: "wx" });
      }
      fs.renameSync(tmpPath, filePath);
    } catch (e) {
      // The original is untouched on this path — that is the whole point of the
      // sibling. Clean up the partial temp so it cannot be mistaken for a log,
      // and report the miss rather than raising: this runs during boot.
      try {
        if (typeof fs.unlinkSync === "function") fs.unlinkSync(tmpPath);
      } catch {
        /* the temp may never have been created; nothing to clean up */
      }
      log(`native log redaction not applied at ${filePath}: ${e && e.message}`);
      return { scanned: true, redacted: false, skipped: "replace-failed" };
    }
    return { scanned: true, redacted: true, skipped: null };
  } catch (e) {
    log(`native log redaction failed at ${filePath}: ${e && e.message}`);
    return { scanned: false, redacted: false, skipped: "error" };
  }
}

/**
 * Pre-create the live log with a tight mode, so Chromium opens an inode that is
 * already owner-only.
 *
 * Chromium creates `--log-file` itself at the process's default umask, which on
 * a normal macOS install is 0644 — world-readable, for a file that records the
 * dashboard session token on every renderer console line. Nothing on this side
 * can filter what Chromium writes into that handle, so the mode has to be set
 * on the inode BEFORE Chromium opens it.
 *
 * `wx` (fail-if-exists) rather than `w`: truncating here would destroy exactly
 * the evidence `rotateNativeLog` just went to the trouble of preserving in the
 * blocked-rotation case. Creating the file empty keeps the module's existing
 * "starts clean either way" property intact — Chromium appending to a
 * zero-length file and truncating one are indistinguishable in the result.
 *
 * Never throws. An EEXIST still falls through to the `chmod`, which is the
 * upgrade path for a log left at 0644 by an earlier build.
 */
function createTightLogFile(logPath, { fs, log = () => {} } = {}) {
  if (!fs) return { created: false, tightened: false };
  let created = false;
  try {
    if (typeof fs.openSync === "function" && typeof fs.closeSync === "function") {
      fs.closeSync(fs.openSync(logPath, "wx", SECRET_FILE_MODE));
      created = true;
    } else if (typeof fs.writeFileSync === "function") {
      fs.writeFileSync(logPath, "", { flag: "wx", mode: SECRET_FILE_MODE });
      created = true;
    }
  } catch (e) {
    if (!e || e.code !== "EEXIST") {
      log(`native log pre-create failed at ${logPath}: ${e && e.message}`);
      return { created: false, tightened: tightenLogMode(logPath, { fs, log }) };
    }
  }
  return { created, tightened: tightenLogMode(logPath, { fs, log }) };
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
    // The retained generation is ours now, and it is the copy users hand over.
    // Strip credentials from it and tighten its mode before anything reads it.
    redactNativeLogSecrets(previousPath, { fs, log });
    tightenLogMode(previousPath, { fs, log });
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

  // Chromium creates `--log-file` at the process umask (0644 on a normal macOS
  // install) and records the dashboard session token on every renderer console
  // line, so the inode has to be owner-only BEFORE Chromium opens it. Skipped
  // when rotation was blocked: the un-rotated file still holds the session we
  // are trying to preserve, and the sink is not armed for it either (below).
  if (fs && !blocked) createTightLogFile(logPath, { fs, log });

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
  redactTokensInText,
  redactNativeLogSecrets,
  createTightLogFile,
  tightenLogMode,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
  SECRET_FILE_MODE,
  MAX_REDACT_BYTES,
};
