// Recover the user's configured PATH on macOS for the bundled Gateway spawn.
//
// A GUI-launched .app inherits launchd's minimal environment rather than a
// login shell's, so `process.env.PATH` is typically just
// `/usr/bin:/bin:/usr/sbin:/sbin`. Every Gateway descendant — agent shell
// tools, MCP servers, ACP runtimes — inherits that, so a user-installed CLI
// living outside the backend's fixed `augmented_path()` list is unresolvable
// in the app even though the same command works in Terminal (issue #2367).
//
// `launchctl setenv PATH ...` writes the launchd USER DOMAIN, which a GUI
// `.app` does not re-read per launch. That is exactly why "export PATH and
// relaunch" does not help, and why the domain has to be read explicitly here.
//
// Extracted from main.js so it can be unit-tested without Electron: every
// dependency is injected and the functions are pure apart from the one
// `execFileSync` call.

// Absolute path, never a bare name: this runs before PATH is trustworthy,
// which is the whole point of the module.
const LAUNCHCTL = "/bin/launchctl";
// Bounds the launchd READ, so a pathological domain value cannot be slurped
// into memory. Deliberately the only size limit in this module: a merged-PATH
// cap on top of it would be dead weight (the environment block limit that
// makes spawn fail with E2BIG is ~1MB, which a read bounded here cannot
// approach) and actively harmful, because an inherited PATH already larger
// than the cap would silently suppress every appended entry — turning the fix
// into a no-op exactly for the users with the most crowded PATH.
const MAX_LAUNCHD_READ_BYTES = 32 * 1024;
// `launchctl getenv` is a local IPC round-trip and returns effectively
// instantly. The timeout exists so a wedged launchd cannot hang the Electron
// main process indefinitely — not because a slow read is expected.
const READ_TIMEOUT_MS = 2000;

/**
 * Read `PATH` from the launchd user domain.
 *
 * @param {object} deps
 * @param {Function} deps.execFileSync - `child_process.execFileSync`
 * @param {string} [deps.platform] - `process.platform` (defaults to it)
 * @returns {string|null} The raw value, or `null` on any non-darwin platform,
 *   any failure, or an unset/empty variable. Callers treat `null` as "nothing
 *   to merge" and leave the inherited PATH alone.
 */
function readLaunchdPath({ execFileSync, platform = process.platform } = {}) {
  if (platform !== "darwin") return null;
  try {
    const out = execFileSync(LAUNCHCTL, ["getenv", "PATH"], {
      encoding: "utf8",
      timeout: READ_TIMEOUT_MS,
      maxBuffer: MAX_LAUNCHD_READ_BYTES,
      // launchctl writes nothing useful to stderr here, and inheriting the
      // parent's stdio would interleave it into the app's own log stream.
      stdio: ["ignore", "pipe", "ignore"],
    });
    const value = typeof out === "string" ? out.trim() : "";
    return value === "" ? null : value;
  } catch {
    // An unset variable exits non-zero, and `launchctl` may be absent or
    // wedged. None of those are actionable: the inherited PATH is still the
    // documented behaviour, so degrade silently to it.
    return null;
  }
}

/**
 * Split a `PATH` string into entries safe to hand to a child process.
 *
 * Rejects anything that could change which executable a bare command name
 * binds to in a way the user did not intend:
 *
 * - relative entries, which a child re-resolves against ITS OWN cwd, letting
 *   a working-directory-relative executable shadow the intended command
 *   (the same rule `env.py::_validated_bin_dir` applies backend-side);
 * - entries with a `..` segment, which obscure what directory is really being
 *   added (matches `validation.js`'s traversal rule for remote paths);
 * - entries containing NUL or a newline, which indicate a mangled value.
 *
 * @param {string|null|undefined} raw - A colon-separated PATH string
 * @returns {string[]} Accepted entries, in their original order
 */
function sanitizePathEntries(raw) {
  if (typeof raw !== "string" || raw === "") return [];
  return raw.split(":").filter((entry) => {
    if (entry === "" || !entry.startsWith("/")) return false;
    if (entry.includes("\0") || entry.includes("\n") || entry.includes("\r")) return false;
    return !entry.split("/").includes("..");
  });
}

