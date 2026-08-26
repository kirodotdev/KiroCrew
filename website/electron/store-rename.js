// Carry settings across the npm `name` rename by SEEDING the new store's file.
//
// Electron derives the userData directory from the npm package name, so renaming it
// ("kirocrew-electron-mac" -> "kirocrew-desktop" / "kirocrew-desktop-nightly")
// repoints electron-store at a brand-new directory and every setting in the old one
// is silently orphaned. The rename itself is correct and load-bearing — a shared name
// made both channels write ONE userData dir and ONE updater cache, so uninstalling
// either destroyed the other's window state and pending download — but nothing
// carried the existing settings across.
//
// updateChannel is why this is a bug rather than an inconvenience. It is the
// stable<->insider switcher, and resolveChannel() treats an unset preference as
// "follow stable", so an insider user whose preference is orphaned is moved onto the
// stable feed with no consent and no message — exactly what that function's contract
// says a channel decision must never do implicitly.
//
// WHY SEEDING, AND NOT A MERGE. The hard part is never copying values; it is proving
// the destination is untouched. Value comparison cannot do it, because every default
// is REACHABLE — clearing your last remote host leaves `{}`, clearing the accent
// leaves "" — and no persisted flag can do it either, because a retry has to answer
// two questions that pull apart: "may I read the legacy store?" (yes, even though
// config.json now exists) and "is the destination untouched?" (no, the user has had a
// session with it).
//
// Writing the file BEFORE electron-store opens it collapses both questions into one
// filesystem fact, and keeps no bookkeeping at all:
//
//   file absent    -> there is nothing to overwrite, by definition
//   seeding throws -> file stays absent -> the next launch retries identically
//   seeding works  -> file exists       -> never eligible again
//
// Deliberately NOT handled here: the legacy directory is left in place. It may still
// be the OTHER channel's live store (both channels shared it before the rename), so
// removing it is the same cross-channel hazard the rename exists to end. The same
// reasoning governs the updater cache in build/installer.nsh, which declines to
// remove the shared pre-rename directory.

const fs = require("fs");
const path = require("path");

// The pre-rename npm package name, and so the pre-rename userData directory. Pinned
// rather than derived: the rename removed it from package.json, so there is nothing
// left to compute it from.
const LEGACY_STORE_NAME = "kirocrew-electron-mac";

// electron-store's file name inside a userData directory.
const STORE_FILE_NAME = "config.json";

// Attempts at reading the legacy store before giving up. More than one because a
// failed read cannot be retried on a LATER launch: the seed writes nothing, main.js
// constructs the store regardless, and the destination then exists for good. Small
// because the realistic cause is a momentary lock, not a lasting condition.
const LEGACY_READ_ATTEMPTS = 3;

// Pause between failed read attempts. Without one the retries all land in the same
// tick, and a lock that outlives a microsecond — the other channel flushing its own
// store, a scanner holding the file — defeats all three at once, making them one
// attempt in disguise. Growing, because a lock that survived the first wait is more
// likely a slow holder than a fast one. The wait only ever runs on a launch that is
// already about to lose the settings, so it costs the common path nothing.
const LEGACY_READ_BACKOFF_MS = [80, 240];

// Synchronous sleep. This runs at boot before any window exists, so blocking the
// main process is acceptable and an async hop is not available to the caller —
// main.js must construct the store immediately after this returns.
function sleepSync(ms) {
  // A non-finite or non-positive timeout must never reach Atomics.wait: an
  // undefined/NaN timeout means WAIT FOREVER, which here would hang boot with no
  // window on screen — strictly worse than any settings loss this module exists
  // to prevent. Guarded structurally rather than by call-site inspection.
  const t = Number(ms);
  if (!Number.isFinite(t) || t <= 0) return;
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, t);
  } catch {
    /* SharedArrayBuffer unavailable — proceed without the wait */
  }
}

