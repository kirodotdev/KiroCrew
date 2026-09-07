"use strict";
//
// Turn the crash records the OS and Crashpad ALREADY write into something a
// user can notice and hand over.
//
// `native-logging.js` arms two capture channels and then stops there: the
// minidump lands in `crashDumps/`, the macOS `.ips` lands in
// `~/Library/Logs/DiagnosticReports/`, and nothing ever mentions either one
// again. That gap is the whole reason a main-process crash reaches us as "it
// closed by itself" three days later, with the reporter guessing at a cause and
// the artifacts still sitting unread on their disk. The evidence was captured;
// nobody knew it existed.
//
// So this module does three things, in the order that matters:
//
//   1. Notices. It diffs the crash directories against a persisted seen-set, so
//      "a crash happened since you last looked" is a fact the app can state
//      rather than a question the user has to be asked.
//   2. Records. It appends one line per crash to `crashes.log`, which — unlike
//      chromium.log — is deliberately NOT rotated per boot. The history of "how
//      often does this happen" is the part a single crash report cannot answer.
//   3. Filters. Which is the part that makes the other two trustworthy.
//
// ## Why the filtering is the load-bearing half
//
// The Crashpad database is NOT ours alone, and treating it as ours produces a
// number that is wrong by two orders of magnitude. Measured on one developer
// machine: 689 `.dmp` files in `crashDumps/pending/`, of which ZERO were this
// app crashing. Every one was a `ruby` process — a child this app spawned,
// which inherited the Crashpad handler through the environment and dumped into
// our database — and every one carried exception code `0x0`, i.e.
// `DumpWithoutCrashing()`, which is a deliberate snapshot and not a crash at
// all. A collector that counted files would have told that user "689 crashes".
//
// Hence two independent gates, both required:
//
//   * The dump's FIRST module must be ours (Crashpad writes the crashing
//     executable first). This rejects the inherited-handler children.
//   * The exception code must be non-zero. This rejects `DumpWithoutCrashing`
//     snapshots, including our own.
//
// Reading the module list means parsing the minidump, which is why there is a
// parser in here rather than a `readdir().length`.
//
// ## Why the first run reports nothing
//
// On the run that first creates the seen-set there is no honest way to report:
// the existing files predate the feature, their count is dominated by the
// foreign dumps described above, and inspecting hundreds of multi-megabyte
// files on a boot is a real startup cost. So the first run establishes a
// BASELINE — everything present is marked seen, uninspected, and noted as such
// in `crashes.log` — and only crashes that appear afterwards are reported. The
// history we never had is not worth a slow launch to half-recover.
//
// ## Privacy: DiagnosticReports holds every app's crashes, not just ours
//
// `~/Library/Logs/DiagnosticReports/` is a shared directory. Filenames are
// matched against the app name BEFORE any file is opened, and a non-matching
// name is never read and never logged. The scan must not become a way to learn
// what else the user runs, or crashes.
//
// ## What to do with an artifact this finds
//
// The path in `crashes.log` is the input to `scripts/symbolize-crash.sh`, which
// fetches the matching Electron symbols and prints real frames. That last step
// is not optional: a release-build `.ips` is unsymbolized and its frame symbols
// are nearest-neighbour guesses, so read literally it implicates whatever
// exported symbol happens to sit below the crash address. Collecting the
// artifact and reading it raw is how a crash gets filed against the wrong
// component.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so the decisions have to be testable without a live `app`
// (same pattern as native-logging.js / renderer-recovery.js).
//

const path = require("path");

/** Crash history, alongside chromium.log in the app's logs directory. */
const CRASH_LOG_BASENAME = "crashes.log";

/** The seen-set, so a crash is reported once rather than on every boot. */
const CRASH_STATE_BASENAME = "crashes-seen.json";

/**
 * Newest unseen artifacts inspected per app session.
 *
 * A cap, not a budget: after a genuine crash the number of new files is one or
 * two, so this only ever binds when something has gone very wrong (a crash
 * loop, or a foreign process dumping in bulk) — exactly the case where reading
 * every file would turn a bad launch into a hung one.
 */
const MAX_INSPECT_PER_RUN = 25;

/**
 * How many scans a dump may read short/unreadable before it is aged out —
 * acknowledged and stopped being retried.
 *
 * The point is what happens to the newest crashes, not the oldest. A dump that
 * stays unreadable holds a slot of the per-run inspection cap on every launch;
 * enough of them (a burst of torn dumps from a kill-mid-write or a full disk)
 * pin the whole cap and an older, still-uninspected real crash behind them never
 * gets read. So a dump that has read short THIS many consecutive scans is
 * written off: we only care about the crashes we can still surface, and one that
 * has failed to parse three launches running is not coming back readable.
 *
 * Three, not one, so the genuinely transient case is preserved: a dump Crashpad
 * was still flushing when the scan ran reads whole within a launch or two and is
 * reported normally, well before it ages out. Aging is for the permanently
 * broken file, not the momentarily incomplete one.
 */
const MAX_PENDING_ATTEMPTS = 3;

/** Retained history. ~500 lines of ~140 chars is a bounded ~70 KB. */
const MAX_CRASH_LOG_LINES = 500;

/**
 * Retained keys in the seen-set. Generous, because dropping a key means
 * re-reporting the crash it stood for — annoying rather than harmful, but the
 * file is small enough that there is no reason to trim it aggressively.
 */
const MAX_SEEN_KEYS = 2000;

/** Bytes read from the head of a `.ips`. The exception block sits well before
 *  the thread backtraces, which are the part that makes these files large. */
const IPS_HEAD_BYTES = 256 * 1024;

/** Artifacts larger than this are recorded but not parsed. */
const MAX_PARSE_BYTES = 64 * 1024 * 1024;

/**
 * What inspecting one artifact concluded. Four outcomes, not two, because they
 * persist DIFFERENTLY and collapsing them loses crashes:
 *
 *   `crash`    — ours, and a real crash. Gets a ledger line; acknowledged only
 *                once that line is on disk.
 *   `foreign`  — PROVEN not ours (a parsed dump whose first module is another
 *                process, or whose exception code is zero). No ledger line, and
 *                acknowledged unconditionally: it was fully accounted for, and
 *                without that the 689 inherited-handler dumps are re-parsed on
 *                every launch forever.
 *   `unparsed` — we cannot read it and never will (it is past the parse cap; a
 *                file's size does not shrink). Terminal, so it gets a ledger
 *                line naming it as unparsed and is then acknowledged — but it is
 *                NOT counted as a crash, because nothing proved it was one.
 *   `pending`  — we could not read it THIS time. Left un-acknowledged so the
 *                next launch tries again.
 *
 * The distinction that matters is `unparsed`/`pending` vs `foreign`. All three
 * used to be one `return null`, which meant "not ours" — so an oversized dump
 * and a short read from a dump Crashpad was still writing were both acknowledged
 * as somebody else's, with no ledger line and no banner. That is a real crash
 * discarded on the strength of not having been read.
 */
