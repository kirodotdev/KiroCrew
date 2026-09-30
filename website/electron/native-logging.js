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
// framework returned zero fatal lines. What that leaves behind is a `.ips`
// crash report whose `asi` field is null and whose every frame symbol is a
// nearest-neighbour mismatch. `renderer-recovery.js` could say THAT the
// renderer died and reload it; nothing could say WHY.
//
// What this does NOT capture, stated plainly because an earlier version of this
// comment claimed the opposite: V8's own fatal line (`Fatal error in ... /
// Reached heap limit / invalid size`) is printed with `fputs` to raw fd 2, and
// `--enable-logging=file` redirects Chromium's `LOG()` sink, which is a
// DIFFERENT stream. A V8 fatal therefore never lands in chromium.log. See the
// "Deliberately NOT attempted" note below for why fd 2 is still unredirected,
// and `cage-trace.js` for the narrower capture that does reach V8's own path.
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
// Capturing is only half of it: `crash-collector.js` is what makes either
// channel reachable by the person who hit the crash, by noticing new artifacts
// and recording them in a `crashes.log` ledger the user can hand over. Until
// that landed, both channels wrote files nothing ever mentioned again.
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
 * @returns {Array<[string, string]>} `[name, value]` pairs for appendSwitch.
 */
function nativeLoggingSwitches(logPath) {
  return [
    // `=file` is what sends output to --log-file instead of stderr, which the
    // GUI launch we are compensating for would throw away again.
    ["enable-logging", "file"],
    ["log-file", String(logPath)],
    // Makes `performance.memory` exact and uncached. Without it Chromium
    // BUCKETIZES those values and caches them for 20 MINUTES unless the renderer
    // happens to be locked to a site -- so a memory probe reading it can return a
    // plausible-looking constant forever and be misread as "flat and healthy".
    // The renderer-memory trajectory (src/lib/memoryWatch.ts) derives V8 external
    // memory from that reading, so this switch is what makes its series real; its
    // flush reports `externalMoved=NO-FROZEN-VALUE` if the number never changes,
    // which is the check that this switch actually took effect. Value-less switch,
    // so the empty string is the whole argument.
    ["enable-precise-memory-info", ""],
  ];
}

/** Mode for a file that may contain a bearer credential: owner read/write only. */
const SECRET_FILE_MODE = 0o600;

/**
 * Size at which redaction switches from a whole-file pass to a streaming one.
 *
 * NOT a give-up threshold. Reading a file to rewrite it costs its size in memory,
 * and this runs during boot on a host that may already be under pressure — so
 * past this bound the same redaction is performed a chunk at a time instead, with
 * memory bounded by {@link REDACT_CHUNK_BYTES} rather than by the file. Skipping
 * redaction here would be the wrong trade in exactly the case that matters: the
 * precondition for exceeding this bound is a console loop, which the rotation
 * comment below calls "the thing we want recorded", so an over-cap log is the
 * artifact a user is MOST likely to attach to a bug report — with a live session
 * token in it. The mode bits do not help once the user hands the file over.
 */
const MAX_REDACT_BYTES = 32 * 1024 * 1024;

/** Working-set bound for the streaming pass: one chunk in, one chunk out. */
const REDACT_CHUNK_BYTES = 1024 * 1024;

/**
 * Bound on a single unterminated line held across chunk reads.
 *
 * The streaming pass splits on newlines because a URL never spans one, so a
 * partial trailing line has to be carried into the next chunk or a token sitting
 * across the boundary would escape the pattern. A log with no newline at all
 * would otherwise grow that carry to the size of the file, defeating the point;
 * past this bound the carry is redacted and flushed as-is. A single line this
 * long is not a URL, so the pattern has nothing to lose by it.
 */
const MAX_CARRY_BYTES = 1024 * 1024;

