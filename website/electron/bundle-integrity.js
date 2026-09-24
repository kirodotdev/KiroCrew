"use strict";
//
// Detects a bundled backend tree whose Python stdlib is only PARTLY on disk, so
// the launcher can say "the install is still finishing" instead of exec'ing a
// half-extracted interpreter and surfacing a raw ModuleNotFoundError.
//
// Why this is a real state and not paranoia: the Windows NSIS installer (and an
// auto-update swap) extracts `resources/backend-dist/` incrementally, roughly in
// directory order, while the app is already launchable — `runAfterFinish` starts
// the app as the installer finishes. Launch inside that window and `python.exe`
// plus the top-level stdlib FILES are present while late-alphabet PACKAGE dirs
// are not. `pathlib.py` then imports cleanly and dies on `from urllib.parse
// import quote_from_bytes`, which reads to a user as a corrupt install and to a
// bug report as an inexplicable missing-stdlib crash.
//
// Pure and injectable (no electron, no real fs) so it is unit-testable.

// Stdlib packages the gateway's own import chain needs, spread across the
// alphabet so a partial extraction is caught wherever it stopped.
//
// Probed via each package's `__init__.py`, not the directory: an extractor
// creates a directory before writing its contents, so an empty `Lib/zoneinfo/`
// would satisfy a directory check while `import zoneinfo` still fails. The
// `__init__.py` is also the exact file Python needs to treat the directory as a
// package, so its presence is the same question the interpreter will ask.
// Probing a package rather than a top-level module stays essential: a truncated
// extraction leaves top-level .py files in place long before the late package
// dirs land, so probing `pathlib.py` would report a broken bundle as healthy.
//
// The tail matters as much as the middle. Extraction proceeds roughly in
// directory order, so the last names are the likeliest to be missing when a
// launch races it, and `zipfile`/`zoneinfo` sort after everything else while
// still being module-scope imports on the gateway's chain (cron, cli_setup,
// portability). Stopping the list at `urllib` — the package the observed failure
// hit — would leave exactly the most-likely-missing dirs unchecked.
const REQUIRED_STDLIB_PARTS = [
  "asyncio",
  "collections",
  "concurrent",
  "ctypes",
  "email",
  "encodings",
  "http",
  "importlib",
  "json",
  "logging",
  "multiprocessing",
  "re",
  "sqlite3",
  "urllib",
  "xml",
  "xmlrpc",
  "zipfile",
  "zoneinfo",
];

/**
 * Locate the stdlib root inside a bundled backend tree, across both layouts the
 * builder emits: Windows PBS puts it at `Lib/`, POSIX PBS at
 * `lib/python3.<minor>/`. The POSIX minor version is discovered by scanning
 * rather than hardcoded, so a Python bump does not silently turn this gate off.
 *
 * The POSIX layout is probed FIRST, and that order is load-bearing on a
 * case-insensitive filesystem (macOS APFS by default). There, `Lib` and `lib`
 * name the same directory, so a `Lib/` probe against a POSIX tree succeeds and
 * yields the dir holding `python3.<minor>/` — not the stdlib — which would make
 * every required part look missing and refuse a perfectly good launch. The POSIX
 * branch self-validates by requiring a `python3.<minor>` child, so on Windows
 * (where the same case-insensitive match sends it into the real `Lib/`) it finds
 * no such child and correctly falls through.
 *
 * @param {{existsSync: Function, readdirSync: Function}} fs
 * @param {typeof import("path")} path
 * @param {string} backendRoot  the `…/backend-dist/kirocrew-backend` directory
 * @returns {string|null} absolute stdlib dir, or null if none is present yet
 */
function resolveStdlibDir(fs, path, backendRoot) {
  const root = backendRoot || "";
  const posixLib = path.join(root, "lib");
  if (fs.existsSync(posixLib)) {
    let entries = [];
    try { entries = fs.readdirSync(posixLib); } catch { entries = []; }
    // Sort so the choice is deterministic if a tree ever carries two minors.
    const pyDir = entries.filter((e) => /^python3\.\d+$/.test(e)).sort()[0];
    if (pyDir) {
      const resolved = path.join(posixLib, pyDir);
      if (fs.existsSync(resolved)) return resolved;
    }
  }
  const winLib = path.join(root, "Lib");
  return fs.existsSync(winLib) ? winLib : null;
}