const INSPECT_CRASH = "crash";
const INSPECT_FOREIGN = "foreign";
const INSPECT_UNPARSED = "unparsed";
const INSPECT_PENDING = "pending";

// Minidump layout. Values from Microsoft's DbgHelp structures, which Crashpad
// writes on every platform (including macOS and Linux).
const MINIDUMP_MAGIC = 0x504d444d; // 'MDMP', little-endian
const STREAM_THREAD_LIST = 3;
const STREAM_MODULE_LIST = 4;
const STREAM_EXCEPTION = 6;
/** sizeof(MINIDUMP_MODULE). The name RVA sits at +20: BaseOfImage(8) +
 *  SizeOfImage(4) + CheckSum(4) + TimeDateStamp(4). */
const MINIDUMP_MODULE_NAME_RVA_OFFSET = 20;
/** Refuse an absurd stream count rather than allocating from a corrupt header. */
const MAX_STREAMS = 4096;

/** Absolute path of the crash history inside `logsDir`. */
function crashLogPath(logsDir) {
  return path.join(String(logsDir || ""), CRASH_LOG_BASENAME);
}

/** Absolute path of the seen-set, beside the history. */
function crashStatePath(logsDir) {
  return path.join(String(logsDir || ""), CRASH_STATE_BASENAME);
}

/**
 * Basename of a path that may use EITHER separator.
 *
 * `path.basename` follows the HOST platform, but a module name inside a
 * minidump follows the platform that PRODUCED it. A Windows dump opened for
 * inspection on a posix host would otherwise come back as the whole
 * `C:\...\KiroCrew.exe` string and never match the app name.
 */
function anyBasename(value) {
  const text = String(value || "");
  const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
  return cut >= 0 ? text.slice(cut + 1) : text;
}

/** Lowercase, strip spaces and a trailing `.exe`. Comparison form only. */
function normalizeName(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\.exe$/, "")
    .replace(/\s+/g, "");
}

/**
 * Every name this install goes by, in comparison form. Order and duplicates
 * do not matter; emptiness does, and empties are dropped.
 *
 * Ownership is membership in an ENUMERATED set, and this function is where the
 * enumeration happens. It exists because the two names an Electron app has are
 * different strings that neither derives from the other:
 *
 * - the DISPLAY name, `app.getName()` — `Kiro Crew`, or `Kiro Crew Nightly`;
 * - the EXECUTABLE name, `path.basename(process.execPath)` — on macOS that is
 *   the bundle binary and happens to match, but `electron/package.json` sets
 *   `executableName: "kirocrew-desktop"` for Linux and Windows, and
 *   `packaging/build-desktop.sh` overrides the nightly channel to
 *   `kirocrew-desktop-nightly`.
 *
 * So a rule anchored on the display name alone reads `kirocrew-desktop` as a
 * foreign process and throws away every Linux crash of our own app — and off
 * darwin a minidump is the ONLY crash channel we have, because `main.js` passes
 * no `diagnosticReportsDir` there. Worse than thrown away: a dump we classify
 * as proven-foreign joins the seen-set, so the artifact is written off for good
 * and a later fix never revisits it. Guessing one name from the other cannot
 * work either — `kirocrew-desktop-nightly` is not a prefix-extension of
 * `kirocrewnightly` in any direction.
 */
function ownNames(value) {
  const names = [];
  for (const item of Array.isArray(value) ? value : [value]) {
    const name = normalizeName(item);
    if (name && !names.includes(name)) names.push(name);
  }
  return names;
}

/**
 * Is this one of our own process names, allowing for helpers?
 *
 * The single ownership rule, used by BOTH branches of the ownership chain:
 * `isOwnModule` for a minidump's main module, `ipsBelongsToApp` for a macOS
 * report's filename. A name of ours must be the whole of the candidate, our
 * base helper (`<productName> Helper`) exactly, or an Electron helper variant —
 * macOS names those `<productName> Helper (Renderer)` / ` (GPU)`, which after
 * `normalizeName` (spaces stripped, lowercased) read as `<name>helper(...)`.
 *
 * A bare prefix/substring test was the earlier spelling of both branches, and
 * it claimed any process whose name merely BEGINS with one of ours: a sibling
 * release channel (`KiroCrew Nightly`), or an unrelated vendor's `KiroCrewX`.
 * The helper arm had the same flaw one level down — matching a bare
 * `<name>helper` PREFIX also claims a foreign `<name> HelperX`, a different
 * program that merely begins with our helper name — so it must match the base
 * helper exactly or continue into the parenthesized Electron suffix, never an
 * open prefix.
 * Both inputs come from a directory shared with other programs, so that is not
 * hypothetical — it writes another build's crash into our ledger and shows the
 * user a banner for a crash that never happened in this install.
 *
 * `appNames` is a string or a list of them; see `ownNames` for why one name is
 * never enough.
 */
function nameIsOurs(candidate, appNames) {
  if (!candidate) return false;
  return ownNames(appNames).some(
    (app) =>
      candidate === app ||
      candidate === `${app}helper` ||
      candidate.startsWith(`${app}helper(`),
  );
}

/**
 * Does this module name belong to our app?
 *
 * Exactly `nameIsOurs` applied to a minidump's main-module path, with no extra
 * allowance of its own. A `npm start` dev run is still covered, and covered
 * BETTER than by a special case: `main.js` always passes
 * `path.basename(process.execPath)` as one of `appNames`, which in a dev run is
 * `Electron`, so `nameIsOurs` matches it exactly.
 *
 * This deliberately does NOT fall back to "any module named `electron*`". That
 * fallback was here, and it reinstated for minidumps precisely the bare-prefix
 * test `nameIsOurs` documents as wrong: `crashDumps` is shared with every other
 * Electron app and every Electron project run from this machine, so a foreign
 * `electron`, `electron-helper`, or another checkout's `Electron` would be
 * claimed as ours — a crash we never had, entered in our ledger, banner and all,
 * and then marked seen so the real owner's evidence is written off too. The
 * other branch of the chain (`ipsBelongsToApp`) never had such a fallback; both
 * branches now decide ownership by the same single rule.
 */
function isOwnModule(moduleName, appNames) {
  const base = normalizeName(anyBasename(moduleName));
  if (!base) return false;
  return nameIsOurs(base, appNames);
}

