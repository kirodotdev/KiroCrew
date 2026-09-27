"use strict";

// What an artifact's bytes say: the minidump fields that decide "is this our
// crash, and what killed us", and the summary fields of a macOS `.ips` head.
// Both parsers treat a short read as missing data, never as an answer, so a
// dump Crashpad is still writing stays pending instead of being written off.

const { isOwnModule, isElectronShaped } = require("./ownership");

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
 * Both gates matter and neither subsumes the other — see crash-collector.js's
 * header for the measurement that produced them. `reason` is returned for the skip case
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
 *
 * `deletable` is set only on the PROVEN not-ours verdicts, and only when the dump
 * cannot be another Electron build's real crash (`isElectronShaped`). It is what
 * gates the unlink in `collectCrashReports`; `readable` alone never does.
 */
function classifyMinidump(parsed, appNames) {
  if (!parsed) return { crash: false, readable: false, reason: "not-a-minidump" };
  if (!parsed.mainModule) {
    // No module list, or a name that ran past EOF. Either way this says nothing
    // about ownership, and reading it as "not ours" discards our own crash.
    return { crash: false, readable: false, reason: "module-unreadable" };
  }
  if (!isOwnModule(parsed.mainModule, appNames)) {
    return {
      crash: false,
      readable: true,
      reason: "foreign-process",
      deletable: !isElectronShaped(parsed.mainModule),
    };
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
    return { crash: false, readable: true, reason: "not-a-crash", deletable: true };
  }
  return { crash: true, readable: true, reason: "" };
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

module.exports = {
  parseMinidump,
  classifyMinidump,
  parseIpsHead,
  ipsTimestampToIso,
};
