"use strict";

// Harden the launcher search path for `shell.openPath` in the main process.
//
// THE PROBLEM (LINUX ONLY). On Linux `shell.openPath` delegates to `xdg-open`,
// which Electron launches by the BARE NAME "xdg-open" (Chromium
// platform_util_linux.cc: `XDGUtil({"xdg-open", path}, ...)` ->
// `base::LaunchProcess`, SYNCHRONOUSLY inside the call). A bare name is resolved
// with `execvp` against the child's PATH, and that child inherits the Electron
// main process's live `process.env.PATH` -- Electron sets only `MM_NOTTTY` on
// the launch, it does not scrub PATH. So a directory that sorts ahead of
// `/usr/bin` on the app's own PATH, if it is writable by the user or an agent,
// decides which `xdg-open` (or `gio`) actually runs. Every main process
// `shell.openPath` call site shares this, so it is a property of the primitive,
// not of any one handler (issue #9094).
//
// WHY ONLY LINUX. This is deliberately a no-op on macOS and Windows, because
// neither resolves a launcher through PATH, so there is nothing there to
// hijack and nothing for a narrowed PATH to protect:
//   - macOS  platform_util_mac.mm  OpenPath -> `[NSWorkspace openURL:]`, which
//     resolves the handler through LaunchServices, not PATH.
//   - Windows platform_util_win.cc OpenPath -> `ShellExecute`/`OpenFileViaShell`
//     on a COM STA worker thread, which resolves the handler through the
//     registry file-type association, not PATH.
// Narrowing PATH on those platforms would run code that protects nothing and,
// worse, would READ as protection to a later maintainer. So this hardens the
// one platform where the threat is real and says so, rather than shipping an
// inert branch on the other two.
//
// A VERSION-FLUID DEPENDENCY, STATED PLAINLY. The mitigation is airtight only
// while Linux `OpenPath` forks SYNCHRONOUSLY inside the `shell.openPath()` call
// (verified against Electron 43, the pinned major -- see ELECTRON_MAJOR_VERIFIED
// below and its guard test). macOS and Windows `OpenPath` ALREADY post the
// native open to a worker (a dispatch queue; a COM STA task runner), which
// proves this timing is an implementation detail that varies by platform and
// can therefore vary by version. If a future Electron moved the Linux launch
// onto a worker the same way, the `execvp` would run AFTER the `finally` restore
// and this mitigation would silently revert to pre-fix behaviour -- the worst
// failure shape, because a fake-`shell` unit test cannot observe real fork
// timing and would stay green. `shell.openPath` takes no launcher or env
// argument, so there is no construction here that resolves `xdg-open` to an
// absolute path up front and removes the timing dependency WITHOUT introducing
// a `child_process` spawn (a `detect-child-process` SAST sink the repo does not
// have, and the exact remedy #9094 rejected). The dependency is therefore
// intrinsic to using `shell.openPath` at all. It is not left implicit: it is
// pinned to a documented Electron major by `ELECTRON_MAJOR_VERIFIED`, whose
// guard test in test/open-path.test.js goes RED when the pin in package.json
// moves past it, forcing a human to re-verify the Linux fork is still
// synchronous before the bump ships. That converts a silent revert into a
// visible CI failure, which is the point.
//
// WHY NOT NARROW THE WHOLE PROCESS PATH. The app legitimately shells out to
// tooling the user expects from their own PATH: the bundled Gateway is spawned
// with `process.env.PATH` as its base (gateway-supervisor.js), and every agent
// shell tool, MCP server, and ACP runtime the Gateway starts inherits that.
// mac-env.js exists precisely to RECOVER the user's PATH there (issue #2367),
// and windows-port.js resolves the Gateway binary through it. Sanitising the
// process's own PATH at startup would fix the launcher by re-breaking #2367.
// What the LAUNCHER needs on PATH is not what the PROCESS needs; only the
// launcher's brief window is narrowed.
//
// OBSERVABLE BEHAVIOUR CHANGE (LINUX). While the narrowed PATH is in effect,
// `xdg-open` and its descendant chain resolve bare command names only against
// the system directories. A user who relies on a launcher or helper installed
// under a personal directory like `~/.local/bin` and resolved BY BARE NAME will
// find that open fall back to the system handler instead. That is the defence
// working as intended -- a bare-name lookup through a writable directory is the
// exact hijack surface -- but it is a change from prior behaviour, noted here so
// it is not mistaken for a regression.

