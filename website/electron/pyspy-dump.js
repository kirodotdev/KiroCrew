"use strict";
//
// Best-effort frozen-stack capture for a wedged gateway, taken BEFORE we
// SIGKILL it.
//
// When the liveness monitor declares the backend unresponsive, the asyncio loop
// is stuck on a frame we cannot see: the in-process faulthandler watchdog races
// (and loses to) this very SIGKILL, and a fully-starved loop often cannot dump
// itself anyway. py-spy reads a target's stacks from OUTSIDE via ptrace, so it
// works even on a starved process. We run it on the child PID just before the
// kill and write the all-thread dump next to the gateway log, so the post-
// restart crash report carries the actual frozen frame.
//
// Strictly best-effort: if py-spy is not installed, lacks ptrace permission
// (macOS typically needs sudo), or times out, we log why and return -- capture
// must NEVER block the kill beyond timeoutMs, and never throws.
//
const { execFile } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Electron's PATH under launchd is minimal (no shell profile is sourced), so
// probe the common install locations explicitly before falling back to PATH.
const PYSPY_CANDIDATES = [
  "/opt/homebrew/bin/py-spy",
  "/usr/local/bin/py-spy",
  path.join(os.homedir(), ".cargo", "bin", "py-spy"),
  path.join(os.homedir(), ".local", "bin", "py-spy"),
];

function resolvePySpy(existsSync = fs.existsSync) {
  for (const c of PYSPY_CANDIDATES) {
    try {
      if (existsSync(c)) return c;
    } catch {
      /* ignore unreadable candidate */
    }
  }
  return "py-spy"; // fall back to PATH; ENOENT is handled by the caller
}

function defaultRun(bin, args, timeoutMs, cb) {
  // killSignal SIGKILL (not the default SIGTERM): py-spy attaches to the target
  // via ptrace and can sit in an uninterruptible wait that ignores SIGTERM, so
  // a polite TERM at the deadline may not actually stop it. SIGKILL guarantees
  // the child dies; the JS-side backstop timer below guarantees WE stop waiting
  // even in the pathological case where Node's own timeout doesn't fire.
  execFile(bin, args, { timeout: timeoutMs, killSignal: "SIGKILL", maxBuffer: 16 * 1024 * 1024 }, cb);
}

/**
 * Dump all-thread stacks of `pid` via py-spy and write them under `dumpDir`.
 *
 * @param {object} o
 * @param {number} o.pid              gateway child pid to sample
 * @param {string} o.dumpDir          directory to write the dump into
 * @param {number} [o.timeoutMs=5000] hard cap so a hung py-spy can't delay the kill
 * @param {(msg: string) => void} [o.log]
 * @param {string} [o.bin]            py-spy path (defaults to resolved/PATH)
 * @param {Function} [o.run]          (bin, args, timeoutMs, cb) -- injectable for tests
 * @param {Function} [o.writeFileSync]
 * @param {() => Date} [o.now]
 * @param {(fn: Function, ms: number) => any} [o.setTimeoutFn]
 * @param {(h: any) => void} [o.clearTimeoutFn]
 * @returns {Promise<{ok: boolean, path?: string, reason?: string}>} never rejects
 */
function capturePySpyDump({
  pid,
  dumpDir,
  timeoutMs = 5000,
  log = () => {},
  bin,
  run = defaultRun,
  writeFileSync = fs.writeFileSync,
  now = () => new Date(),
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  return new Promise((resolve) => {
    if (!pid) {
      log("py-spy dump skipped — no gateway pid");
      resolve({ ok: false, reason: "no pid" });
      return;
    }
    const exe = bin || resolvePySpy();
    let settled = false;
    let backstop = null;
    const done = (r) => {
      if (!settled) {
        settled = true;
        if (backstop !== null) clearTimeoutFn(backstop);
        resolve(r);
      }
    };
    // Authoritative JS-side deadline. Node's execFile timeout only SIGNALS the
    // child; if py-spy never receives/honors it (stuck in ptrace), the callback
    // would never fire and `await capturePySpyDump` would hang past timeoutMs,
    // stalling the recovery SIGKILL it runs before. This timer makes the promise
    // resolve regardless, honoring the module's "never block the kill beyond
    // timeoutMs" contract. Grace of 250ms so the normal execFile-timeout path
    // (which reports a specific reason) usually wins this race.
    backstop = setTimeoutFn(() => {
      log(`py-spy dump skipped — exceeded ${timeoutMs}ms (backstop); continuing with kill`);
      done({ ok: false, reason: "timeout" });
    }, timeoutMs + 250);
    if (backstop && typeof backstop.unref === "function") backstop.unref();
    try {
      run(exe, ["dump", "--pid", String(pid), "--nonblocking"], timeoutMs, (err, stdout, stderr) => {
        if (err) {
          const first = (((stderr && stderr.toString()) || err.message || "").split("\n")[0]) || "unknown error";
          const why =
            err.code === "ENOENT"
              ? "py-spy not installed (brew install py-spy)"
              : `py-spy failed: ${first}`;
          log(`py-spy dump skipped — ${why}`);
          done({ ok: false, reason: why });
          return;
        }
        try {
          const ts = now().toISOString().replace(/[:.]/g, "-");
          const outPath = path.join(dumpDir, `py-spy-dump-${pid}-${ts}.txt`);
          writeFileSync(outPath, (stdout && stdout.toString()) || "(empty py-spy output)\n");
          log(`py-spy dump written before kill: ${outPath}`);
          done({ ok: true, path: outPath });
        } catch (e) {
          log(`py-spy dump write failed: ${e && e.message}`);
          done({ ok: false, reason: "write failed" });
        }
      });
    } catch (e) {
      // run() should never throw synchronously, but never let it block the kill.
      log(`py-spy dump invocation threw: ${e && e.message}`);
      done({ ok: false, reason: "invoke threw" });
    }
  });
}

module.exports = { capturePySpyDump, resolvePySpy, PYSPY_CANDIDATES };