/**
 * True if `backendRoot` holds an actual Python interpreter, i.e. the tree this
 * gate understands. The legacy flat PyInstaller layout carries a single frozen
 * executable and NO stdlib directory, so probing it for `Lib/` would report a
 * healthy bundle as incomplete; requiring an interpreter keeps the gate scoped
 * to the python-build-standalone trees the current builder emits.
 *
 * @param {{existsSync: Function, readdirSync: Function}} fs
 * @param {typeof import("path")} path
 * @param {string} backendRoot
 * @returns {boolean}
 */
function hasBundledInterpreter(fs, path, backendRoot) {
  const root = backendRoot || "";
  if (fs.existsSync(path.join(root, "python.exe"))) return true;
  const posixBin = path.join(root, "bin");
  if (!fs.existsSync(posixBin)) return false;
  let entries;
  try { entries = fs.readdirSync(posixBin); } catch { return false; }
  return entries.some((e) => /^python3(\.\d+)?$/.test(e));
}

/**
 * Which required stdlib parts are absent from a bundled backend tree.
 *
 * Returns [] for a tree that carries no bundled interpreter at all (the legacy
 * flat layout, or a path that is not a backend tree) — this gate only speaks
 * about interpreter trees, and must never block a launch it does not understand.
 *
 * @param {{existsSync: Function, readdirSync: Function}} fs
 * @param {typeof import("path")} path
 * @param {string} backendRoot  the `…/backend-dist/kirocrew-backend` directory
 * @returns {string[]} missing part names ([] = complete or not applicable).
 *   `["Lib"]` means the stdlib root itself is not there yet, i.e. extraction has
 *   barely begun.
 */
function findMissingBundleParts(fs, path, backendRoot) {
  if (!hasBundledInterpreter(fs, path, backendRoot)) return [];
  const stdlib = resolveStdlibDir(fs, path, backendRoot);
  if (!stdlib) return ["Lib"];
  return missingRequiredParts(fs, path, stdlib);
}

/** The REQUIRED_STDLIB_PARTS whose `__init__.py` is not yet under `stdlib`. */
function missingRequiredParts(fs, path, stdlib) {
  return REQUIRED_STDLIB_PARTS.filter(
    (rel) => !fs.existsSync(path.join(stdlib, ...rel.split("/"), "__init__.py"))
  );
}

/**
 * User-facing one-liner for a bundle that is not fully on disk. Frames it as an
 * unfinished install (retry is the fix) rather than a corrupt one (reinstall) —
 * waiting for extraction to finish genuinely resolves it.
 *
 * Reports a COUNT rather than the package names: the names are Python stdlib
 * internals a reader cannot act on, and the refusal is already logged with the
 * full list for a bug report. The count conveys the one actionable thing — how
 * much is still arriving.
 *
 * @param {string[]} missing  from findMissingBundleParts
 * @param {{autoRetry?: boolean}} [options]  `autoRetry` when the dialog carrying
 *   this text re-probes the bundle and retries by itself (a pre-spawn refusal);
 *   omit it where the user has to click (a crash the matcher reclassified, whose
 *   missing module the probe cannot see, so no auto-retry is armed).
 * @returns {string}
 */
function describeIncompleteBundle(missing, { autoRetry = false } = {}) {
  const count = Array.isArray(missing) ? missing.length : 0;
  const detail = count === 1 ? "1 component is" : `${count || "Some"} components are`;
  const next = autoRetry
    ? "Kiro Crew starts on its own once the install finishes, or click Retry to check now."
    : "Wait, then retry.";
  // The reinstall advice carries an explicit time anchor. Extraction can run for
  // minutes, so an unqualified "if this persists" reads as satisfied by two Retry
  // clicks twenty seconds apart — steering the user into the very reinstall this
  // message exists to prevent.
  // The remedy names the control on screen: the dialog has a Quit button and
  // nothing labelled "restart".
  return `Kiro Crew's bundled Python runtime is still being installed — ${detail} `
    + `not on disk yet. This can take a few minutes. ${next} If the install is `
    + "still unfinished after five minutes, quit and reopen Kiro Crew (the Quit "
    + "button below); if that does not help, reinstall the Kiro Crew app.";
}