// A narrowed, non-user-writable launcher PATH for Linux. Mirrors auto-update.js
// `managedPath()` and its stated reason: "an agent-writable entry on the user's
// own PATH cannot shadow a command." These are the canonical system binary
// directories where `xdg-open` / `gio` live, so a bare launcher name still
// resolves while a user- or agent-writable directory earlier on the real PATH
// cannot participate.
const LINUX_LAUNCHER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin";

// The Electron MAJOR whose Linux `OpenPath` was verified to fork synchronously
// inside the `shell.openPath()` call (platform_util_linux.cc). Keep this equal
// to the major pinned in package.json's `electron` dependency. The guard test
// in test/open-path.test.js fails when package.json moves past this, which is
// the signal to re-verify the Linux launch is still synchronous and then bump
// this constant in the same change.
const ELECTRON_MAJOR_VERIFIED = 43;

/**
 * The hardened launcher PATH for a platform, or `null` when the platform needs
 * no hardening (macOS, Windows -- their `OpenPath` resolves the handler through
 * LaunchServices / the registry, not PATH). `null` means "leave PATH untouched".
 *
 * @param {string} [platform] - `process.platform` (injectable for tests)
 * @returns {string|null}
 */
function hardenedLauncherPath(platform = process.platform) {
  return platform === "linux" ? LINUX_LAUNCHER_PATH : null;
}

/**
 * Hand a filesystem path to the OS opener, hardening the launcher PATH on Linux.
 *
 * A drop-in wrapper for `shell.openPath(filePath)`: same argument, same
 * `Promise<string>` return (empty string on success, an error message
 * otherwise). On Linux it narrows `process.env.PATH` to {@link LINUX_LAUNCHER_PATH}
 * across the synchronous native launch so the bare launcher name (`xdg-open`)
 * cannot resolve to a user- or agent-writable binary ahead of the system
 * directories, then restores PATH. On macOS and Windows it is a pass-through:
 * those platforms resolve the handler without PATH, so it calls
 * `shell.openPath` unchanged and touches nothing.
 *
 * The caller is still responsible for validating `filePath` before calling
 * (realpath, extension allowlist, regular-file check) -- this wrapper hardens
 * only WHICH launcher runs, not WHAT it is asked to open.
 *
 * @param {{openPath: (p: string) => Promise<string>}} shell - Electron `shell`
 * @param {string} filePath - The path to open (already validated by the caller)
 * @param {object} [deps]
 * @param {string} [deps.platform] - `process.platform` (injectable for tests)
 * @param {NodeJS.ProcessEnv} [deps.env] - the env object to mutate/restore
 *   (defaults to `process.env`; injectable for tests)
 * @returns {Promise<string>} the promise `shell.openPath` returned
 */
function openPathHardened(shell, filePath, { platform = process.platform, env = process.env } = {}) {
  const narrowed = hardenedLauncherPath(platform);
  // macOS / Windows: no PATH-based launcher, so hardening protects nothing.
  // Pass straight through and leave the environment untouched.
  if (narrowed === null) {
    return shell.openPath(filePath);
  }

  // Capture presence, not just value: restoring must be able to DELETE the key
  // again if it was unset, not resurrect it as an empty string (an empty PATH
  // entry is the cwd, which is exactly the kind of shadowing this guards).
  const had = Object.prototype.hasOwnProperty.call(env, "PATH");
  const previous = env.PATH;
  env.PATH = narrowed;
  try {
    // Synchronous on Linux: the native fork+execvp that reads PATH happens
    // inside this call, before the returned promise. Do NOT await here --
    // awaiting would yield the main thread with PATH still narrowed, which is
    // the one thing that could let another spawn observe it. (See the
    // version-fluid dependency note at the top of the file.)
    return shell.openPath(filePath);
  } finally {
    if (had) {
      env.PATH = previous;
    } else {
      delete env.PATH;
    }
  }
}

module.exports = {
  LINUX_LAUNCHER_PATH,
  ELECTRON_MAJOR_VERIFIED,
  hardenedLauncherPath,
  openPathHardened,
};