/**
 * Query-string bearer tokens, as Chromium writes them.
 *
 * Chromium's `INFO:CONSOLE` lines append the document URL, and the desktop app
 * loads the dashboard as `?token=<jwt>` — so every renderer console message from
 * that document records the session token verbatim. The value ends at the next
 * URL/log delimiter; `&` matters because the real URL continues with `&sid=`.
 *
 * Narrow by SHAPE, not by spelling: it matches a query parameter whose value is
 * a credential, and nothing else. A broad "anything JWT-shaped" pattern would
 * start eating legitimate log content. The `access_token` alternative has no
 * producer in `website/` today and is kept deliberately, because the repo's other
 * credential redactors already cover the prefixed spellings and a redactor that
 * covered fewer of them than its siblings would be the surprising one — see the
 * comment at `website/src/utils/errorReport.ts` recording that a bare `\btoken\b`
 * does NOT match `access_token`, and the cache-key stripper in
 * `website/src/test/imageDims.keySecrets.test.ts`.
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

/** The marker written in place of a value, as bytes. */
const REDACTED_BYTES = Buffer.from("[REDACTED]", "ascii");

/** Parameter names whose value is a credential, lowercase, as the byte matcher folds. */
const TOKEN_PARAM_NAMES = ["token=", "access_token="];

/**
 * Bytes that terminate a value, mirroring the character class in
 * {@link TOKEN_QUERY_RE}: `&`, ASCII whitespace, `"`, `'`, a backtick, `)`, `]`.
 */
const VALUE_END_BYTES = new Set([
  0x26, 0x20, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x22, 0x27, 0x60, 0x29, 0x5d,
]);

/** Case-folded ASCII compare of `name` against `buffer` at `at`. */
function matchesNameAt(buffer, at, name) {
  if (at + name.length > buffer.length) return false;
  for (let i = 0; i < name.length; i += 1) {
    let b = buffer[at + i];
    if (b >= 0x41 && b <= 0x5a) b += 0x20; // fold A-Z; the names are lowercase
    if (b !== name.charCodeAt(i)) return false;
  }
  return true;
}

/**
 * Byte-exact counterpart of {@link redactTokensInText}: replaces token VALUES and
 * copies every other byte verbatim. Returns the original buffer untouched when
 * there was nothing to redact.
 *
 * The redaction path uses THIS rather than the string form because a log is bytes,
 * not text. Decoding to a string and re-encoding would be lossy in exactly the
 * situation this module exists for: a crash truncates the file mid-write, the
 * trailing multi-byte sequence is invalid UTF-8, and a decode turns it into U+FFFD.
 * Since the rewrite is a rename over the original, that corruption would be silent
 * and unrecoverable — and it would fire on the COMMON case, because Chromium puts
 * a token on every renderer console line, so "has a token" is not a rare condition.
 *
 * Safe on arbitrary bytes, not just UTF-8: every byte this scans for is ASCII, and
 * a UTF-8 multi-byte sequence is made entirely of bytes >= 0x80, so a match can
 * never land inside one. Non-text bytes are simply copied through.
 */
function redactTokensInBuffer(buffer) {
  const pieces = [];
  let copiedFrom = 0;
  let changed = false;
  let i = 0;
  while (i < buffer.length) {
    const b = buffer[i];
    // A value is only a credential where the query grammar says it is: right after
    // `?` or `&`, same as the regex. `&x_token=` is not a match, in either form.
    if (b !== 0x3f && b !== 0x26) {
      i += 1;
      continue;
    }
    const name = TOKEN_PARAM_NAMES.find((n) => matchesNameAt(buffer, i + 1, n));
    if (!name) {
      i += 1;
      continue;
    }
    const valueStart = i + 1 + name.length;
    let valueEnd = valueStart;
    while (valueEnd < buffer.length && !VALUE_END_BYTES.has(buffer[valueEnd])) valueEnd += 1;
    if (valueEnd === valueStart) {
      // `token=` with no value. The regex needs one or more value characters too,
      // so both forms leave it alone.
      i = valueStart;
      continue;
    }
    pieces.push(buffer.subarray(copiedFrom, valueStart), REDACTED_BYTES);
    copiedFrom = valueEnd;
    changed = true;
    i = valueEnd;
  }
  if (!changed) return { out: buffer, changed: false };
  pieces.push(buffer.subarray(copiedFrom));
  return { out: Buffer.concat(pieces), changed: true };
}

