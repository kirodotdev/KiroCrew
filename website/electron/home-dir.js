// Desktop data-home resolution -- a deliberate 1:1 mirror of the backend's
// decision tree in src/kiro_crew/config/paths.py (config_dir ->
// _maybe_migrate_legacy_home). PARITY-GATED: test/fixtures/
// home-resolution-cases.json is the shared contract -- this module's tests
// assert the JS answers per case, and test/test_home_resolution_parity.py
// runs the REAL backend resolver against the same cases and asserts the
// config content Electron would read pre-spawn equals the content the
// backend serves post-boot. Change either implementation and the other
// side's suite fails until the fixture (and both mirrors) agree again.
//
// The backend contract being mirrored (paths.py):
//   1. A valid KIROCREW_HOME env override wins. INVALID overrides -- a
//      filesystem/drive root ("/" or "C:\") or a POSIX system dir
//      (/usr, /System, /etc) -- are rejected (paths.py _valid_override_home)
//      and fall through to the rules below, so both sides agree on which
//      overrides are honored.
//   2. The completion marker is AUTHORITATIVE: once ~/.kiro/crew holds the
//      migration marker, canonical wins even if a legacy ~/.kirocrew dir is
//      still present -- that legacy dir is resurrection debris (e.g. a
//      downgrade write-back after a completed migration) and is NEVER
//      promoted or re-migrated. This design has no rollback path.
//   3. No marker + a legacy ~/.kirocrew present -> legacy wins: migration is
//      pending and force-copies legacy over ~/.kiro/crew, so the legacy
//      content is the winning pre-spawn read.
//   4. No marker + no legacy -> canonical ~/.kiro/crew (fresh install).
//
// Electron consumes this in two distinct ways:
//   - resolveHome(): the PRE-SPAWN read home -- which directory's
//     config.json content will govern this launch. When migration is pending
//     (no marker, legacy present) the legacy content is about to be
//     force-copied over canonical, so the legacy file IS the winning content
//     even though the backend's post-migration data root is canonical.
//   - secretCandidates(): the POST-SPAWN secret location, re-resolved at
//     call time. By then migration has run: the secret lives in canonical
//     (holding the migrated, legacy-derived value); the legacy path remains
//     only for the backend's migration-FAILURE pin (paths.py falls back to
//     the still-intact legacy home so a botched copy never loses data).
//
// Boot-time side effects (mkdir pre-create, PYTHONPYCACHEPREFIX) must use
// canonicalHome() directly, never resolveHome(): writing into a legacy dir
// -- or recreating one -- re-arms the migration on every launch (issue #483).

const nodeOs = require("os");
const nodePath = require("path");
const nodeFs = require("fs");

// Mirror of paths.py MIGRATION_MARKER_NAME -- the completion-marker filename
// written inside the canonical home once migration verified-completes.
const MIGRATION_MARKER_NAME = ".data-home-ready";

function canonicalHome(os = nodeOs, path = nodePath) {
  return path.join(os.homedir(), ".kiro", "crew");
}

function legacyHome(os = nodeOs, path = nodePath) {
  return path.join(os.homedir(), ".kirocrew");
}

/**
 * The resolved KIROCREW_HOME override iff set AND valid, else null. Mirrors
 * paths.py _valid_override_home: the value is expanduser()'d + resolve()'d to a
 * normalized absolute path (so a literal "~/foo" or a relative override matches
 * Python instead of being read verbatim), then a filesystem/drive root or a
 * POSIX system dir (/usr, /System, /etc) is refused so the backend and Electron
 * never diverge on which override is honored.
 * @returns {string|null}
 */
function validOverride(env, os, path) {
  const raw = env.KIROCREW_HOME;
  if (!raw) return null;
  // Expand a leading "~" against the home dir (path.resolve treats "~" as a
  // literal segment, unlike Python's expanduser()), then normalize to absolute.
  let expanded = raw;
  if (raw === "~") expanded = os.homedir();
  else if (raw.startsWith("~/") || raw.startsWith("~\\")) {
    expanded = path.join(os.homedir(), raw.slice(2));
  }
  const p = path.resolve(expanded);
  if (p === path.dirname(p)) return null; // "/" (POSIX) or "C:\" (Windows) root
  const segs = p.split(path.sep);
  if (segs[0] === "" && ["usr", "System", "etc"].includes(segs[1])) return null;
  return p; // normalized absolute path, matching Python expanduser().resolve()
}

/**
 * The pre-spawn read home: whichever directory's config content will govern
 * this launch under the backend's migration rules.
 * @param {{env?: object, os?: object, path?: object, fs?: object}} deps
 * @returns {string}
 */
function resolveHome({ env = process.env, os = nodeOs, path = nodePath, fs = nodeFs } = {}) {
  const override = validOverride(env, os, path);
  if (override) return override;
  const canonical = canonicalHome(os, path);
  // The completion marker is AUTHORITATIVE (paths.py): once migration
  // completed, canonical wins even if a legacy dir is still present or
  // reappears -- it is debris, never promoted or re-migrated.
  try { if (fs.existsSync(path.join(canonical, MIGRATION_MARKER_NAME))) return canonical; } catch { /* treat as absent */ }
  const legacy = legacyHome(os, path);
  // No marker + legacy present -> migration pending; paths.py force-copies
  // legacy over canonical, so the legacy content is the winning pre-spawn read.
  try { if (fs.existsSync(legacy)) return legacy; } catch { /* treat as absent */ }
  return canonical;
}

/**
 * Ordered candidate paths for .local_secret, re-resolved at call time
 * (post-spawn: migration has run by the time a token is fetched).
 * Canonical first -- it holds the migrated secret; legacy second -- only the
 * backend's migration-failure fallback still serves from there. An env
 * override is authoritative and sole.
 * @returns {string[]}
 */
function secretCandidates({ env = process.env, os = nodeOs, path = nodePath } = {}) {
  const override = validOverride(env, os, path);
  if (override) return [path.join(override, ".local_secret")];
  return [
    path.join(canonicalHome(os, path), ".local_secret"),
    path.join(legacyHome(os, path), ".local_secret"),
  ];
}

module.exports = { resolveHome, secretCandidates, canonicalHome, legacyHome };
