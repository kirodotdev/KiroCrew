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
 * Keep runtime bytecode outside signed/read-only POSIX app bundles.
 *
 * The Windows bundle instead ships checked-hash pycs beside its sources. PE
 * Authenticode does not seal resource trees the way macOS code signing does,
 * and using those build-time caches avoids a thousand-file first-launch write
 * burst. A per-machine install may be read-only to the user; Python can still
 * consume the shipped caches without needing to update them.
 */
function gatewayBytecodeEnvironment(platform, cachePath, isPackaged) {
  if (platform === "win32" && isPackaged) {
    // An empty value makes CPython use adjacent __pycache__ files and, unlike
    // omitting the key, overrides a hostile/inherited cache prefix.
    return { PYTHONPYCACHEPREFIX: "" };
  }
  return { PYTHONPYCACHEPREFIX: cachePath };
}

module.exports = {
  buildGatewayEnvironment,
  gatewayBytecodeEnvironment,
  GATEWAY_UTF8_ENV,
};