/**
 * Open `filePath` only if it is a REGULAR FILE, verified on both sides of the
 * open. Returns `{ fd, size }`, or null when the path could not be established
 * as one. Never throws.
 *
 * Every path this module touches after the rename is a path it does not own the
 * namespace of, and the two operations it performs there — `chmod` and the
 * redaction read — both follow symlinks. `renameSync` moves a link rather than
 * resolving it, so a `chromium.log` symlink planted before boot survives
 * rotation as `chromium.previous.log`, and a path-based `chmod` would then strip
 * permissions off the link's target instead of the log.
 *
 * `lstatSync` does not follow links, so it is what rejects the link itself. The
 * second check closes the window between that stat and the open: `fstatSync` on
 * the descriptor confirms the thing actually opened is still a regular file and
 * still the same inode, so a swap landing in between loses the race into a
 * refusal rather than winning a redirect. Callers then act on the DESCRIPTOR
 * (`fchmodSync`, `readSync`) rather than on the path, which is what makes the
 * check binding instead of advisory.
 *
 * An `fs` double that cannot express the check opts out (null) rather than
 * falling back to the unguarded call — for a security guard, "could not verify"
 * has to mean "do not proceed", not "proceed anyway".
 */
function openRegularFile(filePath, flags, { fs, log = () => {} } = {}) {
  if (!fs || typeof fs.lstatSync !== "function" || typeof fs.openSync !== "function") return null;
  let before;
  try {
    before = fs.lstatSync(filePath);
  } catch (e) {
    log(`native log path could not be stat'd at ${filePath}: ${e && e.message}`);
    return null;
  }
  if (!before || typeof before.isFile !== "function" || !before.isFile()) {
    log(`native log path refused at ${filePath}: not a regular file`);
    return null;
  }
  let fd;
  try {
    fd = fs.openSync(filePath, flags);
  } catch (e) {
    log(`native log path could not be opened at ${filePath}: ${e && e.message}`);
    return null;
  }
  if (typeof fs.fstatSync === "function") {
    let after;
    try {
      after = fs.fstatSync(fd);
    } catch (e) {
      closeQuietly(fd, fs);
      log(`native log descriptor could not be stat'd at ${filePath}: ${e && e.message}`);
      return null;
    }
    const swapped =
      !after ||
      typeof after.isFile !== "function" ||
      !after.isFile() ||
      // `ino` is 0 on platforms that do not report one, so only compare when both
      // sides carry a real value; the isFile check above still holds either way.
      (before.ino && after.ino && before.ino !== after.ino);
    if (swapped) {
      closeQuietly(fd, fs);
      log(`native log path refused at ${filePath}: changed identity between stat and open`);
      return null;
    }
    return { fd, size: Number(after.size) };
  }
  return { fd, size: Number(before.size) };
}

/** Close a descriptor without letting the close itself become the failure. */
function closeQuietly(fd, fs) {
  try {
    if (typeof fs.closeSync === "function") fs.closeSync(fd);
  } catch {
    /* the descriptor is being abandoned either way */
  }
}

/**
 * Write every byte of `buffer` to `fd`, or throw. Never returns short.
 *
 * `writeSync` may write FEWER bytes than it was given and report how many — a
 * pipe-full, a signal, some network filesystems. Ignoring that count here would
 * be the worst possible place to do it: the destination is the temp that is about
 * to be renamed over the retained log, so a short write turns the atomic replace
 * from a safeguard into the thing that truncates the evidence.
 *
 * Takes a Buffer rather than a string so nothing in the write path re-encodes: the
 * bytes handed in are the bytes that land.
 */
function writeAll(fd, buffer, fs) {
  let written = 0;
  while (written < buffer.length) {
    const n = fs.writeSync(fd, buffer, written, buffer.length - written);
    // A zero-byte write is not progress; looping on it would spin forever.
    if (!n) throw new Error(`short write stalled at ${written}/${buffer.length} bytes`);
    written += n;
  }
}

/**
 * Read a whole file through an already-open descriptor, bounded by the size the
 * guarded open measured. Returns the raw BYTES.
 *
 * Bounded by construction: the buffer is allocated from `fstat`'s size rather than
 * grown until EOF, so a path that reports one size and then yields bytes forever
 * cannot run away with memory here. `readSync` may also return short, so the fill
 * is a loop for the same reason `writeAll` is. No decoding — see
 * {@link redactTokensInBuffer} for why the data path stays in bytes.
 */
