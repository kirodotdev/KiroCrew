"use strict";

// One crash scan: list the candidates, baseline what predates the collector,
// inspect the newest unseen ones within a cap, write their ledger lines, and
// only then acknowledge them in the seen-set. Never throws.

const path = require("path");
const {
  crashLogPath,
  crashStatePath,
  readSeenState,
  writeSeenState,
  readCrashLogLines,
  appendCrashLog,
  formatCrashLine,
} = require("./persistence");
const {
  INSPECT_FOREIGN,
  INSPECT_UNPARSED,
  INSPECT_PENDING,
  listMinidumps,
  listIpsReports,
  inspectCandidate,
} = require("./candidates");

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
 *            removed: number, recorded: number}}
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
    removed: 0,
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
    // Baseline run: write off what predates the feature. See crash-collector.js's
    // header for why none of it is inspected.
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
  const provenForeign = []; // outcome `foreign`: acknowledged unconditionally, then deleted
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
      provenForeign.push({ candidate, deletable: result.deletable === true });
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
  //
  // A proven-foreign dump is also DELETED when `deletable`. A dump in this
  // database that is not ours got here because a child process inherited our
  // Crashpad handler (a Mach exception port on macOS), so the handler faithfully
  // wrote the child's crash into our `pending/`. Crashpad only prunes a dump it
  // has uploaded, and `uploadToServer` is off, so nothing ever removes them: one
  // machine accumulated 585 (129 MB) in four days, a dozen an hour, none of
  // them this app. Acknowledging alone kept them out of the ledger but left the
  // leak. Deletion is gated on the same PROOF as the acknowledgement — a
  // readable dump whose first module is someone else's, or whose exception code
  // is zero — never on a failure to read, and never when the stranger is itself
  // an Electron build (`isElectronShaped`): `crashDumps` is per-`app.getName()`,
  // so a dev run of this same app shares it, and its real crash belongs to that
  // build's own collector. Our own dumps are never touched: an own-app crash is
  // `records`, an own-app snapshot (code 0) is the one foreign case that IS
  // ours, and it is a deliberate `DumpWithoutCrashing()` nobody asked to keep
  // either. Best-effort: an unlink that fails leaves the dump acknowledged
  // exactly as before, so a read-only database degrades to the old behaviour
  // rather than to a retry loop.
  for (const { candidate, deletable } of provenForeign) {
    seen.add(candidate.key);
    if (!deletable || candidate.kind !== "minidump") continue;
    try {
      fs.unlinkSync(candidate.filePath);
      summary.removed += 1;
    } catch (e) {
      log(`crash scan could not remove foreign ${candidate.name}: ${e && e.message}`);
    }
  }
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
    + `unparsed=${summary.unparsed} aged=${summary.aged} removed=${summary.removed} `
    + `recorded=${summary.recorded}`
  );
  return summary;
}

module.exports = {
  collectCrashReports,
  MAX_INSPECT_PER_RUN,
  MAX_PENDING_ATTEMPTS,
};
