"use strict";

// What the collector remembers across launches, in the app's logs directory:
// the seen-set that acknowledges each artifact once (with the activation and
// baseline cutoffs that give it meaning), and `crashes.log`, the ledger that
// survives every boot. Every write goes through a temp file and a rename, so a
// crash during the crash scan cannot tear either file.

const path = require("path");

/** Crash history, alongside chromium.log in the app's logs directory. */
const CRASH_LOG_BASENAME = "crashes.log";

/** The seen-set, so a crash is reported once rather than on every boot. */
const CRASH_STATE_BASENAME = "crashes-seen.json";

/** Retained history. ~500 lines of ~140 chars is a bounded ~70 KB. */
const MAX_CRASH_LOG_LINES = 500;

/**
 * Retained keys in the seen-set. Generous, because dropping a key means
 * re-reporting the crash it stood for — annoying rather than harmful, but the
 * file is small enough that there is no reason to trim it aggressively.
 */
const MAX_SEEN_KEYS = 2000;

/** Absolute path of the crash history inside `logsDir`. */
function crashLogPath(logsDir) {
  return path.join(String(logsDir || ""), CRASH_LOG_BASENAME);
}

/** Absolute path of the seen-set, beside the history. */
function crashStatePath(logsDir) {
  return path.join(String(logsDir || ""), CRASH_STATE_BASENAME);
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
 * them; `baselined: false` is the honest answer in both cases, and the scan's
 * baseline then re-establishes itself over whatever `seen` already held.
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

module.exports = {
  CRASH_LOG_BASENAME,
  CRASH_STATE_BASENAME,
  MAX_CRASH_LOG_LINES,
  crashLogPath,
  crashStatePath,
  readSeenState,
  writeSeenState,
  armCrashCollector,
  readCrashLogLines,
  appendCrashLog,
  formatCrashLine,
};
