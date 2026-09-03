"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const {
  initNativeLogging,
  nativeLogPath,
  previousNativeLogPath,
  nativeLoggingSwitches,
  rotateNativeLog,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
} = require("../native-logging");

const LIVE = path.join("/logs", NATIVE_LOG_BASENAME);
const PREV = path.join("/logs", NATIVE_LOG_PREVIOUS_BASENAME);

/**
 * fs double over an in-memory file set, recording renames.
 * `present` lists paths that exist; `throwOn` makes renameSync fail.
 */
function fakeFs({ present = [], throwOn = null } = {}) {
  const files = new Set(present);
  const renames = [];
  return {
    files,
    renames,
    existsSync: (p) => files.has(p),
    renameSync(from, to) {
      if (throwOn) throw new Error(throwOn);
      renames.push({ from, to });
      files.delete(from);
      files.add(to);
    },
  };
}

describe("nativeLogPath / previousNativeLogPath", () => {
  it("sits next to the other launch logs in the logs directory", () => {
    assert.equal(nativeLogPath("/logs/Kiro Crew"), path.join("/logs/Kiro Crew", NATIVE_LOG_BASENAME));
  });

  it("keeps the previous generation beside the live file", () => {
    assert.equal(previousNativeLogPath(LIVE), PREV);
  });

  it("does not throw on a missing directory", () => {
    assert.equal(nativeLogPath(undefined), NATIVE_LOG_BASENAME);
  });
});

describe("nativeLoggingSwitches", () => {
  // These are Chromium's spellings, and an unknown switch is IGNORED rather
  // than rejected — so a typo turns logging silently off and this assertion is
  // the only thing standing between that and a shipped no-op.
  it("uses the exact Chromium switch names", () => {
    assert.deepEqual(nativeLoggingSwitches("/tmp/c.log", { KIROCREW_DEBUG: "1" }), [
      ["enable-logging", "file"],
      ["log-file", "/tmp/c.log"],
      // Value-less switch, so the empty string is the whole argument. It makes
      // performance.memory exact and uncached, which is what the renderer memory
      // trajectory reads -- without it those values are bucketized and cached for
      // 20 minutes, and a memory probe reading them returns a plausible constant.
      ["enable-precise-memory-info", ""],
    ]);
  });

  // The bucketization this switch removes is a PRIVACY control, and removing it
  // applies to every renderer in the process — browser panels showing untrusted
  // pages included. A normal install must not widen that side channel just to
  // capture crash logs, so the switch rides the KIROCREW_DEBUG opt-in while the
  // two logging switches (the reason this module exists) stay unconditional.
  it("leaves precise memory info OFF without the debug opt-in", () => {
    assert.deepEqual(nativeLoggingSwitches("/tmp/c.log", {}), [
      ["enable-logging", "file"],
      ["log-file", "/tmp/c.log"],
    ]);
  });

  // Same gate spelling as the profiler: an explicit falsey value is OFF, not
  // "the variable is set, so on".
  it("treats an explicit falsey debug value as off", () => {
    const names = nativeLoggingSwitches("/tmp/c.log", { KIROCREW_DEBUG: "0" }).map(([n]) => n);
    assert.equal(names.includes("enable-precise-memory-info"), false);
  });

  // `--enable-logging` without `=file` leaves output on stderr, which the GUI
  // launch this module exists to compensate for discards again.
  it("routes to the file sink, not stderr", () => {
    const [[, value]] = nativeLoggingSwitches("/tmp/c.log", {});
    assert.equal(value, "file");
  });
});

describe("rotateNativeLog", () => {
  // THE point of the whole rotation step: the run under investigation is not
  // the run doing the investigating. A boot that destroyed the prior session
  // would delete the evidence at the moment someone relaunched to read it.
  it("preserves the previous session instead of discarding it", () => {
    const fs = fakeFs({ present: [LIVE] });
    const out = rotateNativeLog(LIVE, { fs });
    assert.deepEqual(out, { rotated: true, blocked: false, previousPath: PREV });
    assert.deepEqual(fs.renames, [{ from: LIVE, to: PREV }]);
    assert.equal(fs.files.has(PREV), true);
    // Left absent so Chromium starts clean whether it appends or truncates.
    assert.equal(fs.files.has(LIVE), false);
  });

  // Two files, never N: the bound is one generation, so an older previous is
  // replaced rather than accumulated. `renameSync` replaces an existing
  // destination on Windows as well (libuv passes MOVEFILE_REPLACE_EXISTING),
  // which `perf-metrics.js` already depends on for its rolling artifact.
  it("overwrites an older generation instead of accumulating", () => {
    const fs = fakeFs({ present: [LIVE, PREV] });
    assert.equal(rotateNativeLog(LIVE, { fs }).rotated, true);
    assert.deepEqual([...fs.files], [PREV]);
  });

  it("is a no-op on the first launch, when there is nothing to preserve", () => {
    const fs = fakeFs({ present: [] });
    assert.deepEqual(rotateNativeLog(LIVE, { fs }), {
      rotated: false,
      blocked: false,
      previousPath: null,
    });
    assert.deepEqual(fs.renames, []);
  });

  // A Windows sharing violation (any open handle on either path) is the real
  // failure mode, not the destination existing. It must report `blocked`, which
  // is what separates it from the harmless first launch above.
  it("reports blocked when the rename fails, never throwing", () => {
    const fs = fakeFs({ present: [LIVE], throwOn: "EPERM" });
    const lines = [];
    const out = rotateNativeLog(LIVE, { fs, log: (m) => lines.push(m) });
    assert.deepEqual(out, { rotated: false, blocked: true, previousPath: null });
    assert.equal(fs.files.has(LIVE), true, "the live log must survive a failed rotate");
    assert.equal(lines.length, 1);
    assert.match(lines[0], /EPERM/);
  });
});

