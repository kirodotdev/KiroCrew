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
// ## Where each part lives
//
// This file is the collector's surface; main.js and ipc-registrar.js import it
// and nothing else. The parts it composes live in runtime/crash/:
//
//   ownership.js         whose crash a module name or report filename is
//   artifact-parsers.js  what a minidump or an `.ips` head says
//   candidates.js        which files are candidates, and what inspecting one found
//   persistence.js       the seen-set, its cutoffs, and the crashes.log ledger
//   scan.js              one scan: baseline, inspect, record, then acknowledge
//

const {
  isOwnModule,
  isElectronShaped,
  ipsBelongsToApp,
} = require("./runtime/crash/ownership");
const {
  parseMinidump,
  classifyMinidump,
  parseIpsHead,
  ipsTimestampToIso,
} = require("./runtime/crash/artifact-parsers");
const {
  CRASH_LOG_BASENAME,
  CRASH_STATE_BASENAME,
  MAX_CRASH_LOG_LINES,
  crashLogPath,
  crashStatePath,
  readSeenState,
  writeSeenState,
  armCrashCollector,
  appendCrashLog,
} = require("./runtime/crash/persistence");
const {
  collectCrashReports,
  MAX_INSPECT_PER_RUN,
  MAX_PENDING_ATTEMPTS,
} = require("./runtime/crash/scan");

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
  isElectronShaped,
  appendCrashLog,
  readSeenState,
  writeSeenState,
  CRASH_LOG_BASENAME,
  CRASH_STATE_BASENAME,
  MAX_CRASH_LOG_LINES,
  MAX_INSPECT_PER_RUN,
  MAX_PENDING_ATTEMPTS,
};
