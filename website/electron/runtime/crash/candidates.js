"use strict";

// Which files are crash candidates, and what inspecting one concluded. Minidumps
// come from both Crashpad queues; `.ips` reports are selected by NAME before any
// file is opened. Each inspection reads a bounded head of the file and returns
// exactly one of four outcomes, because they are acknowledged differently.

const path = require("path");
const { anyBasename, ipsBelongsToApp } = require("./ownership");
const {
  parseMinidump,
  classifyMinidump,
  parseIpsHead,
  ipsTimestampToIso,
} = require("./artifact-parsers");

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
 * Inspect one candidate. Always returns one of the four INSPECT_* outcomes,
 * never null, because "null" is what conflated three of them.
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
          deletable: verdict.deletable === true,
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

module.exports = {
  INSPECT_CRASH,
  INSPECT_FOREIGN,
  INSPECT_UNPARSED,
  INSPECT_PENDING,
  listMinidumps,
  listIpsReports,
  inspectCandidate,
};