/**
 * Read a UTF-16LE MINIDUMP_STRING at `rva`.
 *
 * Returns "" rather than throwing on a length that runs past EOF: a truncated
 * dump (the writer died mid-flush, which a hard crash can do) should cost us
 * the module name, not the whole scan.
 */
function readMinidumpString(read, rva) {
  const lengthField = read(rva, 4);
  if (!lengthField || lengthField.length < 4) return "";
  const byteLength = lengthField.readUInt32LE(0);
  // A name is a filename, not a document. Anything past this is corruption.
  if (byteLength === 0 || byteLength > 8192) return "";
  const body = read(rva + 4, byteLength);
  // Require the COMPLETE declared string. A short read (Crashpad still
  // publishing, or a torn dump) used to be truncated to whole code units and
  // returned as a partial name — and a partial name reads as SOME module, which
  // classifies the dump foreign/own on half a string and then acknowledges it.
  // Returning "" instead leaves mainModule empty, so classifyMinidump calls it
  // module-unreadable and the artifact stays pending until it reads whole.
  if (!body || body.length < byteLength) return "";
  // Round down to whole code units so an odd declared length cannot split one.
  return body.subarray(0, body.length - (body.length % 2)).toString("utf16le");
}

/**
 * Extract the facts that decide "is this our crash, and what killed us".
 *
 * @param {(offset: number, length: number) => Buffer|null} read Random access
 *        over the dump. A short or null return means EOF, never an error.
 * @returns {{exceptionCode: number|null, exceptionAddress: string, crashedThreadId: number,
 *            threadCount: number, mainModule: string}|null} null if this is not
 *        a minidump at all, or its header is unusable.
 */
function parseMinidump(read) {
  const header = read(0, 32);
  if (!header || header.length < 32) return null;
  if (header.readUInt32LE(0) !== MINIDUMP_MAGIC) return null;

  const streamCount = header.readUInt32LE(8);
  const directoryRva = header.readUInt32LE(12);
  if (streamCount === 0 || streamCount > MAX_STREAMS) return null;

  const directory = read(directoryRva, streamCount * 12);
  if (!directory) return null;

  const streams = new Map();
  const entries = Math.floor(directory.length / 12);
  for (let i = 0; i < entries; i += 1) {
    const type = directory.readUInt32LE(i * 12);
    // First wins: a well-formed dump has one of each of the streams we want,
    // and a duplicate is corruption we should not prefer.
    if (!streams.has(type)) {
      streams.set(type, { size: directory.readUInt32LE(i * 12 + 4), rva: directory.readUInt32LE(i * 12 + 8) });
    }
  }

  const result = {
    // null, not 0, until the exception block is actually read. An absent or
    // too-short stream means we do not KNOW the code, and 0 is a real value
    // (`DumpWithoutCrashing`). Defaulting to 0 let an own crash whose exception
    // stream had not been flushed yet read as a deliberate snapshot and get
    // written off; only `classifyMinidump` may turn an explicitly read 0 into
    // "not a crash", and it leaves an unread code (null) pending instead.
    exceptionCode: null,
    exceptionAddress: "",
    crashedThreadId: -1,
    threadCount: 0,
    mainModule: "",
  };

  const exception = streams.get(STREAM_EXCEPTION);
  if (exception) {
    // MINIDUMP_EXCEPTION_STREAM: ThreadId(4) __alignment(4) then
    // MINIDUMP_EXCEPTION { ExceptionCode(4) ExceptionFlags(4)
    // ExceptionRecord(8) ExceptionAddress(8) ... }.
    const block = read(exception.rva, 32);
    if (block && block.length >= 32) {
      result.crashedThreadId = block.readUInt32LE(0);
      result.exceptionCode = block.readUInt32LE(8);
      const address = block.readBigUInt64LE(24);
      result.exceptionAddress = address === 0n ? "" : `0x${address.toString(16)}`;
    }
  }

  const threads = streams.get(STREAM_THREAD_LIST);
  if (threads) {
    const count = read(threads.rva, 4);
    if (count && count.length >= 4) result.threadCount = count.readUInt32LE(0);
  }

  const modules = streams.get(STREAM_MODULE_LIST);
  if (modules) {
    // Crashpad writes the crashing executable as module 0, which is what makes
    // this one string enough to decide ownership. Layout: NumberOfModules(4)
    // then the MINIDUMP_MODULE array.
    const nameRvaField = read(modules.rva + 4 + MINIDUMP_MODULE_NAME_RVA_OFFSET, 4);
    if (nameRvaField && nameRvaField.length >= 4) {
      result.mainModule = readMinidumpString(read, nameRvaField.readUInt32LE(0));
    }
  }

  return result;
}

/**
 * Is a parsed dump a REAL crash of OUR app?
 *
 * Both gates matter and neither subsumes the other — see the module header for
 * the measurement that produced them. `reason` is returned for the skip case
 * because "we found dumps and reported none" is otherwise indistinguishable
 * from a broken scan.
 *
 * `crash: false` alone is not enough for the caller to act on, so `readable`
 * comes back too. Failing a gate means PROVEN not-ours; failing to read the
 * header or the module name means we learned nothing, and the caller must leave
 * such a dump pending rather than write it off. Crashpad publishes a `.dmp` into
 * `pending/` before it has finished filling it in, so an unreadable dump is
 * routinely one we are simply too early for — the most likely moment for that
 * being the launch right after the crash, which is when this runs.
 */
function classifyMinidump(parsed, appNames) {
  if (!parsed) return { crash: false, readable: false, reason: "not-a-minidump" };
  if (!parsed.mainModule) {
    // No module list, or a name that ran past EOF. Either way this says nothing
    // about ownership, and reading it as "not ours" discards our own crash.
    return { crash: false, readable: false, reason: "module-unreadable" };
  }
  if (!isOwnModule(parsed.mainModule, appNames)) {
    return { crash: false, readable: true, reason: "foreign-process" };
  }
  if (parsed.exceptionCode === null) {
    // The exception stream was absent or too short to read. Crashpad publishes
    // a dump into pending/ before it has finished filling it in, so this is the
    // same "too early, learned nothing" case as an unreadable module: leave it
    // pending rather than write our own crash off as a snapshot it never was.
    return { crash: false, readable: false, reason: "exception-unreadable" };
  }
  if (parsed.exceptionCode === 0) {
    // DumpWithoutCrashing(): a deliberate snapshot. The process kept running.
    return { crash: false, readable: true, reason: "not-a-crash" };
  }
  return { crash: true, readable: true, reason: "" };
}