// The installing dialog's title while parts are still arriving, and once the
// probe finds every part on disk. Both live here so the title and the message
// under it can never say opposite things: the completion paint swaps both.
const INSTALLING_DIALOG_TITLE = "Kiro Crew — installation still finishing";
const INSTALLED_DIALOG_TITLE = "Kiro Crew — installation finished";
// Painted into the dialog the moment the probe finds every part on disk; the
// window lingers on it briefly before closing and the boot retries. It exists so
// a user watching the count fall sees the transition land rather than the
// dialog vanishing mid-sentence.
const BUNDLE_COMPLETE_MESSAGE = "Installation finished — starting Kiro Crew…";

/**
 * Which bundle parts would make spawnGateway REFUSE to launch `bin` right now.
 *
 * This IS the pre-spawn refusal: spawnGateway refuses exactly when this returns
 * a non-empty list, and the "installation still finishing" dialog re-asks the
 * same function while it waits. One predicate for both keeps the probe at least
 * as strict as the launcher by construction -- were it laxer, the dialog would
 * retry a launch the launcher then refuses again, and the two would chase each
 * other every probe tick. Two conditions refuse:
 *   - a bundled interpreter tree missing required stdlib packages
 *     (findMissingBundleParts);
 *   - the Windows `bin\kirocrew.cmd` shim present while `python.exe` beside it
 *     has not landed (findMissingBundleParts is silent there by design -- no
 *     interpreter means "not a tree it understands" -- so the interpreter is
 *     named as a part of its own, ahead of whichever required packages are
 *     also still missing, so the count the dialog paints only ever falls).
 *
 * @param {{existsSync: Function, readdirSync: Function}} fs
 * @param {typeof import("path")} path
 * @param {string} bin  the launcher findKirocrewBin resolved
 * @returns {string[]|null} part names still to arrive ([] = the launcher would
 *   spawn); null when `bin` is not inside a bundled tree at all -- the bundled
 *   launcher itself has not been written yet, or a non-bundled install is in
 *   use -- which a caller must read as "unknown", never as "complete".
 */
function launchBlockingBundleParts(fs, path, bin) {
  if (typeof bin !== "string" || !bin.includes("backend-dist")) return null;
  const backendRoot = path.resolve(path.dirname(bin), "..");
  if (
    bin.endsWith("kirocrew.cmd")
    && !fs.existsSync(path.resolve(backendRoot, "python.exe"))
  ) {
    // Count the stdlib too: reporting the interpreter alone would paint "1
    // component" and then climb to eighteen the moment python.exe lands.
    const stdlib = resolveStdlibDir(fs, path, backendRoot);
    const parts = stdlib ? missingRequiredParts(fs, path, stdlib) : [...REQUIRED_STDLIB_PARTS];
    return ["python.exe", ...parts];
  }
  return findMissingBundleParts(fs, path, backendRoot);
}

/**
 * The "installation still finishing" dialog's next state after one probe.
 *
 * Pure so the count text and the complete -> retry transition can be tested
 * without Electron; the dialog paints `title` and `message` and, on `complete`,
 * fires its own Retry action.
 *
 * @param {string[]|null} missing  from launchBlockingBundleParts. null (launcher
 *   not resolvable yet) is still installing with an unknown count; only an
 *   empty ARRAY is complete.
 * @returns {{complete: boolean, title: string, message: string}}
 */