describe("initNativeLogging", () => {
  function harness(over = {}) {
    const applied = [];
    const started = [];
    const lines = [];
    const fs = over.fs === undefined ? fakeFs({ present: [LIVE] }) : over.fs;
    const result = initNativeLogging({
      logsDir: "/logs",
      appendSwitch: (n, v) => applied.push([n, v]),
      startCrashReporter: (o) => started.push(o),
      log: (m) => lines.push(m),
      // Explicit rather than inherited: these assertions pin the switch SET, so
      // they must not depend on whether the runner's own environment happens to
      // carry KIROCREW_DEBUG.
      env: { KIROCREW_DEBUG: "1" },
      ...over,
      fs,
    });
    return { applied, started, lines, result, fs };
  }

  it("applies every switch and starts the crash reporter", () => {
    const { applied, started, result } = harness();
    assert.deepEqual(applied, [
      ["enable-logging", "file"],
      ["log-file", LIVE],
      ["enable-precise-memory-info", ""],
    ]);
    assert.equal(started.length, 1);
    assert.equal(result.crashReporter, true);
    assert.equal(result.rotated, true);
    assert.equal(result.previousPath, PREV);
    assert.deepEqual(result.switches, ["enable-logging", "log-file", "enable-precise-memory-info"]);
  });

  // The default install: crash logging armed, privacy control intact.
  it("arms logging without the memory switch when debug is off", () => {
    const { applied, result } = harness({ env: {} });
    assert.deepEqual(applied, [
      ["enable-logging", "file"],
      ["log-file", LIVE],
    ]);
    assert.deepEqual(result.switches, ["enable-logging", "log-file"]);
  });

  // The one non-negotiable option: this app does not phone home, so a minidump
  // that left the machine would be a new egress path rather than a diagnostic.
  it("never uploads crash dumps off the machine", () => {
    const { started } = harness();
    assert.equal(started[0].uploadToServer, false);
  });

  // Ordering is load-bearing: Chromium opens the log path during init, so a
  // rotation that ran afterwards would preserve nothing.
  it("rotates before arming the switches", () => {
    const { fs } = harness();
    assert.deepEqual(fs.renames, [{ from: LIVE, to: PREV }]);
    assert.equal(fs.files.has(LIVE), false);
  });

  // A boot-path helper must never be the reason the app fails to start.
  it("survives an appendSwitch that throws, keeping the other switch", () => {
    const { result, lines } = harness({
      appendSwitch: (n) => {
        if (n === "enable-logging") throw new Error("refused");
      },
    });
    assert.deepEqual(result.switches, ["log-file", "enable-precise-memory-info"]);
    assert.ok(lines.some((l) => /refused/.test(l)));
  });

  it("survives a crashReporter that throws", () => {
    const { result, lines } = harness({
      startCrashReporter: () => {
        throw new Error("no dump dir");
      },
    });
    assert.equal(result.crashReporter, false);
    assert.deepEqual(result.switches, ["enable-logging", "log-file", "enable-precise-memory-info"]);
    assert.ok(lines.some((l) => /no dump dir/.test(l)));
  });

  it("still arms logging when no crash reporter is supplied", () => {
    const { result, started } = harness({ startCrashReporter: undefined });
    assert.equal(result.crashReporter, false);
    assert.equal(started.length, 0);
    assert.deepEqual(result.switches, ["enable-logging", "log-file", "enable-precise-memory-info"]);
  });

  it("skips rotation when no fs is supplied", () => {
    const { result } = harness({ fs: null });
    assert.equal(result.rotated, false);
    assert.equal(result.blocked, false);
    assert.equal(result.previousPath, null);
    assert.deepEqual(result.switches, ["enable-logging", "log-file", "enable-precise-memory-info"]);
  });

  // THE fail-safe. A blocked rotation leaves the un-rotated live log holding the
  // session we were trying to preserve, and Chromium's open mode for --log-file
  // is not pinnable from here, so arming the sink could truncate exactly that
  // evidence. Skipping this boot's file logging is the cheaper loss.
  it("does NOT arm the file sink when a needed rotation failed", () => {
    const fs = fakeFs({ present: [LIVE], throwOn: "EPERM" });
    const { applied, result, lines } = harness({ fs });
    assert.equal(result.blocked, true);
    assert.deepEqual(applied, [], "no logging switch may point at an unrotated log");
    assert.deepEqual(result.switches, []);
    assert.equal(fs.files.has(LIVE), true, "the retained evidence must still be on disk");
    assert.ok(lines.some((l) => /NOT armed/.test(l)));
  });

  // Minidumps go to their own directory and are unaffected by the log file, so a
  // blocked rotation must not leave a crash this boot completely undocumented.
  it("still arms minidumps when the file sink is skipped", () => {
    const { started, result } = harness({ fs: fakeFs({ present: [LIVE], throwOn: "EPERM" }) });
    assert.equal(result.blocked, true);
    assert.equal(result.crashReporter, true);
    assert.equal(started.length, 1);
    assert.equal(started[0].uploadToServer, false);
  });

  it("names the skip in the verdict line rather than a file it did not arm", () => {
    const { lines } = harness({ fs: fakeFs({ present: [LIVE], throwOn: "EPERM" }) });
    const verdict = lines.find((l) => /native logging armed/.test(l));
    assert.match(verdict, /file=skipped/);
    assert.match(verdict, /switches=none/);
    assert.match(verdict, /minidumps=true/);
  });

  it("logs a one-line verdict naming both generations", () => {
    const { lines } = harness();
    const verdict = lines.find((l) => /native logging armed/.test(l));
    assert.ok(verdict, "expected an armed verdict line");
    assert.match(verdict, /chromium\.log/);
    assert.match(verdict, /chromium\.previous\.log/);
    assert.match(verdict, /minidumps=true/);
  });

  it("names no previous generation on a first launch", () => {
    const { lines, result } = harness({ fs: fakeFs({ present: [] }) });
    assert.equal(result.previousPath, null);
    assert.match(
      lines.find((l) => /native logging armed/.test(l)),
      /previous=none/
    );
  });
});