/**
 * The `-YYYY-MM-DD-HHMMSS` stamp macOS appends to every `.ips` it writes, plus
 * the `.NNN` counter it adds when two reports land in the SAME second.
 *
 * The counter is not an edge case and it is not optional: of 212 reports in one
 * real `~/Library/Logs/DiagnosticReports`, 38 (18%) carried one — `.000`,
 * `.0002`, `.0003`, `.0004` — and they appear precisely during a burst, when a
 * process and its helpers go down together or a crash loop retries. Anchoring
 * the stamp at `$` without it dropped exactly the reports this collector exists
 * to surface, including the `Google Chrome Helper (GPU)` / `(Renderer)` shape
 * that is the same Chromium helper naming our own app produces.
 */
const IPS_STAMP_RE = /-\d{4}-\d{2}-\d{2}-\d{6}(\.\d+)?$/;

/**
 * Filename-only ownership test for a shared crash-report directory.
 *
 * Runs BEFORE the file is opened, which is the point: DiagnosticReports holds
 * every application's crash reports, and this scan must not become a way to
 * enumerate them. macOS names these `<ProcessName>-<date>-<time>.ips`.
 *
 * The stamp is stripped FIRST so there is a boundary to anchor against, and the
 * process name that remains has to satisfy `nameIsOurs` outright. Testing the
 * whole filename for our prefix instead — which is what this did — makes
 * `KiroCrew Nightly-2026-09-03-101530.ips` read as a stable-channel crash,
 * because "kirocrew" is a prefix of "kirocrewnightly-2026-...".
 *
 * A name carrying no stamp is therefore refused, and the asymmetry is
 * deliberate: skipping a report of ours costs the banner one launch and leaves
 * the artifact on disk for the next scan, while claiming somebody else's writes
 * a crash that never happened into a ledger a human will later read as fact.
 */
function ipsBelongsToApp(basename, appNames) {
  const name = String(basename || "");
  if (!name.toLowerCase().endsWith(".ips")) return false;
  const stem = name.slice(0, -".ips".length);
  if (!IPS_STAMP_RE.test(stem)) return false;
  return nameIsOurs(normalizeName(stem.replace(IPS_STAMP_RE, "")), appNames);
}

/**
 * Pull the summary fields out of a `.ips` head.
 *
 * These files are two concatenated JSON documents: a one-line header, then a
 * payload. The header parses properly. The payload does NOT get parsed — it
 * reaches megabytes on a process with many threads, and everything we want
 * from it is one small object near the front — so the exception block is
 * matched textually and treated as best-effort. `exception` staying empty is a
 * normal outcome, not an error: the artifact itself is what the user hands
 * over, and this line only has to be enough to recognise it.
 */
function parseIpsHead(text) {
  const content = String(text || "");
  const newline = content.indexOf("\n");
  if (newline < 0) return null;
  let header = null;
  try {
    header = JSON.parse(content.slice(0, newline));
  } catch {
    // Not an .ips, or the head was cut mid-header. Either way, nothing to say.
    return null;
  }
  if (!header || typeof header !== "object") return null;

  let exception = "";
  const block = content.slice(newline).match(/"exception"\s*:\s*\{[^{}]*\}/);
  if (block) {
    const type = block[0].match(/"type"\s*:\s*"([^"]*)"/);
    const signal = block[0].match(/"signal"\s*:\s*"([^"]*)"/);
    exception = [type && type[1], signal && signal[1]].filter(Boolean).join("/");
  }

  return {
    appVersion: String(header.app_version || ""),
    osVersion: String((header.os_version && String(header.os_version)) || ""),
    timestamp: String(header.timestamp || ""),
    incidentId: String(header.incident_id || ""),
    exception,
  };
}

/** An ISO string back to epoch ms, or null for anything unparseable. */
function readStamp(raw) {
  if (raw === undefined || raw === null || raw === "") return null;
  const ms = typeof raw === "number" ? raw : Date.parse(String(raw));
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Read the crash state: the seen-set plus the two timestamps that make it mean
 * something.
 *
 * `activatedAt` is when the collector was first armed on this install, and
 * `baselinedAt` is when the pre-existing artifacts were written off. They are
 * separate because arming happens EAGERLY at launch while the scan is lazy, so
 * between the two there is a state file that has a cutoff but no baseline yet.
 *
 * Both absent is either a first run or a state file from a build that predates
 * them; `baselined: false` is the honest answer in both cases, and the baseline
 * below then re-establishes itself over whatever `seen` already held.
 */
function readSeenState(statePath, { fs, log = () => {} } = {}) {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, "utf8"));
    if (parsed && Array.isArray(parsed.seen)) {
      const baselinedAt = readStamp(parsed.baselinedAt);
      return {
        seen: new Set(parsed.seen.map(String)),
        activatedAt: readStamp(parsed.activatedAt),
        baselinedAt,
        baselined: baselinedAt !== null,
        // Per-key count of consecutive scans a dump has read short. Bounds how
        // long an unreadable file may hold an inspection slot before it ages out.
        attempts: readAttempts(parsed.attempts),
      };
    }
    log(`crash state at ${statePath} has no seen list; re-establishing baseline`);
  } catch {
    // First run, or a torn write. Both mean the same thing to the caller.
  }
  return { seen: new Set(), activatedAt: null, baselinedAt: null, baselined: false, attempts: {} };
}

/** A `{key: count}` map, defended against a torn or hand-edited state file. */
function readAttempts(raw) {
  const out = {};
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [key, value] of Object.entries(raw)) {
      const n = typeof value === "number" ? value : parseInt(String(value), 10);
      if (Number.isFinite(n) && n > 0) out[String(key)] = n;
    }
  }
  return out;
}

/**
 * Persist the seen-set and its timestamps through a temp file.
 *
 * Atomic on purpose: a torn state file parses as "no baseline", which would
 * silently re-baseline and swallow the very crash the user is about to be told
 * about. Cheap insurance against a crash DURING the crash scan, which is not a
 * hypothetical on a machine that is already crashing.
 *
 * The stamps are written as ISO strings rather than epoch numbers because this
 * file is something a person reads while working out why a crash was or was not
 * reported, and `1788539034123` does not answer that question.
 */
function writeSeenState(statePath, seen, { fs, log = () => {}, activatedAt = null, baselinedAt = null, attempts = null } = {}) {
  // Keep the newest keys: `seen` is insertion-ordered and appended newest-last.
  const keys = Array.from(seen);
  const kept = keys.length > MAX_SEEN_KEYS ? keys.slice(keys.length - MAX_SEEN_KEYS) : keys;
  const state = { version: 1 };
  if (Number.isFinite(activatedAt)) state.activatedAt = new Date(activatedAt).toISOString();
  if (Number.isFinite(baselinedAt)) state.baselinedAt = new Date(baselinedAt).toISOString();
  state.seen = kept;
  // Only carried when there is something pending: an empty map stays absent so a
  // legacy state file and a quiescent one look identical on disk.
  if (attempts && Object.keys(attempts).length > 0) state.attempts = attempts;
  const temp = `${statePath}.tmp`;
  try {
    fs.writeFileSync(temp, `${JSON.stringify(state)}\n`);
    fs.renameSync(temp, statePath);
    return true;
  } catch (e) {
    log(`crash state write failed at ${statePath}: ${e && e.message}`);
    return false;
  }
}