/**
 * Merge the launchd-domain PATH into the inherited one.
 *
 * The inherited value is preserved VERBATIM and the launchd-only entries are
 * APPENDED. Both halves of that are deliberate:
 *
 * - *Appended*, because appending can only make a name resolve that resolved
 *   nowhere before — which is the reported failure — while prepending would let
 *   a user-writable directory shadow a system binary that already resolves. It
 *   mirrors how `env.py::augmented_path` appends the interpreter's own `bin`
 *   last, "as a pure fallback [that] resolves only names found nowhere else".
 * - *Verbatim*, because this function must never REMOVE a directory the Gateway
 *   would otherwise have had. Validation and the byte cap therefore apply only
 *   to what is being ADDED, exactly as `augmented_path` validates the
 *   directories it contributes and passes `base_path` through untouched. An odd
 *   entry already in the inherited PATH (relative, `..`, empty) is left where it
 *   is: rewriting it would silently stop resolving a command that resolves
 *   today, and hardening the inherited environment is not this function's job.
 *
 * Appended entries are de-duplicated against the inherited ones and against each
 * other, so the function is idempotent: feeding an already-merged value back in
 * adds nothing.
 *
 * @param {object} args
 * @param {string} [args.basePath] - The inherited `PATH`, used as-is
 * @param {string|null} [args.launchdPath] - Value from {@link readLaunchdPath}
 * @returns {{path: string, added: string[]}} The merged PATH and the entries
 *   the merge contributed (for logging).
 */
function mergeGatewayPath({ basePath = "", launchdPath = null } = {}) {
  const base = typeof basePath === "string" ? basePath : "";
  // Split only to compare against — never to rewrite. Empty segments are
  // excluded from the comparison set so they cannot swallow a real entry.
  const seen = new Set(base.split(":").filter((entry) => entry !== ""));

  const added = [];
  for (const entry of sanitizePathEntries(launchdPath)) {
    if (seen.has(entry)) continue;
    seen.add(entry);
    added.push(entry);
  }

  if (added.length === 0) return { path: base, added };
  // Always a ":" between the inherited value and the appended tail, even when
  // the inherited value already ends in one. A trailing ":" is a ZERO-LENGTH
  // ENTRY, which POSIX defines as the cwd -- so "/usr/bin:" is [/usr/bin, cwd],
  // and appending to it must yield [/usr/bin, cwd, <new>], i.e. the "::" form.
  // Collapsing that to a single ":" would DELETE the cwd entry and stop
  // resolving a command that resolves today, which is exactly the removal this
  // function exists to prevent. The doubled separator is correct output, not a
  // formatting bug -- see the test that pins it.
  return { path: base === "" ? added.join(":") : `${base}:${added.join(":")}`, added };
}

/**
 * Compute the `PATH` the bundled Gateway should be spawned with.
 *
 * Returns `null` when there is nothing to change — a non-darwin platform, an
 * unreadable launchd domain, or a domain that contributes no new directory —
 * so the caller can leave the inherited environment completely untouched
 * rather than rewriting it to an equal value.
 *
 * @param {object} deps
 * @param {Function} deps.execFileSync - `child_process.execFileSync`
 * @param {string} [deps.platform] - `process.platform`
 * @param {string} [deps.basePath] - The inherited `PATH`
 * @returns {{path: string, added: string[]}|null}
 */
function resolveGatewayPath({ execFileSync, platform = process.platform, basePath = "" } = {}) {
  const launchdPath = readLaunchdPath({ execFileSync, platform });
  if (launchdPath === null) return null;
  const merged = mergeGatewayPath({ basePath, launchdPath });
  return merged.added.length === 0 ? null : merged;
}

module.exports = {
  LAUNCHCTL,
  MAX_LAUNCHD_READ_BYTES,
  READ_TIMEOUT_MS,
  readLaunchdPath,
  sanitizePathEntries,
  mergeGatewayPath,
  resolveGatewayPath,
};