function readAllIntoBuffer(fd, size, { fs }) {
  const bound = Number.isFinite(size) && size > 0 ? size : 0;
  if (!bound) return Buffer.alloc(0);
  const buffer = Buffer.alloc(bound);
  let filled = 0;
  while (filled < bound) {
    const n = fs.readSync(fd, buffer, filled, bound - filled, null);
    if (!n) break; // EOF: the file shrank since the stat, which is not an error
    filled += n;
  }
  return filled === bound ? buffer : buffer.subarray(0, filled);
}

/**
 * `chmod` to {@link SECRET_FILE_MODE} through an identity-checked descriptor.
 * Never throws.
 *
 * Separate from creation because it applies to BOTH generations: the file this
 * boot creates and the one the previous boot left behind at an inherited 0644.
 * On Windows the POSIX mode is largely advisory — the call is still made rather
 * than platform-gated, because it is harmless there and gating it would be one
 * more branch that only ever runs on one OS.
 *
 * `fchmodSync` on a descriptor from {@link openRegularFile} rather than
 * `chmodSync` on the path: the path form follows a symlink and would strip
 * permissions off the link's target, and a rotated log can BE a link because
 * `renameSync` moves one rather than resolving it. See `openRegularFile`.
 */
function tightenLogMode(filePath, { fs, log = () => {} } = {}) {
  if (!fs || typeof fs.fchmodSync !== "function") return false;
  const opened = openRegularFile(filePath, "r", { fs, log });
  if (!opened) return false;
  try {
    fs.fchmodSync(opened.fd, SECRET_FILE_MODE);
    return true;
  } catch (e) {
    log(`native log chmod failed at ${filePath}: ${e && e.message}`);
    return false;
  } finally {
    closeQuietly(opened.fd, fs);
  }
}

/**
 * Redact an over-cap log a chunk at a time, so a looping-console log is still
 * cleaned without ever holding it whole in memory. Never throws.
 *
 * Reads from a descriptor the CALLER opened and still owns, so the guarded open
 * happens exactly once for both redaction strategies and the size that chose the
 * strategy is the same one the read is bounded by — there is no second stat for a
 * swap to land between.
 *
 * Splits on newlines because a URL never spans one, carrying the unterminated
 * trailing line into the next read so a token straddling a chunk boundary is not
 * missed. Decoding is streamed too, so a multi-byte character split across the
 * boundary is not corrupted.
 *
 * Same atomic-replace discipline as the whole-file path: build a sibling at the
 * tight mode, rename over the original only once every byte is written. If nothing
 * was redacted the sibling is removed instead of renamed, so a clean multi-gigabyte
 * log is not rewritten for no reason.
 */