/**
 * Stamp the activation cutoff, at launch, before anything can crash.
 *
 * This exists because "pre-existing" needs a definition that does not depend on
 * when the user happens to open the dashboard. The scan is lazy — nothing calls
 * it until the crash notice asks — so the baseline used to mean "everything
 * present at the first scan". A crash BETWEEN installing this feature and first
 * opening the dashboard therefore landed on the wrong side of that line and was
 * written off as history, which is the one crash a new collector most needs to
 * report. Worse, the app can crash on launch and never reach a dashboard at all,
 * so that window is not hypothetical.
 *
 * Cheap enough to be eager: one small read, and a write only on the launch that
 * first arms it. Returns the cutoff in epoch ms, or null if it could not be
 * persisted — in which case the scan has no durable definition of pre-existing
 * and defers the baseline (inspecting candidates instead of writing any off)
 * rather than silently acknowledging a real crash it cannot date.
 */
function armCrashCollector({ logsDir, fs, now = () => new Date(), log = () => {} } = {}) {
  if (!fs) return null;
  const statePath = crashStatePath(logsDir);
  const { seen, activatedAt, baselinedAt } = readSeenState(statePath, { fs, log });
  if (activatedAt !== null) return activatedAt;
  const stamped = now().getTime();
  if (!writeSeenState(statePath, seen, { fs, log, activatedAt: stamped, baselinedAt })) return null;
  log(`crash collector armed at ${new Date(stamped).toISOString()}`);
  return stamped;
}

/** Existing history lines, for the trim-on-append below. */
function readCrashLogLines(logPath, { fs } = {}) {
  try {
    return fs.readFileSync(logPath, "utf8").split("\n").filter(Boolean);
  } catch {
    return [];
  }
}

/**
 * Append history, bounded by LINE count rather than by rotation.
 *
 * The opposite choice from chromium.log, and deliberately so. That file is
 * rotated per boot because it is a firehose whose value is entirely in the last
 * session. This one is a ledger: a handful of lines a year on a healthy install,
 * and its whole value is that it spans the launches BETWEEN crashes, which is
 * the question a single crash report cannot answer. So it survives every boot
 * and is trimmed only when it gets long.
 */
function appendCrashLog(logPath, lines, { fs, log = () => {} } = {}) {
  if (!lines.length) return { written: 0, trimmed: false };
  const existing = readCrashLogLines(logPath, { fs });
  const combined = existing.concat(lines);
  const trimmed = combined.length > MAX_CRASH_LOG_LINES;
  const kept = trimmed ? combined.slice(combined.length - MAX_CRASH_LOG_LINES) : combined;
  try {
    if (trimmed) {
      // Trim rewrites the whole file. Write a sibling temp and rename it into
      // place so an ENOSPC or an interrupted write cannot truncate the existing
      // ledger before the replacement is complete — the same temp+rename the
      // seen-set write uses, for the same reason.
      const temp = `${logPath}.tmp`;
      fs.writeFileSync(temp, `${kept.join("\n")}\n`);
      fs.renameSync(temp, logPath);
    } else {
      fs.appendFileSync(logPath, `${lines.join("\n")}\n`);
    }
    return { written: lines.length, trimmed };
  } catch (e) {
    log(`crash log write failed at ${logPath}: ${e && e.message}`);
    return { written: 0, trimmed: false };
  }
}

/**
 * `key=value` pairs, empty values dropped, for one history line.
 *
 * Whitespace inside a value becomes `_` rather than being kept: several values
 * legitimately contain spaces (`macOS 26.6.2 (25G83)`), and a space-separated
 * `key=value` line that also has spaces inside its values cannot be split back
 * apart by the person reading it — or by the `grep`/`awk` they reach for first.
 */
function formatCrashLine(fields) {
  return Object.entries(fields)
    .filter(([, value]) => value !== "" && value !== null && value !== undefined)
    .map(([key, value]) => `${key}=${String(value).trim().replace(/\s+/g, "_")}`)
    .join(" ");
}

/**
 * Normalize a macOS `.ips` timestamp (`2026-09-03 10:15:30.0000 +0800`) to ISO.
 *
 * Worth the regex so the ledger has ONE time format: a minidump is dated from
 * its file mtime, already ISO and already UTC, and a reader comparing "when did
 * these two artifacts get written" should not have to also reconcile a local
 * time with an offset. Returns "" when the shape is unfamiliar, so the caller
 * can fall back rather than write a half-parsed date.
 */
function ipsTimestampToIso(raw) {
  const match = String(raw || "").match(
    /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*([+-]\d{2}):?(\d{2})$/
  );
  if (!match) return "";
  const parsed = new Date(`${match[1]}T${match[2]}${match[3]}:${match[4]}`);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toISOString();
}

/**
 * Random access over a file, using a bounded number of small reads.
 *
 * `readFileSync` would be simpler and is wrong here: a minidump is routinely
 * tens of megabytes, the parser needs about 100 bytes of it, and this runs on
 * the launch AFTER a crash — the launch a user is already watching impatiently.
 * Returns null past EOF so the parser can treat truncation as missing data.
 *
 * A file that cannot be OPENED is a different thing from a file that reads
 * short, and this used to conflate them by returning a reader whose every read
 * yields null. Downstream that is indistinguishable from a dump belonging to
 * some other app, so an EACCES or a dump Crashpad had not finished writing was
 * classified "not ours" and then acknowledged in the seen-set — a real crash
 * discarded on the strength of a transient error. Let the failure out instead:
 * `collectCrashReports` catches it per candidate and leaves that artifact
 * pending for the next launch, which is the honest outcome.
 */
function fileReader(filePath, { fs }) {
  const fd = fs.openSync(filePath, "r");
  return {
    read(offset, length) {
      if (!Number.isFinite(offset) || offset < 0 || length <= 0) return null;
      try {
        const buffer = Buffer.alloc(length);
        const got = fs.readSync(fd, buffer, 0, length, offset);
        return got > 0 ? buffer.subarray(0, got) : null;
      } catch {
        return null;
      }
    },
    close() {
      try {
        fs.closeSync(fd);
      } catch {
        // Nothing useful to do; the process is about to move on either way.
      }
    },
  };
}