// Settings worth carrying, as an ALLOWLIST rather than a wholesale copy: the legacy
// file is a full store dump that may contain keys a newer build has retired, and
// copying it verbatim would resurrect settings this build no longer understands.
//
// No key needs excluding for ambiguity here. Seeding only ever happens when the
// destination does not exist, so there is no user choice to override — which is what
// lets the behavioural settings (runLocalGateway, sshTimeoutMs) come along. Dropping
// those would mean a user who runs as a pure client gets an unwanted local gateway on
// the first launch after the rename.
//
// Values equal to their defaults are carried too, deliberately. The destination never
// exists when the seed runs, so seeding a default-equal value is indistinguishable
// from electron-store writing that default itself — filtering them out could only be
// done against a hand-copied defaults table, whose silent divergence from main.js
// would misclassify a real legacy choice as "default, drop it".
const MIGRATED_KEYS = [
  // Behavioural: an orphaned value silently changes which release channel the user
  // follows, or starts a gateway they run as a pure client to avoid.
  "updateChannel",
  "runLocalGateway",
  // Remote-connection setup, which is real configuration work to redo. The
  // single-host pair predates remoteHosts and is consumed by a SECOND migration that
  // runs right after this one (host-config.js migrateRemoteHostConfig converts it to
  // the per-port map). Dropping the pair loses the connection twice over: the seed
  // carries an empty map, and the host migration then finds nothing to convert.
  "remoteHosts",
  "remoteHost",
  "kirocrewBinPath",
  "sshTimeoutMs",
  // Comfort, but immediately visible as "the app forgot me".
  "windowState",
  "themeAccent",
  "globalHotkey",
  "linuxFrameless",
];

/**
 * electron-store's config.json inside the PRE-RENAME userData directory, derived from
 * the live one.
 *
 * Derived rather than rebuilt per platform on purpose: Electron picks userData
 * differently on each OS (`%APPDATA%\<name>`, `~/Library/Application Support/<name>`,
 * `~/.config/<name>`), so hand-rolling "APPDATA or ~/.config" silently resolved to a
 * nonexistent `~/.config` path on macOS — the lookup ENOENT'd, the failure was
 * swallowed, and every setting stayed orphaned there. The legacy directory is always a
 * SIBLING of the current one, whatever Electron chose.
 *
 * @param {string} userDataPath app.getPath("userData")
 * @param {string} [storeFileName] electron-store file inside the userData directory
 * @returns {string} path to the legacy store file, or "" if it cannot be derived
 */
function legacyStoreFile(userDataPath, storeFileName = STORE_FILE_NAME) {
  if (typeof userDataPath !== "string" || !userDataPath) return "";
  // Pick the parser from the path's own SHAPE rather than process.platform, so all
  // three layouts stay testable on one machine and a win32 path is never parsed with
  // POSIX semantics.
  const impl = /^[A-Za-z]:[\\/]|^\\\\/.test(userDataPath) ? path.win32 : path.posix;
  const normalized = userDataPath.replace(/[\\/]+$/, "");
  const parent = impl.dirname(normalized);
  if (!parent || parent === normalized) return "";
  return impl.join(parent, LEGACY_STORE_NAME, storeFileName);
}

/**
 * Seed a not-yet-existing store file with the settings orphaned by the rename.
 *
 * MUST be called before `new Store(...)`: electron-store writes its defaults to
 * config.json on construction, after which the file always exists and this can never
 * run.
 *
 * Fails SOFT and SILENT. It runs at boot, before any window or error dialog exists, so
 * nothing here may throw — losing this convenience must never cost the user their app.
 * An absent legacy store is also the overwhelmingly common case (every fresh install),
 * so it is not worth a log line on every launch.
 *
 * @param {string} userDataPath app.getPath("userData")
 * @param {object} [opts]
 * @param {string} [opts.storeFileName] which electron-store file to seed; defaults to
 *   the shell's main config.json. Other stores in the same userData directory (e.g.
 *   Mochi's mochi-machine.json) are orphaned by the same rename and reuse this
 *   mechanism with their own file name and key allowlist.
 * @param {string[]} [opts.keys] allowlist of top-level keys to carry from the legacy
 *   file; defaults to the main store's MIGRATED_KEYS. Must match the RAW file shape:
 *   electron-store resolves dotted keys via dot-notation, so a namespaced store's
 *   user data lives under its top-level namespace segment, not the dotted spelling.
 * @param {() => object|null} [opts.readLegacy] injected for tests
 * @param {(file:string, data:string) => void} [opts.writeFile] injected for tests
 * @param {(msg:string) => void} [opts.log]
 * @param {(ms:number) => void} [opts.sleep] injected for tests; defaults to a real
 *   synchronous wait between failed legacy-read attempts
 * @returns {boolean} whether a file was written
 */
