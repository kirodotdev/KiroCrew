"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const {
  armCrashCollector,
  collectCrashReports,
  crashNoticeSummary,
  crashLogPath,
  crashStatePath,
  parseMinidump,
  classifyMinidump,
  parseIpsHead,
  ipsBelongsToApp,
  isOwnModule,
  appendCrashLog,
  readSeenState,
  writeSeenState,
  MAX_PENDING_ATTEMPTS,
  CRASH_LOG_BASENAME,
  CRASH_STATE_BASENAME,
  MAX_CRASH_LOG_LINES,
} = require("../crash-collector");

const LOGS = "/logs";
const DUMPS = "/dumps";
const REPORTS = "/reports";
// The name the packaged bundle, its executable, and therefore every crash
// artifact on disk actually carry: electron-builder derives all three from
// `build.productName`, which is the joined form. The collector is handed the
// spaced display name at runtime and normalizes the difference away, so these
// fixtures have to spell the on-disk form to be worth anything.
const APP = "KiroCrew"; // brand-ok: build.productName, an on-disk identifier
// Every fixture .ips carries the real `-YYYY-MM-DD-HHMMSS` stamp macOS appends,
// because that stamp is what gives `ipsBelongsToApp` a boundary to anchor our
// name against. A convenient shorthand like `${APP}-x.ips` is not a name macOS
// ever writes, and a fixture the collector would refuse in production tests
// nothing about production.
const IPS_NAME = `${APP}-2026-09-03-101530.ips`;
// The executable basename on Linux and Windows, which is NOT the display name and
// is not derivable from it: `electron/package.json` sets `executableName`, and
// `packaging/build-desktop.sh` renames it again for the nightly channel.
const EXEC = "kirocrew-desktop";
const NIGHTLY_EXEC = "kirocrew-desktop-nightly";
const PENDING = path.join(DUMPS, "pending");
const COMPLETED = path.join(DUMPS, "completed");
const LOG = path.join(LOGS, CRASH_LOG_BASENAME);
const STATE = path.join(LOGS, CRASH_STATE_BASENAME);

/**
 * A real minidump, byte for byte, because the parser's whole job is offsets and
 * a mock of it would only assert that the mock agrees with itself.
 *
 * Layout: header, a three-entry stream directory, then the three streams and
 * the module's name string.
 */
function buildMinidump({
  exceptionCode = 0x80000003,
  threadId = 7,
  address = 0x1234abcdn,
  threadCount = 12,
  moduleName = `/Applications/${APP}.app/Contents/MacOS/${APP}`,
  magic = 0x504d444d,
  streamCount = 3,
} = {}) {
  const DIR_RVA = 32;
  const EXC_RVA = 68;
  const THREADS_RVA = 100;
  const MODULES_RVA = 104;
  const NAME_RVA = 216;

  const nameBytes = Buffer.from(moduleName, "utf16le");
  const buffer = Buffer.alloc(NAME_RVA + 4 + nameBytes.length);

  buffer.writeUInt32LE(magic, 0);
  buffer.writeUInt32LE(0xa793, 4); // MINIDUMP_VERSION
  buffer.writeUInt32LE(streamCount, 8);
  buffer.writeUInt32LE(DIR_RVA, 12);

  const entry = (index, type, size, rva) => {
    const at = DIR_RVA + index * 12;
    buffer.writeUInt32LE(type, at);
    buffer.writeUInt32LE(size, at + 4);
    buffer.writeUInt32LE(rva, at + 8);
  };
  entry(0, 6, 32, EXC_RVA); // ExceptionStream
  entry(1, 3, 4, THREADS_RVA); // ThreadList
  entry(2, 4, 112, MODULES_RVA); // ModuleList

  buffer.writeUInt32LE(threadId, EXC_RVA);
  buffer.writeUInt32LE(exceptionCode, EXC_RVA + 8);
  buffer.writeBigUInt64LE(BigInt(address), EXC_RVA + 24);

  buffer.writeUInt32LE(threadCount, THREADS_RVA);

  buffer.writeUInt32LE(1, MODULES_RVA); // NumberOfModules
  // MINIDUMP_MODULE starts at +4; its ModuleNameRva sits at +20 within it.
  buffer.writeUInt32LE(NAME_RVA, MODULES_RVA + 4 + 20);

  buffer.writeUInt32LE(nameBytes.length, NAME_RVA);
  nameBytes.copy(buffer, NAME_RVA + 4);
  return buffer;
}

/** A two-document .ips: one-line JSON header, then the payload. */
function buildIps({
  appVersion = "0.6.0",
  timestamp = "2026-09-03 10:15:30.0000 +0800",
  incidentId = "ABCD-1234",
  exception = '"exception":{"codes":"0x0000000000000001, 0x0000000000000000","rawCodes":[1,0],"type":"EXC_BREAKPOINT","signal":"SIGTRAP"}',
} = {}) {
  const header = JSON.stringify({
    app_name: APP,
    timestamp,
    app_version: appVersion,
    bug_type: "309",
    os_version: "macOS 26.6.2 (25G83)",
    incident_id: incidentId,
  });
  const payload = `{"uptime":300,${exception},"threads":[]}`;
  return Buffer.from(`${header}\n${payload}`, "utf8");
}

/**
 * fs double over in-memory buffers, with the positional-read surface the
 * collector uses. Directories are implied by the file paths.
 */
function fakeFs(initial = {}) {
  const files = new Map(
    Object.entries(initial).map(([p, v]) => [p, Buffer.isBuffer(v) ? v : Buffer.from(String(v))])
  );
  const handles = new Map();
  let nextFd = 10;
  const api = {
    files,
    writes: [],
    readdirSync(dir) {
      const prefix = dir.endsWith(path.sep) ? dir : dir + path.sep;
      const names = [];
      for (const p of files.keys()) {
        if (!p.startsWith(prefix)) continue;
        const rest = p.slice(prefix.length);
        if (!rest.includes(path.sep)) names.push(rest);
      }
      if (!names.length) throw new Error(`ENOENT: ${dir}`);
      return names;
    },
    statSync(p) {
      const buf = files.get(p);
      if (!buf) throw new Error(`ENOENT: ${p}`);
      return { size: buf.length, mtimeMs: api.mtimes?.[p] ?? 1000 };
    },
    readFileSync(p) {
      const buf = files.get(p);
      if (!buf) throw new Error(`ENOENT: ${p}`);
      return buf.toString("utf8");
    },
    writeFileSync(p, data) {
      api.writes.push(p);
      files.set(p, Buffer.from(String(data)));
    },
    appendFileSync(p, data) {
      const existing = files.get(p) || Buffer.alloc(0);
      files.set(p, Buffer.concat([existing, Buffer.from(String(data))]));
    },
    renameSync(from, to) {
      const buf = files.get(from);
      if (!buf) throw new Error(`ENOENT: ${from}`);
      files.delete(from);
      files.set(to, buf);
    },
    openSync(p) {
      const buf = files.get(p);
      if (!buf) throw new Error(`ENOENT: ${p}`);
      const fd = nextFd++;
      handles.set(fd, buf);
      return fd;
    },
    readSync(fd, buffer, offset, length, position) {
      const source = handles.get(fd);
      if (!source) throw new Error(`EBADF: ${fd}`);
      if (position >= source.length) return 0;
      return source.copy(buffer, offset, position, Math.min(source.length, position + length));
    },
    closeSync(fd) {
      handles.delete(fd);
    },
  };
  return api;
}

