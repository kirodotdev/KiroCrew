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
  redactTokensInBuffer,
  redactNativeLogSecrets,
  createTightLogFile,
  tightenLogMode,
  openRegularFile,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
  SECRET_FILE_MODE,
  MAX_REDACT_BYTES,
  REDACT_CHUNK_BYTES,
} = require("../native-logging");

const LIVE = path.join("/logs", NATIVE_LOG_BASENAME);
const PREV = path.join("/logs", NATIVE_LOG_PREVIOUS_BASENAME);

/**
 * fs double over an in-memory file set, recording renames.
 * `present` lists paths that exist; `throwOn` makes renameSync fail.
 *
 * Also models the things the credential-hygiene path needs: file CONTENT (so a
 * redaction can be observed), file MODE (so a tightening can be), `wx` create
 * semantics on BOTH `openSync` and `writeFileSync` (so "must not truncate an
 * existing log" and "must not follow a planted temp path" are testable),
 * DESCRIPTORS with per-fd read positions (so the streaming pass and
 * `fchmodSync` can be driven), and SYMLINKS via `links` — an entry there is
 * visible to `lstatSync` as a non-regular file, which is what the symlink guard
 * has to reject. Every write is recorded in `writes` with its flag, so the
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
  links = {},
  shortWriteTo = null,
} = {}) {
  const files = new Set(present);
  const renames = [];
  const unlinked = [];
  const writes = [];
  const body = new Map(Object.entries(contents));
  const modes = new Map();
  const symlinks = new Map(Object.entries(links));
  const inos = new Map();
  const handles = new Map();
  let nextIno = 1;
  let nextFd = 10;
  for (const p of present) modes.set(p, 0o644); // what Chromium's umask leaves
  for (const p of present) inos.set(p, nextIno++);
  for (const p of symlinks.keys()) files.add(p);

  const enoent = (p) => {
    const err = new Error(`ENOENT: no such file or directory, open '${p}'`);
    err.code = "ENOENT";
    return err;
  };
  const statFor = (p, isFile) => ({
    size: Buffer.byteLength(body.get(p) || "", "latin1"),
    ino: inos.get(p) || 0,
    isFile: () => isFile,
    isSymbolicLink: () => !isFile,
  });

  const api = {
    files,
    renames,
    unlinked,
    writes,
    body,
    modes,
    symlinks,
    inos,
    handles,
    existsSync: (p) => files.has(p),
    renameSync(from, to) {
      if (throwOn) throw new Error(throwOn);
      renames.push({ from, to });
      files.delete(from);
      files.add(to);
      for (const map of [body, modes, inos]) {
        if (map.has(from)) {
          map.set(to, map.get(from));
          map.delete(from);
        }
      }
      // A rename MOVES a symlink rather than resolving it, which is exactly how a
      // planted live-log link becomes the retained generation.
      if (symlinks.has(from)) {
        symlinks.set(to, symlinks.get(from));
        symlinks.delete(from);
      }
    },
    unlinkSync(p) {
      unlinked.push(p);
      files.delete(p);
      body.delete(p);
      modes.delete(p);
      inos.delete(p);
      symlinks.delete(p);
    },
    openSync(p, flag, mode) {
      const f = String(flag);
      const creating = f.includes("w") || f.includes("a") || f.includes("x");
      if (f.includes("x") && files.has(p)) {
        const err = new Error(`EEXIST: file already exists, open '${p}'`);
        err.code = "EEXIST";
        throw err;
      }
      if (!creating && !files.has(p)) throw enoent(p);
      if (creating) {
        files.add(p);
        body.set(p, "");
        if (!inos.has(p)) inos.set(p, nextIno++);
        if (mode !== undefined) modes.set(p, mode);
      }
      const fd = nextFd++;
      handles.set(fd, { path: p, pos: 0, buf: Buffer.from(body.get(p) || "", "latin1") });
      return fd;
    },
    closeSync(fd) {
      handles.delete(fd);
    },
    lstatSync(p) {
      if (symlinks.has(p)) return statFor(p, false);
      if (!files.has(p)) throw enoent(p);
      return statFor(p, true);
    },
    statSync(p) {
      if (!files.has(p) && !symlinks.has(p)) throw enoent(p);
      return statFor(p, true);
    },
    fstatSync(fd) {
      const h = handles.get(fd);
      if (!h) throw new Error("EBADF");
      return statFor(h.path, true);
    },
    fchmodSync(fd, mode) {
      const h = handles.get(fd);
      if (!h) throw new Error("EBADF");
      modes.set(h.path, mode);
    },
    readSync(fd, buffer, offset, length) {
      const h = handles.get(fd);
      if (!h) throw new Error("EBADF");
      if (throwReadOn && h.path === throwReadOn) throw new Error("EIO");
      const n = h.buf.copy(buffer, offset, h.pos, Math.min(h.pos + length, h.buf.length));
      h.pos += n;
      return n;
    },
    writeSync(fd, data, offset, length) {
      const h = handles.get(fd);
      if (!h) throw new Error("EBADF");
      if (throwWriteOn && h.path === throwWriteOn) throw new Error("ENOSPC");
      // Mirrors the real signature: a Buffer plus an offset/length window, and a
      // return count that MAY be short. `shortWriteTo` makes it short on purpose.
      let text;
      let count;
      if (Buffer.isBuffer(data)) {
        const start = offset || 0;
        const span = length === undefined ? data.length - start : length;
        const allowed = shortWriteTo === h.path ? Math.max(1, Math.floor(span / 2)) : span;
        // latin1 throughout the double, so every byte maps 1:1 to one code unit and
        // a non-UTF-8 byte survives the in-memory representation unchanged. A utf8
        // round-trip here would corrupt exactly what the byte-exactness test checks.
        text = data.subarray(start, start + allowed).toString("latin1");
        count = allowed;
      } else {
        text = String(data);
        count = Buffer.byteLength(text, "latin1");
      }
      writes.push({ path: h.path, flag: "fd", mode: modes.get(h.path), bytes: count });
      body.set(h.path, (body.get(h.path) || "") + text);
      return count;
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
      body.set(p, Buffer.isBuffer(data) ? data.toString("latin1") : String(data));
      if (!inos.has(p)) inos.set(p, nextIno++);
      if (opts && opts.mode !== undefined && !modes.has(p)) modes.set(p, opts.mode);
    },
  };
  return api;
}

/**
 * Make a double report an over-cap size on the DESCRIPTOR, which is where the
 * redaction path reads it from. Preserves `ino` and `isFile` so the guarded open's
 * identity check still passes — overriding the whole stat would refuse instead.
 */
