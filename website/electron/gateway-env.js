"use strict";

// Environment invariants for every gateway the desktop shell owns.
//
// CPython chooses the encoding for redirected stdout/stderr before any Kiro
// Crew Python runs.  Reconfiguring sys.stdout later is therefore only a
// best-effort repair.  Windows defaults redirected files/pipes to its ANSI code
// page, while every platform permits PYTHONIOENCODING to override an otherwise
// UTF-8 locale.  The gateway prints emoji during boot, so a hostile inherited
// ascii/cp1252 value turns an ordinary in-app restart (Tailnet, update,
// stale-assets, or the explicit restart API) into a fatal UnicodeEncodeError.
//
// Pinning the interpreter at this parent boundary covers both the first launch
// and every Electron liveness respawn on Windows, macOS, and Linux.  The values
// are inherited by the gateway's exec successor and Python children too,
// including MCP/session processes.  platform_compat.py republishes the same
// invariant for gateways started outside Electron.
const GATEWAY_UTF8_ENV = Object.freeze({
  PYTHONUTF8: "1",
  PYTHONIOENCODING: "utf-8:backslashreplace",
});

// Where packaging/build-desktop.sh stages the pinned kiro-cli, relative to the
// app's resources directory. One spelling shared with the backend's reader
// (kiro_cli.known_kiro_cli_dirs, via the env var below) and the docs.
const BUNDLED_KIRO_CLI_SUBDIR = ["backend-dist", "kiro-cli"];

/**
 * Build a gateway child environment without mutating Electron's process.env.
 *
 * The desktop app owns this Python process tree, so the UTF-8 values
 * deliberately override hostile inherited values such as PYTHONUTF8=0 or
 * PYTHONIOENCODING=cp1252 on every supported desktop platform.
 *
 * @param {NodeJS.ProcessEnv} baseEnv
 * @returns {NodeJS.ProcessEnv}
 */
function buildGatewayEnvironment(baseEnv) {
  return {
    ...baseEnv,
    ...GATEWAY_UTF8_ENV,
  };
}

/**
 * Decide where a packaged gateway's bytecode may go — and on macOS, that it may
 * not be written at all.
 *
 * Both desktop bundles ship hash-based pycs beside their sources, so a
 * packaged runtime finds its modules already compiled and has no reason to write
 * one. That is what lets this consume adjacent `__pycache__` instead of
 * redirecting to a per-user cache the first launch would have to populate.
 * macOS compiles the WHOLE tree as CHECKED-hash
 * (`compileall --invalidation-mode checked-hash` in
 * `packaging/build-desktop.sh`); Windows ships the narrower traced startup
 * closure as UNCHECKED-hash, which is enough there because a later write is
 * harmless. Both ignore mtime, which is the property that survives extraction
 * restamping; they differ only in whether the loader re-reads and re-hashes the
 * `.py` on every import, which on Windows measured a median 12.5 s per cold boot
 * across the closure's 1639 modules.
 *
 * macOS additionally FORBIDS the write. `codesign` seals every file under a
 * `.app`'s `Contents/`, so bytecode written there after signing invalidates the
 * signature and Gatekeeper refuses the app as "damaged" — reported on managed
 * Macs, whose policy re-evaluates instead of reusing a cached accept verdict.
 * Redirecting the cache elsewhere would also prevent that, but at a real price:
 * a set prefix makes CPython ignore the adjacent caches entirely, so the shipped
 * caches become dead weight and every version's first launch recompiles them
 * (measured 3.95s cold vs 0.84s warm). Forbidding the write keeps the shipped
 * caches readable — `PYTHONDONTWRITEBYTECODE` disables writing only — so a
 * module inside the closure loads from disk and one outside it compiles in
 * memory and leaves no file behind.
 *
 * Windows is not locked down, deliberately: Authenticode does not seal resource
 * trees, so a write there is harmless, and letting modules outside the traced
 * closure cache themselves helps later launches. A per-machine install may be
 * read-only to the user; Python still consumes the shipped caches either way.
 *
 * Unpackaged and Linux keep the redirect: a dev tree has no shipped closure, and
 * a Linux package may be read-only with no signature to protect.
 */