/**
 * A collector call with the seen-set already established.
 *
 * `activatedAt` is the epoch so that every fixture artifact (default mtime 1000)
 * counts as produced AFTER this build gained the ability to collect — which is
 * what makes these the "already baselined, now inspect" cases. A state file
 * carrying `baselinedAt` is what marks the baseline as done; a `seen` array
 * alone does not, because arming writes one before the first scan.
 */
function withBaseline(fs, extra = {}) {
  fs.files.set(STATE, Buffer.from(JSON.stringify({
    version: 1,
    activatedAt: new Date(0).toISOString(),
    baselinedAt: new Date(0).toISOString(),
    seen: [],
  })));
  return {
    logsDir: LOGS,
    crashDumpsDir: DUMPS,
    diagnosticReportsDir: REPORTS,
    appName: APP,
    fs,
    ...extra,
  };
}

describe("crashLogPath / crashStatePath", () => {
  it("keeps both files in the logs directory beside chromium.log", () => {
    assert.equal(crashLogPath("/logs/Kiro Crew"), path.join("/logs/Kiro Crew", CRASH_LOG_BASENAME));
    assert.equal(crashStatePath("/logs/Kiro Crew"), path.join("/logs/Kiro Crew", CRASH_STATE_BASENAME));
  });

  it("tolerates a missing directory rather than throwing on a bad launch", () => {
    assert.equal(crashLogPath(undefined), CRASH_LOG_BASENAME);
  });
});

describe("parseMinidump", () => {
  const readerFor = (buffer) => (offset, length) => {
    if (offset >= buffer.length) return null;
    return buffer.subarray(offset, Math.min(buffer.length, offset + length));
  };

  it("reads the exception, thread and module facts at their real offsets", () => {
    const parsed = parseMinidump(readerFor(buildMinidump()));
    assert.equal(parsed.exceptionCode, 0x80000003);
    assert.equal(parsed.crashedThreadId, 7);
    assert.equal(parsed.exceptionAddress, "0x1234abcd");
    assert.equal(parsed.threadCount, 12);
    assert.equal(parsed.mainModule, `/Applications/${APP}.app/Contents/MacOS/${APP}`);
  });

  it("rejects a file that is not a minidump", () => {
    assert.equal(parseMinidump(readerFor(Buffer.alloc(64))), null);
    assert.equal(parseMinidump(readerFor(buildMinidump({ magic: 0x41414141 }))), null);
  });

  it("rejects a truncated header instead of reading past it", () => {
    assert.equal(parseMinidump(readerFor(buildMinidump().subarray(0, 16))), null);
  });

  it("refuses an absurd stream count from a corrupt header", () => {
    assert.equal(parseMinidump(readerFor(buildMinidump({ streamCount: 999999 }))), null);
    assert.equal(parseMinidump(readerFor(buildMinidump({ streamCount: 0 }))), null);
  });

  it("survives a dump truncated after the directory, losing only the module name", () => {
    // A hard crash can kill the writer mid-flush. The exception facts are
    // written early and must still come back.
    const parsed = parseMinidump(readerFor(buildMinidump().subarray(0, 120)));
    assert.equal(parsed.exceptionCode, 0x80000003);
    assert.equal(parsed.mainModule, "");
  });

  it("leaves the module name empty when the name string reads short of its declared length", () => {
    // The length field is flushed but only part of the name bytes are on disk
    // (Crashpad still writing into pending/). A partial name reads as SOME
    // module and would classify the dump on half a string; it must come back
    // empty so the artifact stays module-unreadable and pending, not baselined
    // or judged foreign, until the name lands whole. NAME_RVA is 216, its
    // 4-byte length field at 216 and the body at 220; keep only 8 body bytes.
    const parsed = parseMinidump(readerFor(buildMinidump().subarray(0, 220 + 8)));
    assert.equal(parsed.exceptionCode, 0x80000003);
    assert.equal(parsed.mainModule, "");
  });

  it("reports a zero exception address as absent rather than as 0x0", () => {
    const parsed = parseMinidump(readerFor(buildMinidump({ address: 0n })));
    assert.equal(parsed.exceptionAddress, "");
  });

  it("marks the exception code unknown when its stream is present but unread", () => {
    // Crashpad publishes into pending/ before the dump is complete: the
    // exception stream can be declared in the directory yet not yet flushed.
    // Truncating just past the directory leaves the 32-byte exception block
    // short. That must read as unknown (null), never as 0 — a real
    // DumpWithoutCrashing value — or an own crash read a moment too early is
    // written off as a snapshot it never was.
    const parsed = parseMinidump(readerFor(buildMinidump().subarray(0, 80)));
    assert.equal(parsed.exceptionCode, null);
  });
});