/** Every `*.dmp` under a Crashpad database, newest information attached. */
function listMinidumps(crashDumpsDir, { fs, log = () => {} } = {}) {
  const found = [];
  if (!crashDumpsDir) return found;
  // Crashpad moves a dump from pending/ to completed/ once its handler has
  // finished with it, so a dump we care about can be in either. `Crashpad/` is
  // the layout Electron's crashDumps path already points at.
  for (const sub of ["pending", "completed"]) {
    const dir = path.join(crashDumpsDir, sub);
    let names = [];
    try {
      names = fs.readdirSync(dir);
    } catch {
      // Absent until the first dump, which is the normal case.
      continue;
    }
    for (const name of names) {
      if (!String(name).toLowerCase().endsWith(".dmp")) continue;
      const filePath = path.join(dir, name);
      let stat = null;
      try {
        stat = fs.statSync(filePath);
      } catch (e) {
        log(`crash scan could not stat ${name}: ${e && e.message}`);
        continue;
      }
      found.push({ kind: "minidump", key: `minidump:${name}`, name, filePath, mtimeMs: stat.mtimeMs || 0, size: stat.size || 0 });
    }
  }
  return found;
}

/** Our own `.ips` reports, selected by NAME before anything is opened. */
function listIpsReports(diagnosticReportsDir, appNames, { fs, log = () => {} } = {}) {
  const found = [];
  if (!diagnosticReportsDir) return found;
  let names = [];
  try {
    names = fs.readdirSync(diagnosticReportsDir);
  } catch {
    return found;
  }
  for (const name of names) {
    if (!ipsBelongsToApp(name, appNames)) continue;
    const filePath = path.join(diagnosticReportsDir, name);
    let stat = null;
    try {
      stat = fs.statSync(filePath);
    } catch (e) {
      log(`crash scan could not stat ${name}: ${e && e.message}`);
      continue;
    }
    found.push({ kind: "ips", key: `ips:${name}`, name, filePath, mtimeMs: stat.mtimeMs || 0, size: stat.size || 0 });
  }
  return found;
}

/**
 * Inspect one candidate. Always returns one of the four outcomes above — never
 * null, because "null" is what conflated three of them.
 */
function inspectCandidate(candidate, { fs, appNames, log = () => {} }) {
  if (candidate.size > MAX_PARSE_BYTES) {
    // Terminal rather than pending: a file on disk does not get smaller, so
    // retrying next launch would re-stat it forever and still not read it. It
    // still gets a ledger line, because the artifact is real and handing it over
    // is exactly what the user should do with it — we just cannot say whether it
    // was a crash, so it is not counted as one.
    log(`crash scan skipped oversized ${candidate.name} (${candidate.size} bytes)`);
    return {
      outcome: INSPECT_UNPARSED,
      kind: candidate.kind,
      name: candidate.name,
      at: new Date(candidate.mtimeMs).toISOString(),
      fields: {
        kind: "unparsed",
        file: candidate.name,
        artifact: candidate.kind,
        bytes: candidate.size,
        why: `over-${MAX_PARSE_BYTES}-byte-parse-cap`,
      },
    };
  }
  const reader = fileReader(candidate.filePath, { fs });
  try {
    if (candidate.kind === "minidump") {
      const parsed = parseMinidump(reader.read);
      const verdict = classifyMinidump(parsed, appNames);
      if (!verdict.crash) {
        log(`crash scan ignored ${candidate.name}: ${verdict.reason}`);
        return {
          outcome: verdict.readable ? INSPECT_FOREIGN : INSPECT_PENDING,
          name: candidate.name,
          reason: verdict.reason,
        };
      }
      return {
        outcome: INSPECT_CRASH,
        kind: "minidump",
        name: candidate.name,
        at: new Date(candidate.mtimeMs).toISOString(),
        fields: {
          kind: "minidump",
          file: candidate.name,
          module: anyBasename(parsed.mainModule),
          exc: `0x${parsed.exceptionCode.toString(16)}`,
          addr: parsed.exceptionAddress,
          thread: parsed.crashedThreadId >= 0 ? parsed.crashedThreadId : "",
          threads: parsed.threadCount || "",
          bytes: candidate.size,
        },
      };
    }

    const head = reader.read(0, IPS_HEAD_BYTES);
    const parsed = head ? parseIpsHead(head.toString("utf8")) : null;
    if (!parsed) {
      // PENDING, not foreign. The filename already proved this report is ours —
      // `listIpsReports` matched it before opening anything — so an unreadable
      // header says nothing about ownership and everything about timing: the OS
      // writes these in place, and a head that is short or cut mid-header is one
      // we arrived at too early. Acknowledging it here would discard our own
      // crash report on the strength of a race.
      log(`crash scan deferred ${candidate.name}: unreadable report header`);
      return { outcome: INSPECT_PENDING, name: candidate.name, reason: "unreadable-header" };
    }
    return {
      outcome: INSPECT_CRASH,
      kind: "ips",
      name: candidate.name,
      // The report's own timestamp beats the file mtime: a report copied or
      // restored from a backup keeps the former and loses the latter.
      at: ipsTimestampToIso(parsed.timestamp) || new Date(candidate.mtimeMs).toISOString(),
      fields: {
        kind: "ips",
        file: candidate.name,
        version: parsed.appVersion,
        os: parsed.osVersion,
        exc: parsed.exception,
        incident: parsed.incidentId,
        bytes: candidate.size,
      },
    };
  } finally {
    reader.close();
  }
}

/**
 * Scan for crash artifacts, record the new ones, and summarize. Never throws.
 *
 * @param {object} deps
 * @param {string} deps.logsDir               Where crashes.log and the seen-set live.
 * @param {string} [deps.crashDumpsDir]       Electron's `crashDumps` path.
 * @param {string} [deps.diagnosticReportsDir] macOS DiagnosticReports; omit elsewhere.
 * @param {string} deps.appName               Display name, `app.getName()`.
 * @param {string} [deps.execName]            Executable basename. Defaults to this
 *        process's own, which IS our binary in a packaged app; pass it explicitly
 *        from the caller so the wiring is visible. See `ownNames` for why the
 *        display name alone is not an ownership test.
 * @param {object} deps.fs
 * @param {() => Date} [deps.now]
 * @param {(msg: string) => void} [deps.log]
 * @param {number} [deps.maxInspect]
 * @returns {{crashLogPath: string, logsDir: string, baseline: boolean,
 *            newCrashes: Array<object>, candidates: number, inspected: number,
 *            skipped: number, deferred: number, unparsed: number, aged: number,
 *            recorded: number}}
 */
