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
//   1. KIROCREW_HOME env override wins.
//   2. A legacy ~/.kirocrew dir present at boot ALWAYS wins, marker or not:
//      migration force-copies legacy over ~/.kiro/crew and deletes legacy
//      ("legacy always wins" -- covers downgrade write-backs where an old
//      release re-populated ~/.kirocrew after a completed migration).
//      Canonical-dir existence is NEVER consulted -- only the marker+legacy
//      combination matters, and legacy presence forces migration either way.
//   3. No legacy dir -> canonical ~/.kiro/crew (marked-complete or fresh).
//
// Electron consumes this in two distinct ways:
//   - resolveHome(): the PRE-SPAWN read home -- which directory's
//     config.json content will govern this launch. When legacy exists its
//     content is about to be force-copied over canonical, so the legacy
//     file IS the winning content even though the backend's post-migration
//     data root is canonical.
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

function canonicalHome(os = nodeOs, path = nodePath) {
  return path.join(os.homedir(), ".kiro", "crew");
}

function legacyHome(os = nodeOs, path = nodePath) {
  return path.join(os.homedir(), ".kirocrew");
}

/**
 * The pre-spawn read home: whichever directory's config content will govern
 * this launch under the backend's migration rules.
 * @param {{env?: object, os?: object, path?: object, fs?: object}} deps
 * @returns {string}
 */
function resolveHome({ env = process.env, os = nodeOs, path = nodePath, fs = nodeFs } = {}) {
  if (env.KIROCREW_HOME) return env.KIROCREW_HOME;
  const legacy = legacyHome(os, path);
  // Legacy always wins when present (paths.py force-copies it over the
  // canonical home, marker or not). Canonical existence is deliberately not
  // checked -- the backend never consults it either.
  try { if (fs.existsSync(legacy)) return legacy; } catch { /* treat as absent */ }
  return canonicalHome(os, path);
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
  if (env.KIROCREW_HOME) return [path.join(env.KIROCREW_HOME, ".local_secret")];
  return [
    path.join(canonicalHome(os, path), ".local_secret"),
    path.join(legacyHome(os, path), ".local_secret"),
  ];
}

module.exports = { resolveHome, secretCandidates, canonicalHome, legacyHome };