describe("isOwnModule", () => {
  it("accepts the app executable and its helpers", () => {
    assert.equal(isOwnModule(`/Applications/${APP}.app/Contents/MacOS/${APP}`, APP), true);
    assert.equal(isOwnModule(`${APP} Helper (Renderer)`, APP), true);
  });

  it("ignores spacing and .exe so one app name serves every platform", () => {
    assert.equal(isOwnModule(`C:\\Program Files\\${APP}\\${APP}.exe`, "Kiro Crew"), true);
  });

  // A `npm start` dev run is where a crash is most likely to be looked at, and it
  // is covered — but through `execName`, not through a special case for the string
  // `electron`. `main.js` always passes `path.basename(process.execPath)`, which in
  // a dev run IS `Electron`, so the ordinary rule matches it exactly.
  it("accepts a dev-run electron binary via the executable name it is passed", () => {
    assert.equal(isOwnModule("/repo/node_modules/electron/dist/Electron", [APP, "Electron"]), true);
  });

  // The counterpart, and the reason the `base.startsWith("electron")` fallback had
  // to go: `crashDumps` is shared with every other Electron app on the machine and
  // with every other Electron project run from it. Claiming those writes a crash we
  // never had into our ledger, shows a banner for it, and marks the artifact seen —
  // so the real owner's evidence is discarded too. Without our own name in
  // `appNames`, an electron-shaped module is a stranger.
  it("refuses a foreign electron process rather than claiming it", () => {
    assert.equal(isOwnModule("/other/app.app/Contents/MacOS/Electron", APP), false);
    assert.equal(isOwnModule("/somewhere/node_modules/electron/dist/electron", APP), false);
    assert.equal(isOwnModule("electron-builder", [APP, "Electron"]), false);
    assert.equal(isOwnModule("electronmail", [APP, "Electron"]), false);
  });

  it("rejects a foreign process, which is the whole point", () => {
    // Measured reality: hundreds of ruby dumps in our own Crashpad database,
    // from a child that inherited the handler through the environment.
    assert.equal(isOwnModule("/usr/bin/ruby", APP), false);
    assert.equal(isOwnModule("", APP), false);
    assert.equal(isOwnModule("/usr/bin/ruby", ""), false);
  });

  // The ownership test used to be `base.includes(app)`, which claims anything
  // our name is merely a substring of. Two installs side by side is the normal
  // case for anyone tracking a pre-release channel, and the stable install would
  // have counted the nightly's crash as its own.
  it("never claims a sibling channel or a name ours is only a prefix of", () => {
    assert.equal(isOwnModule(`/Applications/${APP} Nightly.app/Contents/MacOS/${APP} Nightly`, APP), false);
    assert.equal(isOwnModule(`${APP}X`, APP), false);
    assert.equal(isOwnModule(`Not${APP}`, APP), false);
  });

  // Anchoring on the display name alone was the regression that made ownership an
  // enumerated set: on Linux and Windows the binary is `kirocrew-desktop`, which
  // is neither equal to `Kiro Crew` nor a helper of it. Off darwin a minidump is
  // the ONLY crash channel, and a dump judged foreign is added to the seen-set —
  // so getting this wrong discarded every Linux crash permanently.
  it("accepts the executable name, which the display name does not imply", () => {
    assert.equal(isOwnModule(`/opt/${APP}/${EXEC}`, ["Kiro Crew", EXEC]), true);
    assert.equal(isOwnModule(`C:\\Program Files\\${APP}\\${EXEC}.exe`, ["Kiro Crew", EXEC]), true);
    // The nightly channel renames the binary again, and `kirocrew-desktop-nightly`
    // extends `kirocrewnightly` in no direction at all — which is precisely why
    // the name has to be passed in rather than computed from the display name.
    assert.equal(isOwnModule(`/opt/${APP}/${NIGHTLY_EXEC}`, ["Kiro Crew Nightly", NIGHTLY_EXEC]), true);
  });

  // Enumerated, not widened. Adding a second name must not turn the rule back
  // into a prefix test, or the sibling-channel defect returns through the door
  // that was opened to fix Linux.
  it("still refuses a sibling channel's binary once two names are in play", () => {
    assert.equal(isOwnModule(`/opt/${APP}/${NIGHTLY_EXEC}`, ["Kiro Crew", EXEC]), false);
    assert.equal(isOwnModule(`/opt/${APP}/${EXEC}`, ["Kiro Crew Nightly", NIGHTLY_EXEC]), false);
    assert.equal(isOwnModule(`${EXEC}-helper-x`, ["Kiro Crew", EXEC]), false);
  });

  // The helper arm is a set too, not an open prefix. Our real helpers are the
  // base helper exactly and the parenthesized Electron variants; a bare
  // `<name>helper` prefix also claimed a foreign `<name> HelperX`, a different
  // program sharing that directory whose crash would then be logged as ours.
  it("accepts our real helpers but not a name that only starts like one", () => {
    assert.equal(isOwnModule(`${APP} Helper`, APP), true);
    assert.equal(isOwnModule(`${APP} Helper (Renderer)`, APP), true);
    assert.equal(isOwnModule(`${APP} Helper (GPU)`, APP), true);
    assert.equal(isOwnModule(`${APP} HelperX`, APP), false);
    assert.equal(isOwnModule(`${APP} Helper Monitor`, APP), false);
  });
});

describe("classifyMinidump", () => {
  const parsed = (over) => ({
    exceptionCode: 0x80000003,
    exceptionAddress: "0x1",
    crashedThreadId: 0,
    threadCount: 1,
    mainModule: APP,
    ...over,
  });

  it("accepts our own process dying on a real exception", () => {
    assert.deepEqual(classifyMinidump(parsed(), APP), { crash: true, readable: true, reason: "" });
  });

  // `readable` is what separates "we read this and it is not a crash of ours"
  // from "we could not read it". Only the former may be acknowledged; the latter
  // has to come back on the next launch, or a dump read a moment too early is
  // written off as somebody else's forever.
  it("rejects a dump whose first module is another program, readably", () => {
    const verdict = classifyMinidump(parsed({ mainModule: "/usr/bin/ruby" }), APP);
    assert.equal(verdict.crash, false);
    assert.equal(verdict.readable, true);
    assert.equal(verdict.reason, "foreign-process");
  });

  it("rejects exception code 0, which is DumpWithoutCrashing and not a crash", () => {
    const verdict = classifyMinidump(parsed({ exceptionCode: 0 }), APP);
    assert.equal(verdict.crash, false);
    assert.equal(verdict.readable, true);
    assert.equal(verdict.reason, "not-a-crash");
  });

  it("leaves an own dump pending when the exception code could not be read", () => {
    // exceptionCode null = the stream was absent or too short to read, not a
    // parsed 0. Reading that as code 0 would write our own crash off as
    // DumpWithoutCrashing and mark it seen forever; it must come back next boot.
    const verdict = classifyMinidump(parsed({ exceptionCode: null }), APP);
    assert.equal(verdict.crash, false);
    assert.equal(verdict.readable, false);
    assert.equal(verdict.reason, "exception-unreadable");
  });

  it("reports an unparseable file as unreadable, not as someone else's", () => {
    const verdict = classifyMinidump(null, APP);
    assert.equal(verdict.readable, false);
    assert.equal(verdict.reason, "not-a-minidump");
  });

  it("reports an empty module name as unreadable, not as someone else's", () => {
    // `readMinidumpString` returns "" for a name that ran past EOF, and an empty
    // name makes `isOwnModule` false — which read as "foreign" would discard our
    // own crash on the strength of a short read.
    const verdict = classifyMinidump(parsed({ mainModule: "" }), APP);
    assert.equal(verdict.crash, false);
    assert.equal(verdict.readable, false);
    assert.equal(verdict.reason, "module-unreadable");
  });
});

