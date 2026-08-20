// Desktop data-home resolution -- a mirror of the backend's data-home resolver
// in src/kiro_crew/config/paths.py (config_dir -> _resolve_default_home):
//   1. A valid KIROCREW_HOME env override wins. INVALID overrides -- a
//      filesystem/drive root ("/" or "C:\") or a POSIX system dir
//      (/usr, /System, /etc) -- are rejected (paths.py _valid_override_home)
//      and fall through to the default, so both sides agree on which overrides
//      are honored.
//   2. Otherwise the default home ~/.kiro/crew.
//
// Electron consumes this in two ways:
//   - resolveHome(): the data home whose config.json content governs this
//     launch.
//   - secretCandidates(): the .local_secret location, re-resolved at call time.
//     The backend reads .local_secret only from config_dir() (the override or
//     the default home), so this returns that single location.
//
// Boot-time side effects (mkdir pre-create, PYTHONPYCACHEPREFIX) use
// canonicalHome() directly.

const nodeOs = require("os");
const nodePath = require("path");

function canonicalHome(os = nodeOs, path = nodePath) {
  return path.join(os.homedir(), ".kiro", "crew");
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
 * The data home whose config content governs this launch: a valid
 * KIROCREW_HOME override, else the default ~/.kiro/crew.
 * @param {{env?: object, os?: object, path?: object}} deps
 * @returns {string}
 */
function resolveHome({ env = process.env, os = nodeOs, path = nodePath } = {}) {
  return validOverride(env, os, path) || canonicalHome(os, path);
}

/**
 * Candidate paths for .local_secret, re-resolved at call time. Mirrors the
 * backend, which reads it from config_dir() only -- the override when set, else
 * the default home.
 * @returns {string[]}
 */
function secretCandidates({ env = process.env, os = nodeOs, path = nodePath } = {}) {
  const override = validOverride(env, os, path);
  const home = override || canonicalHome(os, path);
  return [path.join(home, ".local_secret")];
}

module.exports = { resolveHome, secretCandidates, canonicalHome };