function seedRenamedStore(userDataPath, {
  storeFileName = STORE_FILE_NAME,
  keys = MIGRATED_KEYS,
  readLegacy = null,
  writeFile = null,
  log = null,
  sleep = sleepSync,
} = {}) {
  const target = path.join(userDataPath || "", storeFileName);
  // This invocation's temp sibling, named outside the try so the failure path can
  // remove exactly the file this call created and nothing else.
  let tmp = "";
  try {
    // The ONLY eligibility condition. An existing destination is the user's, whether
    // from a prior launch or from an install that already crossed the rename.
    if (fs.existsSync(target)) return false;

    const read = readLegacy || (() => {
      const file = legacyStoreFile(userDataPath, storeFileName);
      if (!file) return null;
      return JSON.parse(fs.readFileSync(file, "utf8"));
    });
    // A read that fails is the one case with no second chance: the seed writes
    // nothing, main.js constructs the store anyway (it must — boot cannot be held
    // hostage to a convenience), and the destination then exists forever. Retry a
    // couple of times in-process to ride out the realistic cause, a momentary lock
    // from the other channel or a scanner touching the same file.
    //
    // If it still fails the settings are lost and we say so in the log. That is
    // accepted deliberately: the alternatives are refusing to boot, or persisting a
    // retry flag whose two meanings ("may I read?" and "is the destination
    // untouched?") cannot both be answered correctly — the ambiguity this design
    // exists to remove.
    let legacy = null;
    for (let attempt = 0; attempt < LEGACY_READ_ATTEMPTS; attempt += 1) {
      try {
        legacy = read();
        break;
      } catch (err) {
        // Absent is final and silent: there was never anything to carry.
        if (err && err.code === "ENOENT") return false;
        // Corrupted is final and loud: waiting cannot fix malformed JSON — the same
        // bytes fail identically on every attempt, this launch or any other.
        if (err instanceof SyntaxError) {
          if (log) log(`pre-rename store is not valid JSON, settings not carried: ${err.message}`);
          return false;
        }
        if (attempt === LEGACY_READ_ATTEMPTS - 1) {
          if (log) log(`could not read the pre-rename store, settings not carried: ${err && err.message}`);
          return false;
        }
        sleep(LEGACY_READ_BACKOFF_MS[Math.min(attempt, LEGACY_READ_BACKOFF_MS.length - 1)]);
      }
    }
    if (!legacy || typeof legacy !== "object") return false;

    const seed = {};
    for (const key of keys) {
      // Key ABSENCE, not emptiness, means "the legacy store has nothing to say". An
      // explicit "" is a real choice — main.js documents globalHotkey null as
      // "platform default" and "" as "disabled" — so treating empty as absent would
      // silently re-enable a hotkey the user turned off. Default-equal values are
      // carried for the same reason absence is respected: the destination does not
      // exist, so re-stating a default is a no-op, and filtering would require a
      // second defaults table that could drift from the store's real one.
      if (!Object.prototype.hasOwnProperty.call(legacy, key)) continue;
      const value = legacy[key];
      if (value === undefined) continue;
      seed[key] = value;
    }
    const seededKeys = Object.keys(seed);
    if (!seededKeys.length) return false;

    // ATOMIC: write a temp sibling, then rename. config.json is what electron-store
    // parses on the next launch, so a torn write is worse than no migration at all —
    // the store would either throw on malformed JSON or silently reset. A rename
    // within one directory is atomic, so the destination only ever appears complete.
    //
    // The sibling is UNIQUE per invocation. Two first launches can both reach the
    // seed (Electron's single-instance lock is taken later), and a shared temp name
    // would let one process truncate the sibling while the other renames it —
    // exposing a partial config.json that electron-store then fails to parse on
    // every later boot. With per-invocation siblings both renames are whole-file:
    // last writer wins, and both seeds are complete JSON from the same legacy
    // source. The pid alone would collide only via pid reuse against an orphan left
    // by a crash; the random suffix closes that at no cost.
    const tmpName = `${target}.migrating.${process.pid}.${Math.random().toString(36).slice(2, 8)}`;
    tmp = tmpName;
    const write = writeFile || ((file, data) => {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, data);
    });
    write(tmpName, `${JSON.stringify(seed, null, "\t")}\n`);
    fs.renameSync(tmpName, target);
    if (log) log(`carried ${seededKeys.length} setting(s) from the pre-rename store: ${seededKeys.join(", ")}`);
    return true;
  } catch (err) {
    // Remove ONLY this invocation's temp sibling — never `target`. The destination
    // is not this call's to delete: a rival first launch may have just renamed its
    // own complete seed there, and deleting `target` would destroy the winner's
    // freshly migrated settings and hand the user defaults instead. The atomic
    // rename already guarantees the destination is either absent or complete.
    if (tmp) {
      try { fs.rmSync(tmp, { force: true }); } catch { /* best effort */ }
    }
    if (log) log(`skipped: ${(err && err.message) || err}`);
    return false;
  }
}

module.exports = {
  seedRenamedStore,
  legacyStoreFile,
  MIGRATED_KEYS,
  LEGACY_STORE_NAME,
};