function nextInstallingDialogState(missing) {
  if (!Array.isArray(missing)) {
    // Still the auto-retry dialog: the probe is armed and will fire, so the
    // copy must keep promising that rather than switching to a manual "retry".
    return {
      complete: false,
      title: INSTALLING_DIALOG_TITLE,
      message: describeIncompleteBundle([], { autoRetry: true }),
    };
  }
  if (missing.length === 0) {
    return { complete: true, title: INSTALLED_DIALOG_TITLE, message: BUNDLE_COMPLETE_MESSAGE };
  }
  return {
    complete: false,
    title: INSTALLING_DIALOG_TITLE,
    message: describeIncompleteBundle(missing, { autoRetry: true }),
  };
}

// The line main.js logs immediately before each spawn. It delimits one launch
// attempt's child output in the append-only log, so the crash matcher can read
// THIS attempt rather than an earlier one. Owned here and imported by main.js so
// the writer and the reader cannot drift apart.
const SPAWN_MARKER = "---- spawning gateway; child stdout+stderr follows ----";

// Top-level stdlib names whose absence means "extraction is unfinished" rather
// than "this build is broken". Kept to modules the gateway's own import chain
// reaches, so an unrelated ModuleNotFoundError is never excused.
//
// Drift here is bounded and fails SAFE, unlike REQUIRED_STDLIB_PARTS: a name that
// should be listed and is not merely means that crash is reported as an ordinary
// failure (the status quo), never that a healthy launch is refused. Which is why
// this set is not gated at build time — a build gate would have to assert absence
// of nothing. Names compiled INTO the interpreter (`sys.builtin_module_names`,
// e.g. zlib) are deliberately excluded: they cannot be missing from an extraction,
// so listing them would only misdescribe what this set is for.
const STDLIB_MODULE_NAMES = new Set([
  ...REQUIRED_STDLIB_PARTS,
  "base64", "configparser", "contextlib", "dataclasses", "datetime", "enum",
  "functools", "gzip", "hashlib", "hmac", "inspect", "io", "ipaddress",
  "pathlib", "platform", "queue", "random", "secrets", "selectors", "shlex",
  "shutil", "signal", "socket", "ssl", "string", "struct", "subprocess",
  "tempfile", "textwrap", "threading", "traceback", "types", "typing", "uuid",
  "warnings", "weakref",
]);

/**
 * True if a launch-log tail shows the interpreter dying on a missing stdlib
 * module — the signature of a bundle that was still being written when it ran.
 *
 * This is the BACKSTOP that makes the pre-spawn check's incompleteness
 * survivable, and it is sound where a filesystem probe cannot be. Extraction
 * order is not ours to control: within a package `__init__.py` is written before
 * its siblings, so `import zoneinfo` can still fail a moment after
 * `zoneinfo/__init__.py` appears. Enumerating each package's private modules
 * would not converge (every CPython release may add one) and would couple the
 * launcher to internals — the exact drift that turns this guard into a permanent
 * refusal. Reading the actual failure asks the only question that settles it:
 * the interpreter itself reported which module it could not find.
 *
 * Deliberately narrow in two ways. The module must belong to the stdlib set, so a
 * missing THIRD-PARTY or first-party dependency (a genuine packaging defect, e.g.
 * `aiohttp` or `kiro_crew.foo`) is never excused as "still installing". And only
 * the CURRENT launch attempt is read: the log is append-only across launches, so
 * a traceback from an earlier attempt must not relabel this attempt's unrelated
 * failure (a SIGKILL, a bound port) as an unfinished install, which would show a
 * reassuring dialog over a real fault and leave Retry looping. `SPAWN_MARKER` is
 * the line `main.js` writes immediately before each spawn, so the text after its
 * last occurrence is exactly this attempt's child output.
 *
 * @param {string} text  launch-log tail
 * @returns {boolean}
 */