function forceOverCap(fsDouble) {
  const real = fsDouble.fstatSync.bind(fsDouble);
  fsDouble.fstatSync = (fd) => ({ ...real(fd), size: MAX_REDACT_BYTES + 1 });
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
    assert.deepEqual(nativeLoggingSwitches("/tmp/c.log"), [
      ["enable-logging", "file"],
      ["log-file", "/tmp/c.log"],
      // Value-less switch, so the empty string is the whole argument. It makes
      // performance.memory exact and uncached, which is what the renderer memory
      // trajectory reads -- without it those values are bucketized and cached for
      // 20 minutes, and a memory probe reading them returns a plausible constant.
      ["enable-precise-memory-info", ""],
    ]);
  });

  // `--enable-logging` without `=file` leaves output on stderr, which the GUI
  // launch this module exists to compensate for discards again.
  it("routes to the file sink, not stderr", () => {
    const [[, value]] = nativeLoggingSwitches("/tmp/c.log");
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
    assert.deepEqual(out, {
      rotated: true,
      blocked: false,
      previousPath: PREV,
      redacted: true,
    });
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
    assert.deepEqual(out, {
      rotated: false,
      blocked: true,
      previousPath: null,
      redacted: false,
    });
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

  // The pre-create only holds if Chromium opens the inode it created. This side
  // cannot pin down Chromium's open mode for `--log-file`, and one of the
  // possibilities is unlink-then-create, which discards that inode and recreates
  // the path at the process umask. So the end state is asserted after the open
  // too, and these three cases are the whole matrix of what the open can do.
  describe("tightenLiveLog (post-open)", () => {
    // The case the pre-create cannot cover: Chromium threw our inode away and
    // made its own at 0644, with the session token about to be written into it.
    it("re-tightens a live log Chromium unlinked and recreated at the umask", () => {
      const { result, fs } = harness();
      fs.unlinkSync(LIVE);
      fs.openSync(LIVE, "w", 0o644);
      assert.equal(fs.modes.get(LIVE), 0o644, "precondition: the recreated file is world-readable");

      assert.equal(result.tightenLiveLog(), true);
      assert.equal(fs.modes.get(LIVE), 0o600);
    });

    // The ordinary path: Chromium appended to or truncated the inode we created,
    // so the mode already holds and this pass changes nothing. Asserted because
    // an idempotent call is what makes it safe to run unconditionally on boot.
    it("is a no-op when the pre-created inode survived", () => {
      const { result, fs } = harness();
      assert.equal(fs.modes.get(LIVE), 0o600, "precondition: the pre-create already tightened it");

      assert.equal(result.tightenLiveLog(), true);
      assert.equal(fs.modes.get(LIVE), 0o600);
    });

    // A blocked rotation means the sink was never armed, so this path holds the
    // RETAINED session rather than a log Chromium opened. `rotateNativeLog`
    // owns that generation's mode; touching it here would be acting on a file
    // this boot deliberately left alone.
    it("does nothing when rotation was blocked and the sink was never armed", () => {
      const blockedFs = fakeFs({ present: [LIVE], throwOn: "EROFS" });
      const { result, fs: usedFs } = harness({ fs: blockedFs });
      assert.equal(result.blocked, true, "precondition: rotation was blocked");
      usedFs.modes.set(LIVE, 0o644);

      assert.equal(result.tightenLiveLog(), false);
      assert.equal(usedFs.modes.get(LIVE), 0o644, "the retained generation must not be touched");
    });

    // Same reason the switches are returned as data: with no `fs` there is no
    // filesystem to act on, and the callback must still be callable.
    it("does nothing when no fs was injected", () => {
      const { result } = harness({ fs: null });
      assert.equal(result.tightenLiveLog(), false);
    });
  });

  // Exercised directly as well as through the callback: this is the one helper
  // that applies to BOTH generations (the live file after Chromium's open and
  // the rotated one), so its own contract is worth pinning separately.
  describe("tightenLogMode", () => {
    it("chmods an existing file to the owner-only mode", () => {
      const fsDouble = fakeFs({ present: [LIVE] });
      assert.equal(fsDouble.modes.get(LIVE), 0o644);

      assert.equal(tightenLogMode(LIVE, { fs: fsDouble }), true);
      assert.equal(fsDouble.modes.get(LIVE), SECRET_FILE_MODE);
    });

    it("reports failure without throwing when the chmod is refused", () => {
      const lines = [];
      const fsDouble = fakeFs({ present: [LIVE] });
      fsDouble.fchmodSync = () => {
        throw new Error("EPERM");
      };

      assert.equal(tightenLogMode(LIVE, { fs: fsDouble, log: (m) => lines.push(m) }), false);
      assert.match(lines.join("\n"), /chmod failed/);
    });

    // The reason this goes through a descriptor at all. `renameSync` MOVES a
    // symlink rather than resolving it, so a `chromium.log` link planted before
    // boot arrives here as the retained generation — and a path-based `chmod`
    // would strip permissions off whatever it points at instead of the log.
    it("refuses a rotated log that is a symlink instead of chmodding its target", () => {
      const lines = [];
      const fsDouble = fakeFs({ present: ["/logs/victim"], links: { [PREV]: "/logs/victim" } });

      assert.equal(tightenLogMode(PREV, { fs: fsDouble, log: (m) => lines.push(m) }), false);
      assert.match(lines.join("\n"), /not a regular file/);
      assert.equal(
        fsDouble.modes.get("/logs/victim"),
        0o644,
        "the link target's mode must be untouched",
      );
    });

    // The window between the stat and the open. A swap landing inside it has to
    // lose into a refusal rather than win a redirect, which is what checking the
    // DESCRIPTOR's identity buys over checking the path's.
    it("refuses when the path changes identity between the stat and the open", () => {
      const lines = [];
      const fsDouble = fakeFs({ present: [PREV] });
      const realFstat = fsDouble.fstatSync.bind(fsDouble);
      fsDouble.fstatSync = (fd) => ({ ...realFstat(fd), ino: 999999 });

      assert.equal(tightenLogMode(PREV, { fs: fsDouble, log: (m) => lines.push(m) }), false);
      assert.match(lines.join("\n"), /changed identity/);
      assert.equal(fsDouble.modes.get(PREV), 0o644, "no mode may be applied on a refusal");
    });

    // An `fs` double without the member simply opts out, matching the module's
    // posture everywhere else: a missing capability is not a failed launch.
    it("opts out when the fs has no fchmodSync", () => {
      assert.equal(tightenLogMode(LIVE, { fs: {} }), false);
    });

    // "Could not verify" must mean "do not proceed": without lstat there is no way
    // to reject a link, so the guard refuses rather than falling back to the
    // unguarded path-based call.
    it("opts out rather than proceeding unguarded when lstatSync is missing", () => {
      const fsDouble = fakeFs({ present: [LIVE] });
      delete fsDouble.lstatSync;

      assert.equal(tightenLogMode(LIVE, { fs: fsDouble }), false);
      assert.equal(fsDouble.modes.get(LIVE), 0o644);
    });
  });

  describe("openRegularFile", () => {
    it("opens a regular file and reports its size", () => {
      const fsDouble = fakeFs({ present: [PREV], contents: { [PREV]: "abc" } });
      const opened = openRegularFile(PREV, "r", { fs: fsDouble });
      assert.ok(opened);
      assert.equal(opened.size, 3);
      fsDouble.closeSync(opened.fd);
    });

    it("returns null for a symlink", () => {
      const fsDouble = fakeFs({ present: ["/logs/victim"], links: { [PREV]: "/logs/victim" } });
      assert.equal(openRegularFile(PREV, "r", { fs: fsDouble }), null);
    });

    it("returns null for a missing path", () => {
      const fsDouble = fakeFs({});
      assert.equal(openRegularFile(PREV, "r", { fs: fsDouble }), null);
    });

    // A refused open must not leak the descriptor it had already taken.
    it("closes the descriptor when the identity check rejects it", () => {
      const fsDouble = fakeFs({ present: [PREV] });
      const realFstat = fsDouble.fstatSync.bind(fsDouble);
      fsDouble.fstatSync = (fd) => ({ ...realFstat(fd), ino: 424242 });

      assert.equal(openRegularFile(PREV, "r", { fs: fsDouble }), null);
      assert.equal(fsDouble.handles.size, 0, "no descriptor may be left open");
    });
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

  // The over-cap path is the one that mattered: exceeding the bound means the
  // console looped, which is precisely the run a user reports — so that log is the
  // MOST likely to be attached, and skipping redaction would hand over a live
  // token. Past the bound the same redaction runs a chunk at a time instead.
  it("redacts a log past the size cap by streaming instead of skipping it", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    forceOverCap(fs);
    const lines = [];
    const out = redactNativeLogSecrets(PREV, { fs, log: (m) => lines.push(m) });
    assert.equal(out.redacted, true);
    assert.equal(out.skipped, null);
    assert.match(fs.body.get(PREV), /token=\[REDACTED\]/);
    assert.doesNotMatch(fs.body.get(PREV), /eyJhbGciOi/);
  });

  // Memory is the reason the whole-file read has a bound at all, so the streaming
  // pass must never hold the file: one chunk in, one chunk out.
  it("holds no more than one chunk while streaming a multi-chunk log", () => {
    const line = `x?token=SECRET${"y".repeat(400)}\n`;
    const bigBody = line.repeat(9000); // comfortably over one 1 MiB chunk
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: bigBody } });
    forceOverCap(fs);
    let widest = 0;
    const realRead = fs.readSync.bind(fs);
    fs.readSync = (fd, buffer, offset, length) => {
      widest = Math.max(widest, length);
      return realRead(fd, buffer, offset, length);
    };

    const out = redactNativeLogSecrets(PREV, { fs });
    assert.equal(out.redacted, true);
    assert.ok(widest <= REDACT_CHUNK_BYTES, `read window ${widest} exceeded the chunk bound`);
    assert.doesNotMatch(fs.body.get(PREV), /SECRET/, "no occurrence may survive the pass");
  });

  // A token sitting across a chunk boundary is the failure mode a naive chunked
  // pass has, so the carry is asserted directly: the value is split by
  // construction, and it must still come out redacted.
  it("redacts a token that straddles a chunk boundary", () => {
    const filler = "f".repeat(REDACT_CHUNK_BYTES - 12);
    const fs = fakeFs({
      present: [PREV],
      contents: { [PREV]: `${filler}u?token=SPLITVALUE&next=1\n` },
    });
    forceOverCap(fs);

    const out = redactNativeLogSecrets(PREV, { fs });
    assert.equal(out.redacted, true);
    assert.doesNotMatch(fs.body.get(PREV), /SPLITVALUE/);
    assert.match(fs.body.get(PREV), /token=\[REDACTED\]&next=1/);
  });

  // Same "do not rewrite what is already clean" property the whole-file path has:
  // a multi-gigabyte clean log must not be copied for nothing.
  it("leaves an already-clean over-cap log untouched and removes its temp", () => {
    const clean = "INFO:CONSOLE nothing to see\n".repeat(50);
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: clean } });
    forceOverCap(fs);

    const out = redactNativeLogSecrets(PREV, { fs });
    assert.equal(out.redacted, false);
    assert.equal(out.skipped, null);
    assert.equal(fs.body.get(PREV), clean, "an untouched log must stay byte-identical");
    assert.deepEqual(fs.renames, [], "nothing may be renamed over a clean log");
    assert.ok(fs.unlinked.includes(`${PREV}.redact.tmp`), "the temp must be cleaned up");
  });

  // The redaction path operates on BYTES, not text. A crash truncates the log
  // mid-write, so the tail is very often an invalid UTF-8 sequence — and a decode
  // would turn it into U+FFFD, which the rename then makes permanent. Both
  // strategies are checked, because both used to round-trip through a string.
  describe("byte fidelity", () => {
    // 0xE4 opens a 3-byte sequence; alone it is invalid UTF-8 and a decode
    // replaces it. Paired with a token, which is the common case.
    const TRUNCATED = "before ?token=SECRET after \xE4";

    it("preserves invalid UTF-8 bytes while redacting, on the whole-file path", () => {
      const fs = fakeFs({ present: [PREV], contents: { [PREV]: TRUNCATED } });

      const out = redactNativeLogSecrets(PREV, { fs });
      assert.equal(out.redacted, true);
      assert.equal(fs.body.get(PREV), "before ?token=[REDACTED] after \xE4");
      assert.ok(
        Buffer.from(fs.body.get(PREV), "latin1").includes(0xe4),
        "the invalid byte must survive verbatim, not become U+FFFD",
      );
    });

    it("preserves invalid UTF-8 bytes while redacting, on the streaming path", () => {
      const fs = fakeFs({ present: [PREV], contents: { [PREV]: `${TRUNCATED}\n` } });
      forceOverCap(fs);

      const out = redactNativeLogSecrets(PREV, { fs });
      assert.equal(out.redacted, true);
      assert.equal(fs.body.get(PREV), "before ?token=[REDACTED] after \xE4\n");
    });

    // A log with no token must come out identical even when it is not valid text,
    // since the no-op early return is what protects it.
    it("leaves a non-UTF-8 log with no token byte-identical", () => {
      const binary = "head \xFF\xFE\x00 tail\n";
      const fs = fakeFs({ present: [PREV], contents: { [PREV]: binary } });

      const out = redactNativeLogSecrets(PREV, { fs });
      assert.equal(out.redacted, false);
      assert.equal(fs.body.get(PREV), binary);
      assert.deepEqual(fs.renames, [], "nothing may be rewritten when nothing changed");
    });

    // The byte form and the documented string form must not drift apart. Same
    // inputs, same answers, over the cases that define the grammar.
    it("agrees with the string form on every ASCII case", () => {
      const cases = [
        "x?token=abc",
        "x&token=abc&next=1",
        "x?access_token=abc",
        "x?ACCESS_TOKEN=abc",
        "x?Token=abc",
        "x?token=",
        "x?x_token=abc",
        "no query at all",
        'log "?token=abc" quoted',
        "?token=a ?token=b",
        "(?token=abc)",
        "[?token=abc]",
      ];
      for (const input of cases) {
        assert.equal(
          redactTokensInBuffer(Buffer.from(input, "latin1")).out.toString("latin1"),
          redactTokensInText(input),
          `byte and string forms disagreed on ${JSON.stringify(input)}`,
        );
      }
    });
  });
  it("creates the streaming temp exclusively", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    forceOverCap(fs);
    const flags = [];
    const realOpen = fs.openSync.bind(fs);
    fs.openSync = (p, flag, mode) => {
      flags.push({ p, flag });
      return realOpen(p, flag, mode);
    };

    redactNativeLogSecrets(PREV, { fs });
    const tmpOpen = flags.find((f) => f.p === `${PREV}.redact.tmp`);
    assert.ok(tmpOpen, "the temp must be opened through a descriptor");
    assert.match(String(tmpOpen.flag), /x/, "the temp open must be exclusive-create");
  });

  // The SMALL-file branch is the one a symlink guard is easiest to forget on, and
  // the most dangerous place to: a link to a character device reports size 0, so it
  // lands under the cap, and an unbounded read of it would exhaust memory at boot.
  // Both branches therefore read through the same guarded descriptor.
  it("refuses a rotated log that is a symlink even when it reports a tiny size", () => {
    const lines = [];
    const fs = fakeFs({ present: ["/dev/zero"], links: { [PREV]: "/dev/zero" } });
    let read = false;
    fs.readFileSync = () => {
      read = true;
      return "";
    };

    const out = redactNativeLogSecrets(PREV, { fs, log: (m) => lines.push(m) });
    assert.equal(out.skipped, "not-a-regular-file");
    assert.equal(out.scanned, false);
    assert.equal(read, false, "the link must never be read");
    assert.match(lines.join("\n"), /not a regular file/);
  });

  // `writeSync` may write fewer bytes than it was given and report how many.
  // Ignoring that count here would make the atomic replace the thing that
  // truncates the retained log, so the write loops until the buffer is drained.
  it("survives a short write while streaming, losing no bytes", () => {
    const bodyText = `a?token=ONE\nb?token=TWO\nplain line\n`;
    const fs = fakeFs({
      present: [PREV],
      contents: { [PREV]: bodyText },
      shortWriteTo: `${PREV}.redact.tmp`,
    });
    forceOverCap(fs);

    const out = redactNativeLogSecrets(PREV, { fs });
    assert.equal(out.redacted, true);
    assert.equal(
      fs.body.get(PREV),
      `a?token=[REDACTED]\nb?token=[REDACTED]\nplain line\n`,
      "every byte must survive a halved write",
    );
    assert.ok(
      fs.writes.filter((w) => w.path === `${PREV}.redact.tmp`).length > 1,
      "a short write must be followed by another",
    );
  });

  // A write that reports zero bytes is not progress; looping on it would hang the
  // boot path, so it has to become an ordinary reported failure instead.
  it("reports rather than spins when a write makes no progress", () => {
    const fs = fakeFs({ present: [PREV], contents: { [PREV]: TOKENED_LINE } });
    forceOverCap(fs);
    fs.writeSync = () => 0;
    const lines = [];

    const out = redactNativeLogSecrets(PREV, { fs, log: (m) => lines.push(m) });
    assert.equal(out.redacted, false);
    assert.equal(out.skipped, "replace-failed");
    assert.match(lines.join("\n"), /short write stalled/);
    assert.ok(fs.unlinked.includes(`${PREV}.redact.tmp`), "the partial temp must be removed");
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
    const out = rotateNativeLog(LIVE, { fs });
    assert.equal(out.rotated, true);
    assert.equal(out.redacted, true);
    assert.match(fs.body.get(PREV), /token=\[REDACTED\]/);
    assert.equal(fs.modes.get(PREV), SECRET_FILE_MODE);
  });

  // Ordering is the safety property. The rename overwrites the older generation,
  // so a redaction that failed after it would leave exactly one copy and a choice
  // between keeping a live credential and destroying the last crash evidence.
  // Redacting first means a failure costs neither.
  it("aborts the rotation without touching either generation when redaction fails", () => {
    const lines = [];
    const fs = fakeFs({
      present: [LIVE, PREV],
      contents: { [LIVE]: TOKENED_LINE, [PREV]: "older session\n" },
      throwWriteOn: `${LIVE}.redact.tmp`,
    });

    const out = rotateNativeLog(LIVE, { fs, log: (m) => lines.push(m) });
    assert.equal(out.rotated, false, "no rotation may be claimed");
    assert.equal(out.redacted, false);
    assert.equal(out.blocked, true, "blocked is what keeps the sink unarmed");
    assert.deepEqual(fs.renames, [], "nothing may be renamed");
    // The failed pass cleans up its own partial temp, which is not a generation.
    assert.equal(fs.unlinked.includes(LIVE), false, "the live generation must not be deleted");
    assert.equal(fs.unlinked.includes(PREV), false, "nor the older one");
    assert.equal(fs.body.get(LIVE), TOKENED_LINE, "the live generation must survive");
    assert.equal(fs.body.get(PREV), "older session\n", "and so must the older one");
    assert.match(lines.join("\n"), /ABORTED/);
  });

  // The redaction has to happen on the live path, before the rename — asserted on
  // the ORDER of operations, since the end state alone cannot tell the two apart.
  // The redaction's own atomic replace is a rename of its temp onto the live path,
  // so a correct order shows that one first and the rotation's rename second.
  it("redacts before renaming, not after", () => {
    const fs = fakeFs({ present: [LIVE], contents: { [LIVE]: TOKENED_LINE } });

    assert.equal(rotateNativeLog(LIVE, { fs }).rotated, true);
    assert.deepEqual(
      fs.renames.map((r) => r.from),
      [`${LIVE}.redact.tmp`, LIVE],
      "the redaction's replace must land before the rotation's rename",
    );
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
