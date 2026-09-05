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
  redactTokensInText,
  redactNativeLogSecrets,
  createTightLogFile,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
  SECRET_FILE_MODE,
  MAX_REDACT_BYTES,
} = require("../native-logging");

const LIVE = path.join("/logs", NATIVE_LOG_BASENAME);
const PREV = path.join("/logs", NATIVE_LOG_PREVIOUS_BASENAME);

/**
 * fs double over an in-memory file set, recording renames.
 * `present` lists paths that exist; `throwOn` makes renameSync fail.
 *
 * Also models the three things the credential-hygiene path needs: file CONTENT
 * (so a redaction can be observed), file MODE (so a tightening can be), and
 * `wx` create semantics on BOTH `openSync` and `writeFileSync` (so "must not
 * truncate an existing log" and "must not follow a planted temp path" are
 * testable). Every write is recorded in `writes` with its flag, so the
 * exclusive-create contract can be asserted directly rather than inferred.
 * `contents` seeds bodies for paths in `present`; `throwReadOn` makes a read of
 * that path fail, which is the fail-soft case.
 */
function fakeFs({
  present = [],
  throwOn = null,
  contents = {},
  throwReadOn = null,
  throwWriteOn = null,
} = {}) {
  const files = new Set(present);
  const renames = [];
  const unlinked = [];
  const writes = [];
  const body = new Map(Object.entries(contents));
  const modes = new Map();
  for (const p of present) modes.set(p, 0o644); // what Chromium's umask leaves
  return {
    files,
    renames,
    unlinked,
    writes,
    body,
    modes,
    existsSync: (p) => files.has(p),
    renameSync(from, to) {
      if (throwOn) throw new Error(throwOn);
      renames.push({ from, to });
      files.delete(from);
      files.add(to);
      if (body.has(from)) {
        body.set(to, body.get(from));
        body.delete(from);
      }
      if (modes.has(from)) {
        modes.set(to, modes.get(from));
        modes.delete(from);
      }
    },
    unlinkSync(p) {
      unlinked.push(p);
      files.delete(p);
      body.delete(p);
      modes.delete(p);
    },
    openSync(p, flag, mode) {
      if (String(flag).includes("x") && files.has(p)) {
        const err = new Error(`EEXIST: file already exists, open '${p}'`);
        err.code = "EEXIST";
        throw err;
      }
      files.add(p);
      body.set(p, "");
      if (mode !== undefined) modes.set(p, mode);
      return 1;
    },
    closeSync() {},
    chmodSync(p, mode) {
      modes.set(p, mode);
    },
    statSync(p) {
      return { size: Buffer.byteLength(body.get(p) || "", "utf8") };
    },
    readFileSync(p) {
      if (throwReadOn && p === throwReadOn) throw new Error("EIO");
      return body.get(p) || "";
    },
    writeFileSync(p, data, opts) {
      if (throwWriteOn && p === throwWriteOn) throw new Error("ENOSPC");
      const flag = opts && opts.flag !== undefined ? String(opts.flag) : "w";
      writes.push({ path: p, flag, mode: opts && opts.mode });
      if (flag.includes("x") && files.has(p)) {
        const err = new Error(`EEXIST: file already exists, open '${p}'`);
        err.code = "EEXIST";
        throw err;
      }
      files.add(p);
      body.set(p, String(data));
      if (opts && opts.mode !== undefined && !modes.has(p)) modes.set(p, opts.mode);
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
  });

  // The live path is deliberately re-created after the rotation (empty, 0600) so
  // Chromium opens an inode that is already owner-only. Before that, Chromium
  // created it at the process umask — 0644 for a file that records the session
  // token on every renderer console line.
  it("leaves the live log pre-created at an owner-only mode", () => {
    const { fs } = harness();
    assert.equal(fs.files.has(LIVE), true, "Chromium must open an inode we already tightened");
    assert.equal(fs.modes.get(LIVE), 0o600);
    assert.equal(fs.body.get(LIVE), "", "pre-creation must not add content of its own");
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

  // The pre-create is skipped on the same fail-safe reasoning as the sink: the
  // un-rotated file still holds the session we were trying to preserve, so this
  // boot must not open it, tighten it, or write a byte into it.
  it("does not pre-create or rewrite the live log when rotation was blocked", () => {
    const fs = fakeFs({
      present: [LIVE],
      contents: { [LIVE]: "prior session evidence" },
      throwOn: "EPERM",
    });
    const { result } = harness({ fs });
    assert.equal(result.blocked, true);
    assert.equal(fs.body.get(LIVE), "prior session evidence");
    assert.equal(fs.modes.get(LIVE), 0o644, "an untouched file keeps the mode it had");
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

// A real line, as Chromium writes it: the token is not something the app logs
// deliberately — `INFO:CONSOLE` appends the document URL, and the desktop app
// loads the dashboard as `?token=<jwt>`, so every renderer console message from
// that document records the session token.
const TOKENED_LINE =
  '[123:0828/122558.487579:INFO:CONSOLE:0] "ResizeObserver loop completed with undelivered ' +
  'notifications.", source: http://localhost:5476/chat/gateway?token=eyJzdWIiOiJVMEJRQTNYUkI4RCJ9' +
  ".MgrpIEuHoX7bVuGcQSmK7xqK4tytvtrZiXvy7NtEesw&sid=chat-155-1787945035 (0)";

describe("redactTokensInText", () => {
  it("replaces the token value and stops at the next parameter", () => {
    const out = redactTokensInText(TOKENED_LINE);
    assert.match(out, /\?token=\[REDACTED\]&sid=chat-155-1787945035/);
    assert.ok(!/eyJzdWIi/.test(out), "no part of the JWT may survive");
  });

  it("keeps everything around the token intact", () => {
    const out = redactTokensInText(TOKENED_LINE);
    assert.match(out, /INFO:CONSOLE:0/);
    assert.match(out, /ResizeObserver loop completed/);
    assert.match(out, /http:\/\/localhost:5476\/chat\/gateway/);
  });

  it("redacts every occurrence, not just the first", () => {
    const out = redactTokensInText("a?token=AAA b\nc?token=BBB d");
    assert.equal(out, "a?token=[REDACTED] b\nc?token=[REDACTED] d");
  });

  it("covers the &token= and access_token= spellings", () => {
    assert.equal(redactTokensInText("x?a=1&token=ZZZ"), "x?a=1&token=[REDACTED]");
    assert.equal(redactTokensInText("x?access_token=ZZZ"), "x?access_token=[REDACTED]");
  });

  // The pattern is deliberately narrow. A broad "anything JWT-shaped" rule would
  // start eating legitimate log content, which is the opposite of the goal.
  it("leaves a line with no query token untouched", () => {
    const line = "[1:0828/1:INFO:CONSOLE:0] plain message, source: http://localhost:5476/ (0)";
    assert.equal(redactTokensInText(line), line);
  });

  it("does not throw on nullish input", () => {
    assert.equal(redactTokensInText(null), "");
    assert.equal(redactTokensInText(undefined), "");
  });
});

describe("redactNativeLogSecrets", () => {
  const TMP = `${PREV}.redact.tmp`;

  it("rewrites the retained log in place and tightens its mode", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    const out = redactNativeLogSecrets(PREV, { fs });
    assert.deepEqual(out, { scanned: true, redacted: true, skipped: null });
    assert.match(fs.body.get(PREV), /token=\[REDACTED\]/);
    assert.ok(!/eyJzdWIi/.test(fs.body.get(PREV)));
  });

  // NOT an in-place rewrite: writeFileSync truncates first, so a partial write
  // would destroy the one retained generation. The sibling+rename is what makes
  // the failure path lose the redaction instead of the evidence.
  it("goes through an owner-only sibling and renames over the original", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    redactNativeLogSecrets(PREV, { fs });
    assert.deepEqual(fs.renames, [{ from: TMP, to: PREV }]);
    assert.equal(fs.modes.get(PREV), SECRET_FILE_MODE, "the mode rides across the rename");
    assert.equal(fs.files.has(TMP), false, "no temp may be left behind");
  });

  it("leaves the original intact when the sibling write fails", () => {
    const fs = fakeFs({
      present: [PREV],
      contents: { [PREV]: TOKENED_LINE },
      throwWriteOn: TMP,
    });
    const lines = [];
    const out = redactNativeLogSecrets(PREV, { fs, log: (m) => lines.push(m) });
    assert.equal(out.skipped, "replace-failed");
    assert.equal(out.redacted, false);
    assert.equal(fs.body.get(PREV), TOKENED_LINE, "the retained evidence must survive verbatim");
    assert.deepEqual(fs.renames, [], "nothing may be renamed over the original");
    assert.ok(lines.some((l) => /not applied/.test(l)));
  });

  it("cleans up the temp when the rename fails", () => {
    const fs = fakeFs({
      present: [PREV],
      contents: { [PREV]: TOKENED_LINE },
      throwOn: "EPERM",
    });
    const out = redactNativeLogSecrets(PREV, { fs });
    assert.equal(out.skipped, "replace-failed");
    assert.equal(fs.body.get(PREV), TOKENED_LINE);
    assert.ok(fs.unlinked.includes(TMP), "a partial temp must not be left looking like a log");
  });

  // The temp path is derived from the log path, so it is predictable. A default
  // `w` write would follow a symlink planted there and land log contents on the
  // link's target; O_EXCL is what refuses that.
  it("creates the temp exclusively so a planted path is refused, not followed", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    redactNativeLogSecrets(PREV, { fs });
    const tmpWrites = fs.writes.filter((w) => w.path === TMP);
    assert.ok(tmpWrites.length > 0, "the redaction must go through the temp");
    for (const w of tmpWrites) {
      assert.equal(w.flag, "wx", "every temp write must be exclusive-create");
      assert.equal(w.mode, SECRET_FILE_MODE);
    }
  });

  // Exclusive create alone would let one crashed pass disable redaction forever,
  // so EEXIST gets a single retry. Unlink removes the entry itself, never what it
  // points at, and the retry is still exclusive.
  it("clears a stale temp and retries rather than abandoning the redaction", () => {
    const fs = fakeFs({ present: [PREV, TMP], contents: { [PREV]: TOKENED_LINE } });
    const out = redactNativeLogSecrets(PREV, { fs });
    assert.deepEqual(out, { scanned: true, redacted: true, skipped: null });
    assert.ok(fs.unlinked.includes(TMP), "the stale entry must be removed, not written through");
    assert.equal(fs.writes.filter((w) => w.path === TMP && w.flag === "wx").length, 2);
    assert.match(fs.body.get(PREV), /token=\[REDACTED\]/);
    assert.equal(fs.modes.get(PREV), SECRET_FILE_MODE);
  });

  it("keeps the log verbatim when the temp cannot be created exclusively", () => {
    const fs = fakeFs({ present: [PREV, TMP], contents: { [PREV]: TOKENED_LINE } });
    delete fs.unlinkSync; // no way to clear the blocking entry, so no retry is possible
    const out = redactNativeLogSecrets(PREV, { fs });
    assert.equal(out.skipped, "replace-failed");
    assert.equal(out.redacted, false);
    assert.equal(fs.body.get(PREV), TOKENED_LINE, "refusing to write beats writing somewhere else");
    assert.deepEqual(fs.renames, []);
  });

  it("does not rewrite a log that carries no credential", () => {
    const clean = "[1:0828/1:INFO:CONSOLE:0] nothing to see, source: http://localhost:5476/ (0)";
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: clean } });
    assert.deepEqual(redactNativeLogSecrets(PREV, { fs }), {
      scanned: true,
      redacted: false,
      skipped: null,
    });
    assert.equal(fs.body.get(PREV), clean, "an untouched log must stay byte-identical");
  });

  // Reading the file costs its size in memory, at boot, on a host that may
  // already be under pressure. Past the cap the honest trade is unredacted
  // evidence over an out-of-memory launch.
  it("skips a log past the size cap rather than reading it into memory", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    fs.statSync = () => ({ size: MAX_REDACT_BYTES + 1 });
    const lines = [];
    const out = redactNativeLogSecrets(PREV, { fs, log: (m) => lines.push(m) });
    assert.equal(out.skipped, "too-large");
    assert.equal(out.redacted, false);
    assert.ok(lines.some((l) => /over cap/.test(l)));
  });

  // Boot-path posture: losing a redaction pass is worth a log line, never a
  // failed launch.
  it("survives an unreadable log without throwing", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE }, throwReadOn: PREV });
    const lines = [];
    const out = redactNativeLogSecrets(PREV, { fs, log: (m) => lines.push(m) });
    assert.equal(out.skipped, "error");
    assert.ok(lines.some((l) => /redaction failed/.test(l)));
  });

  it("opts out of an fs double that cannot read or write", () => {
    const out = redactNativeLogSecrets(PREV, { fs: { existsSync: () => true } });
    assert.equal(out.skipped, "unsupported-fs");
  });
});