function isIncompleteBundleCrash(text) {
  // Only this attempt's output. An absent marker yields "", which matches nothing
  // below, so the ordinary failure path reports it rather than this one guessing.
  const s = currentAttemptLog(text);
  if (!s) return false;
  const missing = /ModuleNotFoundError: No module named ['"]([^'"]+)['"]/.exec(s);
  if (missing) {
    const name = missing[1];
    // A dotted name is a SUBMODULE of a package that is itself present. Where the
    // package is stdlib, that is still the extraction race (the package landed,
    // its submodule had not), so judge it by its top-level package.
    return STDLIB_MODULE_NAMES.has(name.split(".")[0]);
  }
  // A package whose __init__.py landed before its siblings does NOT raise
  // ModuleNotFoundError: CPython reports the half-initialized package instead,
  // e.g. `from . import _tzpath` inside a freshly-written zoneinfo/__init__.py
  // gives "ImportError: cannot import name '_tzpath' from partially initialized
  // module 'zoneinfo'". Verified against the shipped interpreter. Match that
  // form too, or the case the pre-spawn probe cannot see stays uncovered.
  const partial = /ImportError: cannot import name .+ from partially initialized module ['"]([^'"]+)['"]/.exec(s);
  if (partial) return STDLIB_MODULE_NAMES.has(partial[1].split(".")[0]);
  return false;
}

/**
 * The portion of a launch-log tail belonging to the CURRENT launch attempt, i.e.
 * the text after the last `SPAWN_MARKER`. Returns "" when the marker is absent,
 * because the boundary has scrolled off and nothing in the tail can be attributed
 * to this attempt.
 *
 * Callers should derive EVERY log-sniffed signal from this, not from the raw tail:
 * the log is append-only across launches, so an older line (a bound port, a
 * traceback) otherwise gets read as describing the current failure.
 *
 * @param {string} text  launch-log tail
 * @returns {string}
 */
function currentAttemptLog(text) {
  if (!text) return "";
  const full = String(text);
  const at = full.lastIndexOf(SPAWN_MARKER);
  return at === -1 ? "" : full.slice(at);
}

/**
 * Whether a spawn failure should be RE-labelled as an unfinished install based
 * on the launch log. Pure, so the precedence rules are tested directly rather
 * than inferred from the dialog.
 *
 * @param {object} o
 * @param {boolean} o.failedToStart  the wait rejected with kind === 'failed'
 * @param {{incompleteBundle?: boolean}|null} [o.failure]  the failure record
 * @param {string} [o.logTail]      launch-log tail
 * @param {boolean} [o.portInUseInLog]  a bound port reported by THIS attempt
 *   (derive it from `currentAttemptLog`, not the raw tail, or a stale port line
 *   suppresses a genuine current stdlib crash)
 * @param {boolean} [o.bundled]     the spawned binary was our bundled backend
 * @returns {boolean}
 */
function shouldReclassifyAsInstalling({
  failedToStart,
  failure = null,
  logTail = "",
  portInUseInLog = false,
  bundled = false,
} = {}) {
  if (!failedToStart || !failure) return false;
  // Already labelled by the pre-spawn refusal — nothing to re-derive.
  if (failure.incompleteBundle) return false;
  // Only OUR bundled interpreter can be half-extracted. A user's own install or
  // a PATH `kirocrew` failing on a stdlib import is a broken environment, not an
  // unfinished download, and telling that user to "wait for the installer" would
  // be actively misleading. Taken from the caller, which knows which binary it
  // chose, rather than re-parsed out of the log.
  if (!bundled) return false;
  // A bound port is the actionable story: it needs force-stop, not a bare retry,
  // and the append-only log may carry a stdlib traceback from an earlier launch.
  if (portInUseInLog) return false;
  return isIncompleteBundleCrash(logTail);
}

// Exported surface = what production actually calls: main.js uses the three
// launcher entry points, build-desktop.sh's gate uses findMissingBundleParts and
// REQUIRED_STDLIB_PARTS. `resolveStdlibDir` / `hasBundledInterpreter` /
// `isIncompleteBundleCrash` stay internal — they are steps of those, and tests
// reach them through the public functions rather than pinning private shapes.
module.exports = {
  REQUIRED_STDLIB_PARTS,
  SPAWN_MARKER,
  currentAttemptLog,
  findMissingBundleParts,
  launchBlockingBundleParts,
  describeIncompleteBundle,
  nextInstallingDialogState,
  INSTALLING_DIALOG_TITLE,
  shouldReclassifyAsInstalling,
};