describe("ipsBelongsToApp", () => {
  it("matches our reports by name before anything is opened", () => {
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03-101530.ips`, APP), true);
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03-101530.ips`, "Kiro Crew"), true);
  });

  it("matches our helper processes, which crash under their own names", () => {
    assert.equal(ipsBelongsToApp(`${APP} Helper (Renderer)-2026-09-03-101530.ips`, APP), true);
    assert.equal(ipsBelongsToApp(`${APP} Helper (GPU)-2026-09-03-101530.ips`, APP), true);
  });

  it("never claims another application's crash report", () => {
    // DiagnosticReports is shared. A false positive here reads someone else's
    // crash and writes its name into our log.
    assert.equal(ipsBelongsToApp("ruby-2026-09-03-101530.ips", APP), false);
    assert.equal(ipsBelongsToApp("Safari-2026-09-03-101530.ips", APP), false);
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03.wakeups_resource.diag`, APP), false);
    assert.equal(ipsBelongsToApp(`${APP}.ips`, ""), false);
  });

  // The prefix test this replaces made a nightly install's report look like a
  // stable one, so the stable ledger and banner reported a crash that never
  // happened in that install. The process name has to END at the stamp.
  it("never claims a sibling channel's report as this install's crash", () => {
    assert.equal(ipsBelongsToApp(`${APP} Nightly-2026-09-03-101530.ips`, APP), false);
    assert.equal(ipsBelongsToApp(`${APP}X-2026-09-03-101530.ips`, APP), false);
    // Symmetry: the nightly install must not claim stable's report either.
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03-101530.ips`, `${APP} Nightly`), false);
  });

  // Without the stamp there is no boundary to anchor on, so there is no way to
  // tell `${APP}` from `${APP}X` and the file is left alone. It stays on
  // disk, so a later scan can still pick it up if the rule ever widens.
  it("refuses a name carrying no macOS timestamp rather than guessing", () => {
    assert.equal(ipsBelongsToApp(`${APP}.ips`, APP), false);
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03.ips`, APP), false);
  });

  // Counted, not assumed: 38 of 212 reports in one real DiagnosticReports carried
  // a same-second counter (`.000`, `.0002`, `.0003`, `.0004`), and they cluster
  // exactly where a burst does — a process and its helpers going down together,
  // or a crash loop retrying. A `$`-anchored stamp dropped precisely those.
  it("matches a report macOS suffixed for a same-second collision", () => {
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03-101530.000.ips`, APP), true);
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03-101530.0004.ips`, APP), true);
    assert.equal(ipsBelongsToApp(`${APP} Helper (GPU)-2026-09-03-101530.0002.ips`, APP), true);
  });

  // Widening the stamp must not widen the boundary: anything but a numeric counter
  // after the timestamp leaves no place for the name to end, which is the same
  // hole the `$` anchor was closing.
  it("accepts only a numeric collision counter after the timestamp", () => {
    assert.equal(ipsBelongsToApp(`${APP}-2026-09-03-101530.beta.ips`, APP), false);
    assert.equal(ipsBelongsToApp(`${APP}X-2026-09-03-101530.0002.ips`, APP), false);
    assert.equal(ipsBelongsToApp(`${APP} Nightly-2026-09-03-101530.0002.ips`, APP), false);
  });
});

describe("parseIpsHead", () => {
  it("takes the header from JSON and the exception textually", () => {
    const parsed = parseIpsHead(buildIps().toString("utf8"));
    assert.equal(parsed.appVersion, "0.6.0");
    assert.equal(parsed.incidentId, "ABCD-1234");
    assert.equal(parsed.osVersion, "macOS 26.6.2 (25G83)");
    assert.equal(parsed.exception, "EXC_BREAKPOINT/SIGTRAP");
  });

  it("treats a missing exception block as a normal outcome", () => {
    const parsed = parseIpsHead(buildIps({ exception: '"other":{"a":1}' }).toString("utf8"));
    assert.equal(parsed.appVersion, "0.6.0");
    assert.equal(parsed.exception, "");
  });

  it("returns null when the head is not an .ips at all", () => {
    assert.equal(parseIpsHead("not json\npayload"), null);
    assert.equal(parseIpsHead("{}"), null);
    assert.equal(parseIpsHead(""), null);
  });
});

describe("seen-set persistence", () => {
  it("reports no baseline for a missing or torn state file", () => {
    const fs = fakeFs();
    assert.equal(readSeenState(STATE, { fs }).baselined, false);
    fs.files.set(STATE, Buffer.from("{ truncated"));
    assert.equal(readSeenState(STATE, { fs }).baselined, false);
  });

  it("reports no baseline for a state file that only carries the cutoff", () => {
    // What `armCrashCollector` writes before the first scan. Reading this as
    // "already baselined" would send the first scan straight into inspecting
    // every artifact that predates the feature.
    const fs = fakeFs({
      [STATE]: JSON.stringify({ version: 1, activatedAt: "2026-09-04T00:00:00.000Z", seen: [] }),
    });
    const state = readSeenState(STATE, { fs });
    assert.equal(state.baselined, false);
    assert.equal(state.activatedAt, Date.parse("2026-09-04T00:00:00.000Z"));
  });

  it("writes through a temp file so a crash mid-scan cannot tear it", () => {
    const fs = fakeFs();
    writeSeenState(STATE, new Set(["a", "b"]), { fs });
    assert.deepEqual(fs.writes, [`${STATE}.tmp`]);
    assert.deepEqual(readSeenState(STATE, { fs }), {
      seen: new Set(["a", "b"]),
      activatedAt: null,
      baselinedAt: null,
      baselined: false,
      attempts: {},
    });
  });

  it("round-trips both stamps as readable timestamps", () => {
    // ISO strings rather than epoch millis: a user asked to hand over
    // `crash-state.json` should be able to read what it says about their machine.
    const fs = fakeFs();
    writeSeenState(STATE, new Set(["a"]), { fs, activatedAt: 1000, baselinedAt: 2000 });
    assert.match(fs.readFileSync(STATE), /"activatedAt":"1970-01-01T00:00:01.000Z"/);
    const state = readSeenState(STATE, { fs });
    assert.equal(state.activatedAt, 1000);
    assert.equal(state.baselinedAt, 2000);
    assert.equal(state.baselined, true);
  });

  it("keeps the newest keys when the set outgrows its cap", () => {
    const fs = fakeFs();
    const keys = Array.from({ length: 2100 }, (_, i) => `k${i}`);
    writeSeenState(STATE, new Set(keys), { fs });
    const { seen } = readSeenState(STATE, { fs });
    assert.equal(seen.size, 2000);
    assert.equal(seen.has("k2099"), true);
    assert.equal(seen.has("k0"), false);
  });

  it("reports a write failure instead of throwing it at the launch", () => {
    const fs = fakeFs();
    fs.writeFileSync = () => { throw new Error("EROFS"); };
    const messages = [];
    assert.equal(writeSeenState(STATE, new Set(["a"]), { fs, log: (m) => messages.push(m) }), false);
    assert.match(messages.join("\n"), /EROFS/);
  });
});

describe("appendCrashLog", () => {
  it("appends without rewriting while the ledger is short", () => {
    const fs = fakeFs({ [LOG]: "old\n" });
    const result = appendCrashLog(LOG, ["new"], { fs });
    assert.deepEqual(result, { written: 1, trimmed: false });
    assert.equal(fs.readFileSync(LOG), "old\nnew\n");
    assert.deepEqual(fs.writes, []);
  });

  it("keeps the newest lines once the ledger is long", () => {
    const existing = Array.from({ length: MAX_CRASH_LOG_LINES }, (_, i) => `line${i}`).join("\n");
    const fs = fakeFs({ [LOG]: `${existing}\n` });
    const result = appendCrashLog(LOG, ["newest"], { fs });
    assert.equal(result.trimmed, true);
    const lines = fs.readFileSync(LOG).split("\n").filter(Boolean);
    assert.equal(lines.length, MAX_CRASH_LOG_LINES);
    assert.equal(lines[lines.length - 1], "newest");
    assert.equal(lines[0], "line1");
  });

  it("does nothing at all when there is nothing new", () => {
    const fs = fakeFs();
    assert.deepEqual(appendCrashLog(LOG, [], { fs }), { written: 0, trimmed: false });
    assert.equal(fs.files.has(LOG), false);
  });

  it("replaces the ledger through a temp file when it trims, never in place", () => {
    // The trim path rewrites the whole file. Doing that with an in-place
    // writeFileSync truncates the existing ledger first, so an ENOSPC or an
    // interrupted write loses the retained history. The write must land on a
    // sibling temp and rename into place, the way the seen-set write does.
    const existing = Array.from({ length: MAX_CRASH_LOG_LINES }, (_, i) => `line${i}`).join("\n");
    const fs = fakeFs({ [LOG]: `${existing}\n` });
    const result = appendCrashLog(LOG, ["newest"], { fs });
    assert.equal(result.trimmed, true);
    assert.deepEqual(fs.writes, [`${LOG}.tmp`]);
    assert.equal(fs.files.has(`${LOG}.tmp`), false);
    const lines = fs.readFileSync(LOG).split("\n").filter(Boolean);
    assert.equal(lines.length, MAX_CRASH_LOG_LINES);
    assert.equal(lines[lines.length - 1], "newest");
  });
});

describe("collectCrashReports — baseline run", () => {
  it("inspects nothing and reports nothing on the run that creates the state", () => {
    const fs = fakeFs({
      [path.join(PENDING, "a.dmp")]: buildMinidump(),
      [path.join(PENDING, "b.dmp")]: buildMinidump(),
    });
    // Arm first so there is a durable cutoff: the baseline run writes off what
    // predates it (default mtime 1000 <= 2000) without reading a byte. With NO
    // cutoff the scan defers the baseline and DOES inspect instead — see the
    // activation-cutoff describe below.
    armCrashCollector({ logsDir: LOGS, fs, now: () => new Date(2000) });
    fs.openSync = () => { throw new Error("must not open a file on the baseline run"); };

    const scan = collectCrashReports({
      logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs,
      now: () => new Date("2026-09-04T00:00:00Z"),
    });

    assert.equal(scan.baseline, true);
    assert.equal(scan.candidates, 2);
    assert.deepEqual(scan.newCrashes, []);
    assert.match(fs.readFileSync(LOG), /kind=baseline artifacts=2/);
    assert.equal(readSeenState(STATE, { fs }).seen.size, 2);
  });

  it("reports the same artifacts as new only after the baseline exists", () => {
    const fs = fakeFs({ [path.join(PENDING, "a.dmp")]: buildMinidump() });
    collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.baseline, false);
    assert.deepEqual(second.newCrashes, []);

    fs.files.set(path.join(PENDING, "c.dmp"), buildMinidump());
    const third = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(third.newCrashes.length, 1);
    assert.equal(third.newCrashes[0].name, "c.dmp");
  });
});

describe("armCrashCollector", () => {
  it("persists the cutoff before anything can crash", () => {
    const fs = fakeFs();
    const stamped = armCrashCollector({
      logsDir: LOGS, fs, now: () => new Date("2026-09-04T12:00:00Z"),
    });
    assert.equal(stamped, Date.parse("2026-09-04T12:00:00Z"));
    assert.match(fs.readFileSync(STATE), /"activatedAt":"2026-09-04T12:00:00.000Z"/);
    // Arming records the cutoff and NOTHING else: it must not stand in for the
    // baseline, or the first scan would treat every pre-existing dump as fresh.
    assert.equal(readSeenState(STATE, { fs }).baselined, false);
  });

  it("keeps the first launch's stamp on every later launch", () => {
    // Idempotent because the cutoff means "when this build gained the ability to
    // collect". Re-stamping it each boot would make every crash from the previous
    // session pre-existing — the exact bug the cutoff exists to prevent.
    const fs = fakeFs();
    const first = armCrashCollector({ logsDir: LOGS, fs, now: () => new Date(1000) });
    const second = armCrashCollector({ logsDir: LOGS, fs, now: () => new Date(9999) });
    assert.equal(first, 1000);
    assert.equal(second, 1000);
  });

  it("preserves an existing baseline and seen-set", () => {
    const fs = fakeFs({
      [STATE]: JSON.stringify({
        version: 1,
        activatedAt: new Date(500).toISOString(),
        baselinedAt: new Date(600).toISOString(),
        seen: ["k1"],
      }),
    });
    assert.equal(armCrashCollector({ logsDir: LOGS, fs, now: () => new Date(9999) }), 500);
    const state = readSeenState(STATE, { fs });
    assert.equal(state.baselined, true);
    assert.equal(state.seen.has("k1"), true);
  });

  it("reports no cutoff rather than throwing when the state cannot be written", () => {
    const fs = fakeFs();
    fs.writeFileSync = () => { throw new Error("EROFS"); };
    assert.equal(armCrashCollector({ logsDir: LOGS, fs }), null);
    assert.equal(armCrashCollector({ logsDir: LOGS }), null, "and with no fs at all");
  });
});

describe("collectCrashReports — the activation cutoff", () => {
  it("collects a crash that happened after arming, on the very first scan", () => {
    // The whole point of the eager cutoff. The app arms at boot, crashes before
    // the dashboard ever opens, and the NEXT launch's first scan is also its
    // baseline run. Deciding "pre-existing" by "present at first scan" would file
    // that dump as history; deciding it by the cutoff collects it.
    const old = path.join(PENDING, "before.dmp");
    const recent = path.join(PENDING, "after.dmp");
    const fs = fakeFs({ [old]: buildMinidump(), [recent]: buildMinidump() });
    fs.mtimes = { [old]: 500, [recent]: 2000 };
    armCrashCollector({ logsDir: LOGS, fs, now: () => new Date(1000) });

    const scan = collectCrashReports({
      logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs,
      now: () => new Date(3000),
    });

    assert.equal(scan.baseline, true, "still the baseline run");
    assert.match(fs.readFileSync(LOG), /kind=baseline artifacts=1/);
    assert.equal(scan.newCrashes.length, 1, "and the post-cutoff crash is collected");
    assert.equal(scan.newCrashes[0].name, "after.dmp");
    assert.equal(scan.inspected, 1, "the pre-cutoff artifact was never read");
  });

  it("defers the baseline and inspects rather than writing off a real crash when no cutoff was persisted", () => {
    // A state file written by a build that predates arming, or a logs directory
    // that could not be written at boot. Without a durable cutoff there is no
    // definition of "pre-existing", and blanket-baselining every candidate
    // would add a real own-app crash sitting on disk to `seen` and lose it
    // forever. So the scan defers the baseline and inspects: the own crash is
    // collected, and a foreign dump is still acknowledged only on proof.
    const fs = fakeFs({
      [path.join(PENDING, "own.dmp")]: buildMinidump(),
      [path.join(PENDING, "ruby.dmp")]: buildMinidump({ moduleName: "/usr/bin/ruby", exceptionCode: 0 }),
    });
    const scan = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.notEqual(scan.baseline, true, "no cutoff means no blanket baseline");
    assert.equal(scan.inspected, 2, "candidates are inspected, not written off");
    assert.equal(scan.newCrashes.length, 1, "the real own crash is collected, not lost");
    assert.equal(scan.newCrashes[0].name, "own.dmp");
  });

  it("keeps the cutoff across scans that rewrite the state", () => {
    const fs = fakeFs({ [path.join(PENDING, "a.dmp")]: buildMinidump() });
    armCrashCollector({ logsDir: LOGS, fs, now: () => new Date(500) });
    collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(readSeenState(STATE, { fs }).activatedAt, 500);
  });
});

describe("collectCrashReports — filtering", () => {
  it("records our own crash with the facts needed to symbolize it", () => {
    const fs = fakeFs({ [path.join(COMPLETED, "own.dmp")]: buildMinidump() });
    const scan = collectCrashReports(withBaseline(fs));
    assert.equal(scan.newCrashes.length, 1);
    const line = fs.readFileSync(LOG);
    assert.match(line, /kind=minidump file=own\.dmp/);
    assert.match(line, new RegExp(`module=${APP}`));
    assert.match(line, /exc=0x80000003/);
    assert.match(line, /addr=0x1234abcd/);
  });

  it("counts none of the foreign dumps sharing our Crashpad database", () => {
    // The measured failure this collector exists to avoid: a machine with
    // hundreds of inherited-handler dumps being told it crashed hundreds of
    // times.
    const fs = fakeFs({
      [path.join(PENDING, "r1.dmp")]: buildMinidump({ moduleName: "/usr/bin/ruby", exceptionCode: 0 }),
      [path.join(PENDING, "r2.dmp")]: buildMinidump({ moduleName: "/usr/bin/ruby", exceptionCode: 0x8000000b }),
      [path.join(PENDING, "self-snapshot.dmp")]: buildMinidump({ exceptionCode: 0 }),
    });
    const messages = [];
    const scan = collectCrashReports(withBaseline(fs, { log: (m) => messages.push(m) }));
    assert.equal(scan.candidates, 3);
    assert.equal(scan.inspected, 3);
    assert.deepEqual(scan.newCrashes, []);
    assert.equal(fs.files.has(LOG), false);
    assert.match(messages.join("\n"), /foreign-process/);
    assert.match(messages.join("\n"), /not-a-crash/);
  });

  it("never re-reads a rejected dump on the next launch", () => {
    const fs = fakeFs({ [path.join(PENDING, "r1.dmp")]: buildMinidump({ moduleName: "/usr/bin/ruby" }) });
    collectCrashReports(withBaseline(fs));
    fs.openSync = () => { throw new Error("a dump already judged must not be reopened"); };
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.inspected, 0);
  });

  it("reads only the .ips files whose name is ours", () => {
    const fs = fakeFs({
      [path.join(REPORTS, `${APP}-2026-09-03-101530.ips`)]: buildIps(),
      [path.join(REPORTS, "Safari-2026-09-03-101530.ips")]: buildIps(),
    });
    const opened = [];
    const realOpen = fs.openSync;
    fs.openSync = (p) => { opened.push(p); return realOpen(p); };

    const scan = collectCrashReports(withBaseline(fs));
    assert.equal(scan.candidates, 1);
    assert.equal(scan.newCrashes.length, 1);
    assert.deepEqual(opened, [path.join(REPORTS, `${APP}-2026-09-03-101530.ips`)]);
    assert.match(fs.readFileSync(LOG), /kind=ips file=\S+ version=0\.6\.0/);
  });

  it("keeps every value a single token so the ledger stays greppable", () => {
    const fs = fakeFs({ [path.join(REPORTS, IPS_NAME)]: buildIps() });
    collectCrashReports(withBaseline(fs));
    const [line] = fs.readFileSync(LOG).split("\n");
    // `macOS 26.6.2 (25G83)` would otherwise split this line into six fields.
    assert.match(line, /os=macOS_26\.6\.2_\(25G83\)/);
    for (const field of line.split(" ")) assert.match(field, /^[^=]+=[^=]*$/);
  });

  it("dates an .ips from the report rather than from the file, normalized to ISO", () => {
    const fs = fakeFs({ [path.join(REPORTS, IPS_NAME)]: buildIps({ timestamp: "2026-01-02 03:04:05.0000 +0800" }) });
    const scan = collectCrashReports(withBaseline(fs));
    assert.equal(scan.newCrashes[0].at, "2026-01-01T19:04:05.000Z");
  });

  it("falls back to the file time when the report's timestamp is unfamiliar", () => {
    const fs = fakeFs({ [path.join(REPORTS, IPS_NAME)]: buildIps({ timestamp: "sometime last tuesday" }) });
    fs.mtimes = { [path.join(REPORTS, IPS_NAME)]: Date.parse("2026-05-06T07:08:09Z") };
    const scan = collectCrashReports(withBaseline(fs));
    assert.equal(scan.newCrashes[0].at, "2026-05-06T07:08:09.000Z");
  });

  it("skips the diagnostic-report directory entirely when not given one", () => {
    const fs = fakeFs({ [path.join(REPORTS, IPS_NAME)]: buildIps() });
    const scan = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(scan.candidates, 0);
  });
});

describe("collectCrashReports — bounds and failure", () => {
  it("inspects the newest artifacts first and caps how many it reads", () => {
    const files = {};
    const mtimes = {};
    for (let i = 0; i < 6; i += 1) {
      const p = path.join(PENDING, `d${i}.dmp`);
      files[p] = buildMinidump();
      mtimes[p] = 1000 + i;
    }
    const fs = fakeFs(files);
    fs.mtimes = mtimes;

    const scan = collectCrashReports(withBaseline(fs, { maxInspect: 2 }));
    assert.equal(scan.inspected, 2);
    assert.equal(scan.skipped, 4);
    assert.deepEqual(scan.newCrashes.map((c) => c.name).sort(), ["d4.dmp", "d5.dmp"]);
  });

  // The cap DEFERS, it does not discard. An earlier version marked everything
  // fresh as seen so the directory could not grow unboundedly, which quietly
  // threw away every artifact past the cap: acknowledged, never read, never
  // mentioned. A crash LOOP is exactly when the cap is reached, so the excess is
  // the most important thing in that directory rather than the least. Bounding
  // the work per launch is right; bounding it by discarding is not.
  it("defers capped artifacts to the next run instead of acknowledging them", () => {
    const files = {};
    for (let i = 0; i < 4; i += 1) files[path.join(PENDING, `d${i}.dmp`)] = buildMinidump();
    const fs = fakeFs(files);
    const capped = { logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs, maxInspect: 1 };

    const first = collectCrashReports(withBaseline(fs, { maxInspect: 1 }));
    assert.equal(first.inspected, 1);
    assert.equal(first.skipped, 3, "three sit beyond the cap");

    const second = collectCrashReports(capped);
    assert.equal(second.inspected, 1, "the next launch resumes where the cap stopped");
    assert.equal(second.skipped, 2, "and only the still-unread remainder is deferred again");

    // Drains rather than cycling: four capped launches read all four artifacts.
    collectCrashReports(capped);
    const fourth = collectCrashReports(capped);
    assert.equal(fourth.inspected, 1);
    assert.equal(fourth.skipped, 0);
    const fifth = collectCrashReports(capped);
    assert.equal(fifth.inspected, 0, "and then nothing is left pending");
    assert.equal(fifth.skipped, 0);
  });

  it("leaves a crash pending when the ledger write fails", () => {
    const fs = fakeFs({ [path.join(PENDING, "old.dmp")]: buildMinidump() });
    collectCrashReports(withBaseline(fs));
    fs.files.set(path.join(PENDING, "new.dmp"), buildMinidump());

    const messages = [];
    const realAppend = fs.appendFileSync;
    fs.appendFileSync = (p) => { throw new Error(`ENOSPC: ${p}`); };
    const failed = collectCrashReports({
      logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs, log: (m) => messages.push(m),
    });
    // Not announced either. A banner promising saved diagnostics, over a ledger
    // that does not contain them, is worse than staying quiet for one launch.
    assert.deepEqual(failed.newCrashes, [], "an unwritten crash is not announced");
    assert.match(messages.join("\n"), /left pending/);

    fs.appendFileSync = realAppend;
    const retried = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(retried.newCrashes.length, 1, "the next launch re-collects it");
    assert.equal(retried.newCrashes[0].name, "new.dmp");
  });

  it("leaves an unreadable artifact pending rather than acknowledging it", () => {
    // A dump Crashpad is still writing, or a transient EACCES, is readable next
    // launch — so a failed inspection must not consume the artifact.
    const target = path.join(PENDING, "half-written.dmp");
    const fs = fakeFs({ [target]: buildMinidump() });
    const realOpen = fs.openSync;
    fs.openSync = (p) => {
      if (p === target) throw new Error("EACCES");
      return realOpen(p);
    };
    const first = collectCrashReports(withBaseline(fs));
    assert.deepEqual(first.newCrashes, []);

    fs.openSync = realOpen;
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.newCrashes.length, 1, "readable on the next launch, so still collected");
  });

  it("acknowledges an inspected artifact that is not ours", () => {
    // The other half of the same rule: a foreign dump WAS accounted for, so it is
    // acknowledged. Otherwise the 689 dumps another Electron app left behind are
    // re-parsed on every single launch, forever.
    const foreign = path.join(PENDING, "someone-else.dmp");
    const fs = fakeFs({
      [foreign]: buildMinidump({ moduleName: "/Applications/Other.app/Contents/MacOS/Other" }),
    });
    const first = collectCrashReports(withBaseline(fs));
    assert.equal(first.inspected, 1);
    assert.deepEqual(first.newCrashes, []);
    assert.equal(fs.readFileSync(STATE).includes("someone-else.dmp"), true);

    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.inspected, 0, "never inspected a second time");
  });

  it("records an oversized artifact without parsing it", () => {
    // Past the parse cap the collector cannot say whether this was our crash, so
    // it claims neither: a `kind=unparsed` line records that the artifact exists
    // and that this build could not read it, and `newCrashes` stays empty. The
    // line is what stops the artifact being a silent acknowledgement — a user
    // handing over `crashes.log` still has evidence a dump was left behind.
    const fs = fakeFs({ [path.join(PENDING, "huge.dmp")]: buildMinidump() });
    fs.statSync = () => ({ size: 128 * 1024 * 1024, mtimeMs: 1000 });
    fs.openSync = () => { throw new Error("must not open an oversized artifact"); };
    const messages = [];
    const scan = collectCrashReports(withBaseline(fs, { log: (m) => messages.push(m) }));
    assert.deepEqual(scan.newCrashes, []);
    assert.equal(scan.unparsed, 1);
    assert.match(messages.join("\n"), /oversized huge\.dmp/);
    const line = fs.readFileSync(LOG);
    assert.match(line, /kind=unparsed file=huge\.dmp/);
    assert.match(line, /bytes=134217728/);
    // Recorded, so the log has a line to reveal — but NOT announced as a crash:
    // nothing was confirmed, so the renderer's count stays at zero.
    assert.equal(scan.recorded, 1);
    assert.equal(crashNoticeSummary(scan).newCount, 0);
  });

  it("acknowledges an oversized artifact only once its line is on disk", () => {
    // Terminal, not pending — a file does not shrink, so a retry can only reach
    // the same answer — but "terminal" still has to mean "recorded". A failed
    // ledger write leaves it for the next launch like any other record.
    const fs = fakeFs({ [path.join(PENDING, "huge.dmp")]: buildMinidump() });
    fs.statSync = () => ({ size: 128 * 1024 * 1024, mtimeMs: 1000 });
    const realAppend = fs.appendFileSync;
    fs.appendFileSync = (p) => { throw new Error(`ENOSPC: ${p}`); };
    const first = collectCrashReports(withBaseline(fs));
    assert.equal(fs.readFileSync(STATE).includes("huge.dmp"), false);

    fs.appendFileSync = realAppend;
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.unparsed, 1, "re-recorded on the next launch");
    assert.equal(fs.readFileSync(STATE).includes("huge.dmp"), true);

    const third = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(third.inspected, 0, "and then never read again");
    assert.equal(first.unparsed, 0, "the failed run recorded nothing");
  });

  it("leaves a dump whose header reads short pending rather than foreign", () => {
    // A minidump that does not parse says NOTHING about whose it is. Crashpad
    // publishes into pending/ before it has finished filling the file in, so the
    // overwhelmingly likely reading is "we were too early", not "someone else's".
    const target = path.join(PENDING, "torn.dmp");
    const fs = fakeFs({ [target]: buildMinidump().subarray(0, 40) });
    const first = collectCrashReports(withBaseline(fs));
    assert.equal(first.inspected, 1);
    assert.deepEqual(first.newCrashes, []);
    assert.equal(first.deferred, 1);
    // Not acknowledged: absent from `seen`. It IS tracked in `attempts` (a short
    // read counts toward aging), so assert on the seen-set, not the raw file text.
    assert.deepEqual(JSON.parse(fs.readFileSync(STATE)).seen, [], "not acknowledged");

    fs.files.set(target, buildMinidump());
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.newCrashes.length, 1, "collected once the dump is complete");
    assert.equal(second.newCrashes[0].name, "torn.dmp");
  });

  it("leaves a dump with an unreadable module name pending rather than foreign", () => {
    // The module list parsed but the name ran past EOF, so `mainModule` is empty.
    // Ownership is unknown, and an unknown owner must not be read as "not ours".
    const target = path.join(PENDING, "no-module.dmp");
    const full = buildMinidump();
    const fs = fakeFs({ [target]: full.subarray(0, 218) });
    const first = collectCrashReports(withBaseline(fs));
    assert.equal(first.inspected, 1);
    assert.equal(first.deferred, 1);
    assert.deepEqual(JSON.parse(fs.readFileSync(STATE)).seen, [], "not acknowledged");

    fs.files.set(target, full);
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(second.newCrashes.length, 1);
  });

  it("leaves an .ips whose header is cut pending rather than foreign", () => {
    // The filename already proved ownership — `listIpsReports` matched the app
    // name — so an unreadable head is a read that came too early, full stop.
    const target = path.join(REPORTS, `${APP}-2026-09-04-101530.ips`);
    const full = buildIps();
    const fs = fakeFs({ [target]: full.subarray(0, 20) });
    const first = collectCrashReports(withBaseline(fs));
    assert.equal(first.inspected, 1);
    assert.deepEqual(first.newCrashes, []);
    assert.equal(first.deferred, 1);
    assert.deepEqual(JSON.parse(fs.readFileSync(STATE)).seen, [], "not acknowledged");

    fs.files.set(target, full);
    const second = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, diagnosticReportsDir: REPORTS, appName: APP, fs });
    assert.equal(second.newCrashes.length, 1, "collected once the report is complete");
  });

  it("ages out a permanently-unreadable dump after MAX_PENDING_ATTEMPTS so it stops holding the cap", () => {
    // A dump torn by a kill-mid-write reads short on EVERY scan. Left pending
    // forever it holds an inspection slot each launch, and enough of them starve
    // the older real crashes behind them (newest-first only helps when newer
    // READABLE artifacts arrive). So after MAX_PENDING_ATTEMPTS consecutive short
    // reads it is written off — acknowledged, never counted as a crash — and never
    // retried again. The transient case is safe: a dump that completes before the
    // limit is collected normally (covered by the torn-then-complete tests above).
    const target = path.join(PENDING, "torn-forever.dmp");
    const fs = fakeFs({ [target]: buildMinidump().subarray(0, 40) });

    let scan = collectCrashReports(withBaseline(fs));
    for (let i = 1; i < MAX_PENDING_ATTEMPTS; i += 1) {
      assert.equal(scan.deferred, 1, `still pending on attempt ${i}`);
      assert.equal(scan.aged, 0, `not aged before the limit (attempt ${i})`);
      assert.deepEqual(JSON.parse(fs.readFileSync(STATE)).seen, [], "not acknowledged under the limit");
      scan = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    }

    // The scan that reaches the limit ages it out.
    assert.equal(scan.aged, 1, "aged out at the limit");
    assert.deepEqual(scan.newCrashes, [], "aging is not a crash");
    assert.equal(
      JSON.parse(fs.readFileSync(STATE)).seen.includes("minidump:torn-forever.dmp"),
      true,
      "acknowledged so it stops being retried"
    );
    assert.match(fs.readFileSync(LOG), /kind=unreadable file=torn-forever\.dmp/);

    // Acknowledged means never reopened.
    fs.openSync = () => { throw new Error("an aged-out artifact must not be reopened"); };
    const after = collectCrashReports({ logsDir: LOGS, crashDumpsDir: DUMPS, appName: APP, fs });
    assert.equal(after.inspected, 0);
  });

  // End to end rather than on the helper, because the defect this pins was not in
  // the ownership rule — it was in what reached it. `execName` has to travel from
  // the caller through `inspectCandidate` to `classifyMinidump`, and a unit test on
  // `isOwnModule` passes happily while that thread is broken.
  it("collects a Linux crash, whose module is the executable and not the display name", () => {
    const fs = fakeFs({
      [path.join(PENDING, "linux.dmp")]: buildMinidump({ moduleName: `/opt/${APP}/${EXEC}` }),
    });
    const scan = collectCrashReports({
      ...withBaseline(fs),
      appName: "Kiro Crew",
      execName: EXEC,
      diagnosticReportsDir: "", // main.js passes "" off darwin, so dumps are all there is
    });
    assert.equal(scan.newCrashes.length, 1, "a Linux dump of ours must not read as foreign");
    assert.match(fs.readFileSync(LOG), new RegExp(`module=${EXEC}`));
  });

  // The same dump without the executable name is the regression itself: judged
  // foreign, and — because a proven-foreign dump is acknowledged — written off for
  // good, so no later fix ever revisits it. Asserting the seen-set is the point.
  it("acknowledges a genuinely foreign dump, which is why the name must be right", () => {
    const fs = fakeFs({
      [path.join(PENDING, "ruby.dmp")]: buildMinidump({ moduleName: "/usr/bin/ruby" }),
    });
    const scan = collectCrashReports({
      ...withBaseline(fs), appName: "Kiro Crew", execName: EXEC, diagnosticReportsDir: "",
    });
    assert.deepEqual(scan.newCrashes, []);
    assert.equal(scan.inspected, 1);
    assert.equal(fs.readFileSync(STATE).includes("ruby.dmp"), true);
  });

  it("collects an .ips macOS suffixed for a same-second collision", () => {
    const burst = `${APP}-2026-09-03-101530.0002.ips`;
    const fs = fakeFs({ [path.join(REPORTS, burst)]: buildIps() });
    const scan = collectCrashReports({ ...withBaseline(fs), execName: EXEC });
    assert.equal(scan.newCrashes.length, 1, "the burst case is the one worth reporting");
    assert.match(fs.readFileSync(LOG), /kind=ips/);
  });

  it("returns an empty summary rather than throwing when fs is unusable", () => {
    const scan = collectCrashReports({ logsDir: LOGS, appName: APP });
    assert.equal(scan.candidates, 0);
    assert.equal(scan.crashLogPath, LOG);
  });

  it("survives a crash directory that does not exist yet", () => {
    const fs = fakeFs();
    const scan = collectCrashReports(withBaseline(fs));
    assert.equal(scan.candidates, 0);
    assert.deepEqual(scan.newCrashes, []);
  });

  it("keeps going when one artifact cannot be read", () => {
    const fs = fakeFs({
      [path.join(PENDING, "bad.dmp")]: buildMinidump(),
      [path.join(PENDING, "good.dmp")]: buildMinidump(),
    });
    const realOpen = fs.openSync;
    fs.openSync = (p) => {
      if (p.endsWith("bad.dmp")) throw new Error("EACCES");
      return realOpen(p);
    };
    const scan = collectCrashReports(withBaseline(fs));
    assert.equal(scan.newCrashes.length, 1);
    assert.equal(scan.newCrashes[0].name, "good.dmp");
  });
});

describe("crashNoticeSummary", () => {
  it("exposes a count and nothing else — no path, filename, or timestamp", () => {
    const fs = fakeFs({ [path.join(PENDING, "own.dmp")]: buildMinidump() });
    const summary = crashNoticeSummary(collectCrashReports(withBaseline(fs)));
    assert.deepEqual(Object.keys(summary).sort(), ["newCount"]);
    assert.equal(summary.newCount, 1);
    assert.equal(JSON.stringify(summary).includes("own.dmp"), false);
    assert.equal(JSON.stringify(summary).includes(LOGS), false);
  });

  it("is safe on a scan that never ran", () => {
    assert.deepEqual(crashNoticeSummary(null), { newCount: 0 });
  });
});