function gatewayBytecodeEnvironment(platform, cachePath, isPackaged) {
  if (isPackaged && (platform === "win32" || platform === "darwin")) {
    // An empty value makes CPython use adjacent __pycache__ files and, unlike
    // omitting the key, overrides a hostile/inherited cache prefix.
    const env = { PYTHONPYCACHEPREFIX: "" };
    if (platform === "darwin") {
      // Not merely a default: an inherited empty/unset value means "write next
      // to the source", which inside a signed bundle is the corruption itself.
      env.PYTHONDONTWRITEBYTECODE = "1";
    }
    return env;
  }
  return { PYTHONPYCACHEPREFIX: cachePath };
}

/**
 * Point the gateway at the kiro-cli copy staged into the app's own resources.
 *
 * `packaging/build-desktop.sh` (BUNDLE_KIRO_CLI) stages a pinned, sha256-verified
 * entry under `<resources>/backend-dist/kiro-cli/`: `kiro-cli-chat` on POSIX,
 * or the one `kiro-cli.exe` extracted from the Windows MSI. The backend reads
 * the directory from `KIROCREW_BUNDLED_KIRO_DIR` and ranks it above every
 * system install but below the `KIROCREW_KIRO_BIN` operator override
 * (`kiro_cli.known_kiro_cli_dirs`), so the app runs the exact agent runtime it
 * was built against while an operator can still force a different binary.
 *
 * Both variables are set ONLY when the directory actually shipped. A build without
 * the payload (`BUNDLE_KIRO_CLI=0` or a source checkout with no resources)
 * spreads nothing, so discovery falls through to the user's own
 * install exactly as an unbundled build does. A directory rather than a binary
 * path because this side only stats what it staged; the entry binary's name is
 * owned by the backend's `kiro_cli.bundled_kiro_cli_entry` helper.
 *
 * `KIRO_NO_AUTO_UPDATE=1` rides along, set here ONCE for the whole gateway
 * process tree. Every child that runs the bundled copy -- ACP sessions, the
 * `/api/models` listing, `whoami`, the usage scrape, `kirocrew doctor`, the
 * readiness probes -- inherits it by construction, instead of each spawn site
 * remembering to merge it. It is kiro-cli's documented switch for its startup
 * update check (upstream's auto-update guide: "Set to any value to disable
 * auto-update entirely"). Today that check is compiled for Windows (the
 * upstream chat-cli crate's `cli/mod.rs` at v2.24.0 gates the whole block on
 * `target_os = "windows"`; macOS and Linux never start it), so the switch stops
 * the bundled Windows executable from rewriting itself. It is also a
 * guard against upstream's stated "FUTURE: re-enable for all platforms", which
 * would otherwise write into the signed, sealed app bundle. Accepted side
 * effect: a system kiro-cli an operator forces through `KIROCREW_KIRO_BIN` also
 * skips the check WHILE RUNNING AS A CHILD OF THE APP; its own terminal use is
 * unaffected, and the user's `app.disableAutoupdates` setting is never written.
 *
 * @param {Pick<typeof import("fs"), "statSync">} fs
 * @param {Pick<typeof import("path"), "join">} path
 * @param {string | undefined} resourcesPath  `process.resourcesPath`, absent in a
 *   source checkout.
 * @returns {NodeJS.ProcessEnv}
 */
function bundledKiroCliEnvironment(fs, path, resourcesPath) {
  if (!resourcesPath) return {};
  const bundledDir = path.join(resourcesPath, ...BUNDLED_KIRO_CLI_SUBDIR);
  try {
    return fs.statSync(bundledDir).isDirectory()
      ? { KIROCREW_BUNDLED_KIRO_DIR: bundledDir, KIRO_NO_AUTO_UPDATE: "1" }
      : {};
  } catch {
    return {};
  }
}

module.exports = {
  buildGatewayEnvironment,
  bundledKiroCliEnvironment,
  gatewayBytecodeEnvironment,
  GATEWAY_UTF8_ENV,
};