function collectCrashReports({
  logsDir,
  crashDumpsDir,
  diagnosticReportsDir,
  appName,
  execName = path.basename(process.execPath),
  fs,
  now = () => new Date(),
  log = () => {},
  maxInspect = MAX_INSPECT_PER_RUN,
} = {}) {
  const logPath = crashLogPath(logsDir);
  const statePath = crashStatePath(logsDir);
  const summary = {
    crashLogPath: logPath,
    logsDir: String(logsDir || ""),
    baseline: false,
    newCrashes: [],
    candidates: 0,
    inspected: 0,
    skipped: 0,
    deferred: 0,
    unparsed: 0,
    aged: 0,
    recorded: 0,
  };
  if (!fs) return summary;

  // Built once and passed down, so both branches of the ownership chain test
  // against the SAME set and cannot drift apart again.
  const appNames = [appName, execName];

  let candidates = [];
  try {
    candidates = listMinidumps(crashDumpsDir, { fs, log })
      .concat(listIpsReports(diagnosticReportsDir, appNames, { fs, log }));
  } catch (e) {
    // A collector that breaks the launch it was added to diagnose is worse
    // than no collector.
    log(`crash scan failed: ${e && e.message}`);
    return summary;
  }
  summary.candidates = candidates.length;

  const state = readSeenState(statePath, { fs, log });
  const { seen, activatedAt, baselined } = state;
  // Both stamps are carried through every write below, so a scan never drops the
  // cutoff that decides what counts as pre-existing.
  let baselinedAt = state.baselinedAt;
  // Prior per-key unreadable-scan counts. `nextAttempts` is rebuilt from only the
  // keys still pending at the END of this run, so a resolved or vanished key drops
  // out and the map stays bounded by the pending backlog.
  const priorAttempts = state.attempts || {};
  const nextAttempts = {};

  if (!baselined) {
    // Baseline run: write off what predates the feature. See the module header
    // for why none of it is inspected.
    //
    // "Predates" means OLDER THAN THE ACTIVATION CUTOFF, not "present when the
    // dashboard first asked". The scan is lazy, so those two are different by
    // however long the user takes to open the dashboard — and an app that
    // crashes on launch may never get there at all. Keying the baseline on the
    // first scan therefore wrote off exactly the crash a newly-installed
    // collector exists to report. `armCrashCollector` stamps the cutoff at
    // launch so this line does not move.
    //
    // A null cutoff means the stamp could not be persisted (or this state file
    // predates it), and then there is NO durable definition of "pre-existing".
    // Blanket-baselining every candidate in that case adds them all to `seen` —
    // a permanent acknowledgement — so a real own-app crash sitting on disk at
    // that moment is written off and the user never hears about it. That is the
    // exact silent data loss the `seen` invariant below forbids. Without a
    // cutoff we therefore DEFER: baseline nothing, leave baselinedAt unset so a
    // later launch (which may finally have a persisted cutoff) can baseline
    // properly, and let the normal inspect/classify path below decide each
    // candidate. That path is safe to run over old artifacts — a foreign dump
    // is acknowledged only on PROOF (so the pre-existing-foreign backlog still
    // stops being re-parsed, just across a few capped launches instead of one),
    // and a real crash is collected instead of lost.
    if (activatedAt === null) {
      log(`crash scan baseline deferred: no durable activation cutoff, ${candidates.length} candidate(s) left pending`);
    } else {
      const predates = candidates.filter((c) => c.mtimeMs <= activatedAt);
      for (const candidate of predates) seen.add(candidate.key);
      baselinedAt = now().getTime();
      writeSeenState(statePath, seen, { fs, log, activatedAt, baselinedAt });
      appendCrashLog(
        logPath,
        [formatCrashLine({ at: new Date(baselinedAt).toISOString(), kind: "baseline", artifacts: predates.length, note: "pre-existing-artifacts-not-inspected" })],
        { fs, log }
      );
      summary.baseline = true;
      log(`crash scan baseline: ${predates.length} pre-existing artifacts marked seen`);
    }
    // Deliberately NOT a return. Anything newer than the cutoff is a crash from
    // after this feature shipped, and it has to be collected on THIS run — the
    // whole point of the cutoff is that such a crash is not history.
  }

  const fresh = candidates.filter((c) => !seen.has(c.key));

  // Newest first, so the cap keeps the crashes closest to what the user just
  // experienced rather than an arbitrary slice.
  const ordered = fresh.slice().sort((a, b) => b.mtimeMs - a.mtimeMs);
  const toInspect = ordered.slice(0, Math.max(0, maxInspect));
  summary.inspected = toInspect.length;
  summary.skipped = ordered.length - toInspect.length;
  if (summary.skipped > 0) {
    log(`crash scan capped: ${summary.skipped} new artifacts beyond ${maxInspect} left uninspected`);
  }

  // `seen` is an ACKNOWLEDGEMENT: a key in it is never looked at again, so
  // anything added that was not actually accounted for is a crash the user
  // silently never hears about. Every path to `seen.add`, and why:
  //
  //   crash          -> acknowledge ONLY once its ledger line is on disk.
  //   foreign        -> acknowledge. This is the 689-foreign-dump case; without
  //                     it they are re-parsed every launch forever. Requires
  //                     PROOF (a parsed dump whose module is someone else's, or
  //                     whose exception code is zero), never a failure to read.
  //   unparsed       -> acknowledge once its ledger line is on disk. Terminal:
  //                     the artifact is past the parse cap and a file does not
  //                     shrink, so a retry can only produce the same answer. Not
  //                     counted as a crash — we never proved it was one.
  //   pending        -> leave un-acknowledged UNTIL it has read short
  //                     `MAX_PENDING_ATTEMPTS` scans running, then age it out
  //                     (acknowledge, not a crash). Covers a throw (EACCES, or a
  //                     dump Crashpad had not published yet), a dump whose header
  //                     or module name reads short, and an `.ips` whose header is
  //                     cut. A genuinely transient one reads whole within a launch
  //                     or two and is reported before it ages; a permanently
  //                     broken one is written off so it stops holding a cap slot
  //                     and starving the older real crashes behind it.
  //   beyond the cap -> leave un-acknowledged. A crash LOOP is exactly when the
  //                     cap is hit, the worst moment to mark 30 crashes seen
  //                     having read 25. Newest-first means these are the OLDEST
  //                     unseen artifacts; once the aging above drains any stuck
  //                     unreadable dumps off the front, a later scan reaches them.
  const lines = [];
  const records = [];      // outcome `crash`: announced AND acknowledged on write
  const notes = [];        // outcome `unparsed`: acknowledged on write, not announced
  const provenForeign = []; // outcome `foreign`: acknowledged unconditionally
  const pendingKeys = [];   // read short THIS run: aged out once the count trips
  for (const candidate of toInspect) {
    let result = null;
    try {
      result = inspectCandidate(candidate, { fs, appNames, log });
    } catch (e) {
      // Read short this run: counted toward aging, not acknowledged yet.
      log(`crash scan failed on ${candidate.name}: ${e && e.message}`);
      summary.deferred += 1;
      pendingKeys.push({ key: candidate.key, name: candidate.name });
      continue;
    }
    if (!result || result.outcome === INSPECT_PENDING) {
      summary.deferred += 1;
      pendingKeys.push({ key: candidate.key, name: candidate.name });
      continue;
    }
    if (result.outcome === INSPECT_FOREIGN) {
      provenForeign.push(candidate.key);
      continue;
    }
    lines.push(formatCrashLine({ at: result.at, ...result.fields }));
    if (result.outcome === INSPECT_UNPARSED) {
      notes.push({ candidate, record: result });
    } else {
      records.push({ candidate, record: result });
    }
  }

  // The ledger write comes BEFORE the acknowledgement, and its result decides
  // the acknowledgement. A full disk or a read-only logs directory used to
  // acknowledge the crash anyway, which lost it twice over: absent from
  // `crashes.log` AND never re-collected. `written` is 0 both when the write
  // failed and when there was nothing to write, so the empty case is separated
  // out rather than read as a failure.
  const appended = appendCrashLog(logPath, lines, { fs, log });
  const durable = lines.length === 0 || appended.written > 0;

  // Proven-foreign needs no ledger line, so its acknowledgement does not depend
  // on the write landing.
  for (const key of provenForeign) seen.add(key);
  if (durable) {
    for (const { candidate, record } of records) {
      seen.add(candidate.key);
      summary.newCrashes.push({ kind: record.kind, name: record.name, at: record.at });
    }
    // Recorded but never announced: `unparsed` says an artifact exists that this
    // build could not read, which is worth a line in the log the user hands over
    // and is not worth claiming a crash we did not confirm.
    for (const { candidate } of notes) seen.add(candidate.key);
    // Counted here, not above: like `newCrashes`, this reports what the scan
    // RECORDED. A run whose ledger write failed recorded nothing, and saying
    // otherwise would describe an artifact that is still pending as accounted for.
    summary.unparsed = notes.length;
  } else {
    // Not reported to the UI either: a banner saying diagnostics were saved,
    // offering to reveal a log that does not contain them, is worse than
    // silence. They stay pending and are re-attempted on the next launch.
    log(
      `crash scan: ${records.length + notes.length} record(s) left pending `
      + "— ledger write failed"
    );
  }

  // Age out dumps that have read short too many scans running. This is the fix
  // for the starvation corner: without it a permanently-unreadable dump is
  // retried on every launch, and enough of them (a burst of torn dumps) hold the
  // whole inspection cap and starve the older real crashes behind them. After
  // MAX_PENDING_ATTEMPTS the file is written off — acknowledged into `seen`,
  // never counted as a crash — and stops being retried; a file still under the
  // limit keeps its incremented count and is retried next scan. Acknowledgement
  // here is UNCONDITIONAL, not gated on the ledger write below, precisely because
  // the goal is to STOP retrying: gating it on a read-only logs dir would let the
  // backlog persist and re-starve. The ledger line is best-effort documentation.
  const agedLines = [];
  for (const { key, name } of pendingKeys) {
    const n = (priorAttempts[key] || 0) + 1;
    if (n >= MAX_PENDING_ATTEMPTS) {
      seen.add(key);
      summary.aged += 1;
      agedLines.push(
        formatCrashLine({
          at: now().toISOString(),
          kind: "unreadable",
          file: name,
          note: `aged-out-after-${n}-unreadable-scans`,
        })
      );
    } else {
      nextAttempts[key] = n;
    }
  }
  if (agedLines.length > 0) {
    appendCrashLog(logPath, agedLines, { fs, log });
    log(`crash scan: ${summary.aged} unreadable artifact(s) aged out and acknowledged`);
  }

  writeSeenState(statePath, seen, { fs, log, activatedAt, baselinedAt, attempts: nextAttempts });

  // `kind=unparsed` counts here even though it is not a crash: it is still a
  // line in the log the user hands over, so it counts as something recorded.
  // Logged rather than surfaced — the renderer gets `newCount` and nothing else.
  summary.recorded = readCrashLogLines(logPath, { fs }).filter(
    (l) => l.includes("kind=minidump") || l.includes("kind=ips") || l.includes("kind=unparsed")
  ).length;

  log(
    `crash scan: candidates=${summary.candidates} new=${summary.newCrashes.length} `
      + `inspected=${summary.inspected} skipped=${summary.skipped} deferred=${summary.deferred} `
    + `unparsed=${summary.unparsed} aged=${summary.aged} recorded=${summary.recorded}`
  );
  return summary;
}

