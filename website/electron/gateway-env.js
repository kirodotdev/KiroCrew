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
 * Both desktop bundles ship checked-hash pycs beside their sources, so a
 * packaged runtime finds its modules already compiled and has no reason to write
 * one. That is what lets this consume adjacent `__pycache__` instead of
 * redirecting to a per-user cache the first launch would have to populate.
 * macOS compiles the WHOLE tree (`compileall --invalidation-mode checked-hash`
 * in `packaging/build-desktop.sh`); Windows ships the narrower traced startup
 * closure, which is enough there because a later write is harmless.
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

module.exports = {
  buildGatewayEnvironment,
  gatewayBytecodeEnvironment,
  GATEWAY_UTF8_ENV,
};