// main.js is not loadable under the unit runner (it requires `electron`), so the
// call-site ORDER is asserted against its source. This is not a style check: the
// order is the whole correctness of the rotation.
describe("main.js call-site ordering", () => {
  const mainSrc = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");

  // A rejected second instance must never reach initNativeLogging. If it did, it
  // would rename chromium.log out from under the RUNNING primary — whose open fd
  // follows the renamed inode — and destroy the genuine previous generation, so
  // double-clicking the icon of an already-running app would wipe exactly the
  // evidence this capture exists to retain. `app.exit(0)` in the lock-lost branch
  // is synchronous, so being inside the else-branch is what makes that
  // unreachable.
  it("arms logging only after the single-instance lock is won", () => {
    const lock = mainSrc.indexOf("app.requestSingleInstanceLock()");
    assert.ok(lock > 0, "expected a single-instance lock call in main.js");
    // Anchoring on the lock CALL alone is not enough: `arm > lock` also holds
    // when the arming sits INSIDE the lock-lost branch, which is precisely the
    // defect. The branch boundary is the real constraint, so assert past the
    // `} else {` that opens the lock-won branch.
    const elseAt = mainSrc.indexOf("} else {", lock);
    const exitAt = mainSrc.indexOf("app.exit(0)", lock);
    const arm = mainSrc.indexOf("initNativeLogging({");
    assert.ok(elseAt > lock, "expected a lock-won else branch after the lock call");
    assert.ok(exitAt > lock && exitAt < elseAt, "expected app.exit(0) in the lock-lost branch");
    assert.ok(arm > 0, "expected an initNativeLogging call in main.js");
    assert.ok(
      arm > elseAt,
      "initNativeLogging must be called inside the lock-WON branch. After the " +
        "lock call is not sufficient: from the lock-lost branch a rejected " +
        "second instance still rotates the primary's live log"
    );
  });

  // The other half of the same constraint: Chromium reads its logging switches
  // during initialization, so arming after app-ready is accepted and then
  // silently ignored — logging would simply never happen.
  it("arms logging before the app becomes ready", () => {
    const arm = mainSrc.indexOf("initNativeLogging({");
    // `app.whenReady().then(` and not a bare `app.whenReady()`: the bare form
    // also appears in prose comments, and matching one of those would let this
    // assertion pass on an arming call that had moved after the real handler.
    const ready = mainSrc.indexOf("app.whenReady().then(");
    assert.ok(ready > 0, "expected an app.whenReady().then( call in main.js");
    assert.ok(
      arm < ready,
      "initNativeLogging must be called BEFORE app.whenReady(), or Chromium " +
        "ignores the logging switches"
    );
  });
});