/**
 * The renderer-facing view of a scan.
 *
 * Deliberately narrow: ONE number. No paths, filenames, module names, exception
 * codes, timestamps, or logs directory — all of which describe the machine (down
 * to the account name in a home directory) and none of which the notice needs.
 * A connection window pointed at a remote gateway shares this preload, so the
 * smaller this payload is, the less the sender gate has to carry. The detail
 * lives in `crashes.log`, which the user reveals and hands over deliberately.
 *
 * It carried `lastCrashAt` and `hasLog` too, until a review pointed out that no
 * consumer read either one: the banner's text is a count, and the reveal button
 * renders whenever the count is non-zero. `hasLog` in particular looked
 * load-bearing and was not — `newCount > 0` already implies a ledger line,
 * because a crash is only counted once its line is durably on disk. Fields the
 * UI does not read are not free here: every one is another thing crossing the
 * boundary this channel's three gates exist to protect.
 */
function crashNoticeSummary(scan) {
  const newCrashes = (scan && scan.newCrashes) || [];
  return { newCount: newCrashes.length };
}

module.exports = {
  armCrashCollector,
  collectCrashReports,
  crashNoticeSummary,
  crashLogPath,
  crashStatePath,
  parseMinidump,
  classifyMinidump,
  parseIpsHead,
  ipsBelongsToApp,
  ipsTimestampToIso,
  isOwnModule,
  appendCrashLog,
  readSeenState,
  writeSeenState,
  CRASH_LOG_BASENAME,
  CRASH_STATE_BASENAME,
  MAX_CRASH_LOG_LINES,
  MAX_INSPECT_PER_RUN,
  MAX_PENDING_ATTEMPTS,
};