function redactLargeLogByStreaming(filePath, sourceFd, { fs, log = () => {} } = {}) {
  const needed = ["readSync", "writeSync", "closeSync", "renameSync", "unlinkSync"];
  if (needed.some((m) => typeof fs[m] !== "function")) {
    log(`native log streaming redaction unavailable at ${filePath}`);
    return { scanned: false, redacted: false, skipped: "unsupported-fs" };
  }

  const tmpPath = `${filePath}.redact.tmp`;
  let out = null;
  try {
    // Exclusive-create, for the same reason the whole-file path uses `wx`: the
    // temp path is predictable, and `w` would follow a symlink planted there.
    try {
      out = fs.openSync(tmpPath, "wx", SECRET_FILE_MODE);
    } catch (e) {
      if (!e || e.code !== "EEXIST") throw e;
      fs.unlinkSync(tmpPath);
      out = fs.openSync(tmpPath, "wx", SECRET_FILE_MODE);
    }

    const buffer = Buffer.alloc(REDACT_CHUNK_BYTES);
    let carry = Buffer.alloc(0);
    let changed = false;

    const emit = (bytes) => {
      const cleaned = redactTokensInBuffer(bytes);
      if (cleaned.changed) changed = true;
      if (cleaned.out.length) writeAll(out, cleaned.out, fs);
    };

    for (;;) {
      const read = fs.readSync(sourceFd, buffer, 0, buffer.length, null);
      if (!read) break;
      // Copied, not aliased: `buffer` is reused by the next read.
      const chunk = Buffer.from(buffer.subarray(0, read));
      carry = carry.length ? Buffer.concat([carry, chunk]) : chunk;
      const lastBreak = carry.lastIndexOf(0x0a);
      if (lastBreak >= 0) {
        emit(carry.subarray(0, lastBreak + 1));
        carry = Buffer.from(carry.subarray(lastBreak + 1));
      } else if (carry.length > MAX_CARRY_BYTES) {
        // No newline in sight and the carry is past its bound: flush it rather
        // than let one pathological line grow to the size of the file.
        emit(carry);
        carry = Buffer.alloc(0);
      }
    }
    if (carry.length) emit(carry);

    fs.closeSync(out);
    out = null;
    if (!changed) {
      fs.unlinkSync(tmpPath);
      return { scanned: true, redacted: false, skipped: null };
    }
    fs.renameSync(tmpPath, filePath);
    return { scanned: true, redacted: true, skipped: null };
  } catch (e) {
    // The original is untouched on every failure path here — the sibling is what
    // absorbs a partial write. Clean it up so it cannot be mistaken for a log.
    if (out !== null) closeQuietly(out, fs);
    try {
      fs.unlinkSync(tmpPath);
    } catch {
      /* the temp may never have been created */
    }
    log(`native log streaming redaction not applied at ${filePath}: ${e && e.message}`);
    return { scanned: true, redacted: false, skipped: "replace-failed" };
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
 * without the write members simply opts out.
 *
 * BOTH strategies read through one {@link openRegularFile} descriptor. A path-based
 * `statSync`/`readFileSync` pair here would defeat the symlink guard on the very
 * branch most likely to take it: `chromium.log` linked to a character device reports
 * size 0, so it lands UNDER the cap, and an unbounded synchronous read of it would
 * exhaust memory at launch. One guarded open also means the size that selects the
 * strategy is the size the read is bounded by, with no second stat in between for a
 * swap to land in.
 */
function redactNativeLogSecrets(filePath, { fs, log = () => {} } = {}) {
  if (!fs || typeof fs.writeFileSync !== "function") {
    return { scanned: false, redacted: false, skipped: "unsupported-fs" };
  }
  const source = openRegularFile(filePath, "r", { fs, log });
  if (!source) return { scanned: false, redacted: false, skipped: "not-a-regular-file" };
  try {
    // Over the bound the same redaction runs a chunk at a time instead of being
    // skipped: an over-cap log is the one most likely to be attached to a bug
    // report, because exceeding the bound means the console looped.
    if (Number.isFinite(source.size) && source.size > MAX_REDACT_BYTES) {
      return redactLargeLogByStreaming(filePath, source.fd, { fs, log });
    }
    const raw = readAllIntoBuffer(source.fd, source.size, { fs });
    const cleaned = redactTokensInBuffer(raw);
    if (!cleaned.changed) return { scanned: true, redacted: false, skipped: null };
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
        fs.writeFileSync(tmpPath, cleaned.out, { mode: SECRET_FILE_MODE, flag: "wx" });
      } catch (e) {
        if (!e || e.code !== "EEXIST" || typeof fs.unlinkSync !== "function") throw e;
        fs.unlinkSync(tmpPath);
        fs.writeFileSync(tmpPath, cleaned.out, { mode: SECRET_FILE_MODE, flag: "wx" });
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
  } finally {
    // Owned here for BOTH strategies: the streaming pass reads from this
    // descriptor and deliberately does not close what it did not open.
    closeQuietly(source.fd, fs);
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
 * NECESSARY BUT NOT SUFFICIENT, and that is why `initNativeLogging` also returns
 * a `tightenLiveLog` for the caller to run after the open. This side cannot pin
 * down Chromium's open mode for `--log-file` — the same limit `rotateNativeLog`
 * and the blocked-rotation branch already reason from — and one of the possible
 * modes is unlink-then-create, which would discard the inode created here and
 * recreate the path at the umask. The pre-create wins whenever Chromium opens
 * the existing inode (append or truncate); the post-open tighten covers the case
 * where it does not. Neither alone is a complete answer, so both run: the
 * pre-create closes the window before the first line is written, and the
 * post-open pass is what makes the end state owner-only either way.
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
    // Redact BEFORE the rename, and abort rather than delete if it fails.
    //
    // Order is the whole safety property here. The rename OVERWRITES any older
    // retained generation, so a redaction that fails after it has already run
    // leaves exactly one copy in existence and a choice between keeping a live
    // credential in the file users are asked to attach, or destroying the only
    // native crash evidence left. Doing it in this order means a failure costs
    // neither: both files are still on disk, untouched, and rotation simply does
    // not happen this boot.
    //
    // Safe to rewrite this path even though the comment above calls the live file
    // Chromium's: at THIS moment it is not. Rotation runs before the switches are
    // appended, so Chromium has not opened `--log-file` in this process yet, and
    // the file present is the previous session's, whose Chromium is gone. The
    // single-instance lock is what makes that hold — `initNativeLogging` only runs
    // in the winner, so no sibling process is writing either.
    const redaction = redactNativeLogSecrets(logPath, { fs, log });
    if (redaction.skipped) {
      // Reported as `blocked`, which already means "the sink is not armed this
      // launch" — correct here for the same reason as a failed rename: the file
      // still holds a session we could not clean, and letting Chromium truncate
      // or append to it would be acting on evidence we just failed to handle.
      log(
        `native log rotation ABORTED at ${logPath}: redaction reported ` +
          `${redaction.skipped}, so neither generation is touched — the retained ` +
          `copy is the one attached to bug reports and it must not carry a token`
      );
      return { rotated: false, blocked: true, previousPath: null, redacted: false };
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
    tightenLogMode(previousPath, { fs, log });
    return { rotated: true, blocked: false, previousPath, redacted: true };
  } catch (e) {
    // A read-only directory or a Windows sharing violation reaches here. The
    // live log is still on disk and still holds the session we were trying to
    // preserve, so this is `blocked`, not merely "not rotated".
    log(`native log rotate failed at ${logPath}: ${e && e.message}`);
    return { rotated: false, blocked: true, previousPath: null, redacted: false };
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
 * @returns {{logPath: string, previousPath: string|null, rotated: boolean, blocked: boolean, switches: string[], crashReporter: boolean, tightenLiveLog: () => boolean}}
 *   `tightenLiveLog` must be called once the app is ready, i.e. after Chromium
 *   has opened `--log-file`. Calling it earlier is harmless but pointless.
 */
function initNativeLogging({
  logsDir,
  appendSwitch,
  startCrashReporter,
  fs,
  log = () => {},
} = {}) {
  const logPath = nativeLogPath(logsDir);
  const applied = [];
  let rotated = false;
  let blocked = false;
  let previousPath = null;
  let redacted = true;

  // Before the switches: Chromium opens this path during initialization, so the
  // previous generation has to be moved aside first or it is appended to (or
  // clobbered) instead of preserved.
  if (fs) ({ rotated, blocked, previousPath, redacted } = rotateNativeLog(logPath, { fs, log }));

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
    for (const [name, value] of nativeLoggingSwitches(logPath)) {
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

  // The second half of the mode story, deferred to the caller because it can only
  // run AFTER Chromium has opened the log — see `createTightLogFile` for why the
  // pre-create is not sufficient on its own. Returned as a callback rather than
  // wired to an event here so this module keeps its "no Electron imports" shape
  // and a test can drive the post-open moment directly.
  //
  // Idempotent and fail-soft: on the ordinary path the pre-created inode is still
  // there at 0600 and the chmod is a no-op, so the only case it changes anything
  // is the one it exists for. A no-op when the sink was never armed, because then
  // the path holds the retained session rather than a log Chromium opened, and
  // `rotateNativeLog` already tightened that generation.
  const tightenLiveLog = () => {
    if (!fs || blocked) return false;
    return tightenLogMode(logPath, { fs, log });
  };

  log(
    `native logging armed: file=${blocked ? "skipped" : logPath} ` +
      `previous=${previousPath || "none"} ` +
      `retained=${rotated && !redacted ? "UNREDACTED-DROPPED" : "clean"} ` +
      `switches=${applied.join(",") || "none"} minidumps=${crashReporter}`
  );
  return {
    logPath,
    previousPath,
    rotated,
    blocked,
    redacted,
    switches: applied,
    crashReporter,
    tightenLiveLog,
  };
}

module.exports = {
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
  redactLargeLogByStreaming,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
  SECRET_FILE_MODE,
  MAX_REDACT_BYTES,
  REDACT_CHUNK_BYTES,
  MAX_CARRY_BYTES,
};