describe("createTightLogFile", () => {
  it("creates the log empty at owner-only mode", () => {
    const fs = fakeFs({ present: [] });
    const out = createTightLogFile(LIVE, { fs });
    assert.deepEqual(out, { created: true, tightened: true });
    assert.equal(fs.modes.get(LIVE), SECRET_FILE_MODE);
    assert.equal(fs.body.get(LIVE), "");
  });

  // `wx`, not `w`. Truncating here would destroy exactly the evidence
  // rotateNativeLog preserves in the blocked-rotation case.
  it("never truncates an existing log, but still tightens it", () => {
    const fs = fakeFs({ present: [LIVE], contents: { [LIVE]: "prior session evidence" } });
    const out = createTightLogFile(LIVE, { fs });
    assert.equal(out.created, false, "an existing file must not be re-created");
    assert.equal(fs.body.get(LIVE), "prior session evidence");
    assert.equal(fs.modes.get(LIVE), SECRET_FILE_MODE, "0644 left by an older build is upgraded");
  });

  it("survives an fs whose create fails, and still reports the chmod attempt", () => {
    const lines = [];
    const fs = fakeFs({ present: [] });
    fs.openSync = () => {
      throw new Error("EROFS");
    };
    const out = createTightLogFile(LIVE, { fs, log: (m) => lines.push(m) });
    assert.equal(out.created, false);
    assert.ok(lines.some((l) => /pre-create failed/.test(l)));
  });

  it("does nothing at all without an fs", () => {
    assert.deepEqual(createTightLogFile(LIVE, {}), { created: false, tightened: false });
  });
});

describe("rotateNativeLog credential hygiene", () => {
  it("redacts and tightens the generation it just took ownership of", () => {
    const fs = fakeFs({ present: [LIVE], contents: { [LIVE]: TOKENED_LINE } });
    assert.equal(rotateNativeLog(LIVE, { fs }).rotated, true);
    assert.match(fs.body.get(PREV), /token=\[REDACTED\]/);
    assert.equal(fs.modes.get(PREV), SECRET_FILE_MODE);
  });

  // The live file belongs to Chromium's own open handle; rewriting under it would
  // race the writer. Only the renamed copy is ours.
  it("leaves the live file alone when there is nothing to rotate", () => {
    const fs = fakeFs({ present: [], contents: {} });
    assert.equal(rotateNativeLog(LIVE, { fs }).rotated, false);
    assert.deepEqual(fs.renames, []);
  });

  it("does not touch the retained log when a needed rotation was blocked", () => {
    const fs = fakeFs({
      present: [LIVE],
      contents: { [LIVE]: TOKENED_LINE },
      throwOn: "EPERM",
    });
    assert.equal(rotateNativeLog(LIVE, { fs }).blocked, true);
    assert.equal(fs.body.get(LIVE), TOKENED_LINE, "the evidence must survive untouched");
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
