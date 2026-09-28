/**
 * The marker-driven update lane: an EXTERNALLY-MANAGED marker that carries an
 * `updateCommand` (and optionally a `checkCommand`) owns this install's
 * lifecycle, so updates are discovered and applied by running those commands
 * instead of arming electron-updater or contacting the feed.
 *
 * The marker already passed readExternallyManaged's provenance test (in
 * auto-update.js) before this lane is created. This module owns what that
 * trust is spent on: the hardened command executor, the check / download /
 * install state, the quit-time auto-apply and the background schedule.
 *
 * @returns {{check: Function, download: Function, install: Function, getInfo: Function}}
 */
function createManagedLane({
  managed,
  app,
  emit,
  getInfo,
  getAutoDownloadPreference,
  stopGateway,
  onInstallDispatched,
  onInstallFailed,
  classifyError,
  managedPath,
  launchCheckDelayMs,
  checkIntervalMs,
  log,
}) {
  // MANAGED AUTO-UPDATE (marker-driven): the marker carries the very
  // commands that own this install's lifecycle, so instead of arming
  // electron-updater or contacting the feed (which would fight the external
  // manager), we discover and apply updates by SHELLING the marker's own
  // commands.
  //
  // TRUST / HARDENING: reaching here means the marker's metadata already
  // passed the provenance test in readExternallyManaged — either it is BAKED
  // into the application's own code (the same archive as this module, so no
  // write primitive reaches it that does not already reach main.js), or it is
  // a LOOSE marker that neither this euid owns nor group/other can write, so
  // it is a genuine packager artifact rather than a file a prompt-injected
  // agent shell could have planted. That test is
  // what makes the commands trustworthy at all; the hardening below is about
  // the ENVIRONMENT they run in, not about the command string (see
  // runManagedCommand) — a narrowed system-only PATH so a planted shim on the
  // user's PATH cannot shadow a command, a CONSTRUCTED environment so nothing
  // the shell reads as code is inherited at all, cwd="/" (never the app or an
  // inherited dir), a
  // timeout, and bounded retained output. The
  // command still runs through a shell, so the writer MUST name absolute
  // binaries (a bare name will not resolve under the narrowed PATH); we NEVER
  // interpolate untrusted input. Platform-agnostic: the same path serves
  // macOS/Windows/Linux.
  log.info(`[update] externally managed${managed.managedBy ? ` by ${managed.managedBy}` : ""} — managed auto-update (self-contained commands)`);

  let foundVersion = null; // last version discovered by the checkCommand, awaiting apply
  let managedQuitArmed = false; // is a before-quit auto-apply handler installed?
  let managedInstalling = false; // an apply is in progress — pause the poll

  // Mirror the feed lane's emitError renderer contract: a failure WITH ITS
  // PHASE so the card can distinguish check from install failures.
  const emitManagedError = (phase, err) => {
    const { code, detail, httpStatus } = classifyError(err);
    log.error(`[update] managed ${phase} failed (${code})`, err);
    emit("error", {
      phase,
      code,
      message: detail,
      ...(httpStatus === undefined ? {} : { httpStatus }),
    });
  };

  // Bound retained output so a chatty command cannot exhaust memory (we keep
  // DRAINING both streams either way), and cap how long an apply / check runs.
  const MANAGED_OUTPUT_CAP = 64 * 1024;
  const MANAGED_APPLY_TIMEOUT_MS = 30 * 60 * 1000; // 30 min ceiling for an apply
  const MANAGED_CHECK_TIMEOUT_MS = 45 * 1000; // a check must not hang the UI
  const MANAGED_VERSION_CAP = 128; // a version string is short; cap like the sibling
  // The interpreter the marker command runs under. On Windows `shell: true`
  // would read `process.env.ComSpec` -- user-level, and therefore settable by
  // the same agent this whole construction defends against -- so the system
  // cmd.exe is named by PATH instead, anchored on the SystemRoot that
  // managedPath() already trusts. On POSIX Node resolves /bin/sh by path, so
  // `true` is already pinned.
  const managedShell = () =>
    process.platform === "win32"
      ? `${process.env.SystemRoot || "C:\\Windows"}\\System32\\cmd.exe`
      : true;
  // The child's environment is CONSTRUCTED, not filtered.
  //
  // `shell: true` means a shell interprets the command, and a shell reads its
  // environment as code: the loader family (LD_*/DYLD_*), the interpreter
  // family (PYTHON*, NODE_OPTIONS), the startup files (BASH_ENV, ENV), the
  // tracing pair (SHELLOPTS plus a command-substituting PS4), word splitting
  // (IFS), and exported shell FUNCTIONS (BASH_FUNC_* — a function shadows a
  // command name outright, beating managedPath() rather than evading it).
  // That namespace is open-ended and differs by shell and by version, so no
  // denylist over it is provably complete; successive review rounds just find
  // the next name.
  //
  // Naming what the child DOES get inverts that: anything absent from this
  // list is gone by construction, so every present and future injection
  // variable is already handled and there is no enumeration to keep current.
  // The list carries what a packager's own updater plausibly needs — locale,
  // temp dir, proxy — and nothing a shell or an interpreter treats as code. A
  // packager needing more sets it inside its own command, which is the one
  // place that requirement is visible to whoever wrote it.
  //
  // HOME is deliberately NOT here. It is not shell-interpreted, but it is a
  // path an interpreter reads code from: Python derives its user-site
  // directory from HOME, so a planted ~/.local/lib/pythonX/site-packages/
  // sitecustomize.py executes on every `python` start. Passing HOME would
  // re-open the startup-injection class for any marker command that happens to
  // be a Python program, which is the class this whole construction closes.
  const MANAGED_ENV_PASSTHROUGH = [
    "USER", "LOGNAME", "TZ", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
  ];
  // cmd.exe cannot start without these, so the win32 lane mirrors
  // managedPath()'s win32 branch rather than handing it a shell it cannot run.
  // COMSPEC is deliberately NOT inherited: the shell is pinned by
  // managedShell(). (cmd.exe sets COMSPEC to its own path once it starts, so
  // whatever the child sees IS the pinned shell -- what matters is that the
  // app's inherited value never reaches it.)
  const MANAGED_ENV_PASSTHROUGH_WIN32 = [
    "SystemRoot", "SystemDrive", "windir",
    "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
  ];
  const managedEnv = () => {
    // PYTHONNOUSERSITE is SET, not merely withheld: HOME is excluded above
    // because Python derives its user-site directory from it, but on Windows
    // the same directory derives from APPDATA -- which cmd.exe-era tooling
    // needs and so IS passed through. Rather than reason per platform about
    // which variable leads to `site-packages`, tell the interpreter directly
    // that no user site exists. Harmless to every non-Python command.
    // KIROCREW_MANAGED_ARGV0 is DERIVED, not inherited: process.execPath is
    // the running executable's absolute path from the kernel command line,
    // never read from process.env. It is the one fact a relaunch-verifying
    // wrapper needs (which binary launched the app it is about to replace)
    // and the only way to get it, since no app environment variable reaches
    // the command by design.
    const e = {
      PATH: managedPath(),
      PYTHONNOUSERSITE: "1",
      KIROCREW_MANAGED_ARGV0: process.execPath,
    };
    const keys = process.platform === "win32"
      ? [...MANAGED_ENV_PASSTHROUGH, ...MANAGED_ENV_PASSTHROUGH_WIN32]
      : MANAGED_ENV_PASSTHROUGH;
    for (const k of keys) {
      if (process.env[k] !== undefined) e[k] = process.env[k];
    }
    return e;
  };

  // Run a marker command through the platform shell, resolving to
  // {code, out} (combined stdout+stderr, capped). Never rejects: spawn errors
  // and timeouts resolve with a non-zero code so callers treat them uniformly.
  // Hardened like the Python CommandProvider: narrowed PATH, cwd="/", a
  // timeout, and bounded retained output.
  const runManagedCommand = (command, { timeout } = {}) => new Promise((resolve) => {
    const cp = require("child_process");
    let out = "";        // combined stdout+stderr, for logging an apply
    let outStdout = "";  // stdout ONLY, for deriving the check's version
    let settled = false;
    // `failed` marks that the command could not be RUN to completion (spawn
    // error or timeout kill), as distinct from running and exiting non-zero.
    // The check path treats these differently: a run that exits non-zero is
    // "no update", but a command that could not run at all is an error.
    const done = (code, failed) => {
      if (!settled) {
        settled = true;
        resolve({ code: typeof code === "number" ? code : 1, out, stdout: outStdout, failed: !!failed });
      }
    };
    // Keep consuming BOTH streams (so the pipe never blocks the child) but
    // stop RETAINING once capped. stdout is captured separately because the
    // version is derived from stdout ONLY — a warning printed to stderr must
    // never be mistaken for the version.
    const capped = (s) => (s.length > MANAGED_OUTPUT_CAP ? s.slice(0, MANAGED_OUTPUT_CAP) : s);
    const onStdout = (d) => {
      const s = d.toString();
      if (out.length < MANAGED_OUTPUT_CAP) out = capped(out + s);
      if (outStdout.length < MANAGED_OUTPUT_CAP) outStdout = capped(outStdout + s);
    };
    const onStderr = (d) => {
      if (out.length < MANAGED_OUTPUT_CAP) out = capped(out + d.toString());
    };
    let child;
    try {
      // `command` is NOT user input: it is operator-controlled text from the
      // EXTERNALLY-MANAGED marker, and execution is hardened (narrowed system
      // PATH, cwd="/", bounded output, timeout). See the trust note above.
      //
      // The SHELL is pinned, not discovered. `shell: true` on Windows resolves
      // the interpreter from `process.env.ComSpec`, a user-level variable the
      // same agent that managedEnv() defends against can set -- so the trusted
      // command would run under an attacker-chosen shell before its first
      // token was parsed. managedShell() names the system cmd.exe by path
      // (the same SystemRoot anchor managedPath() already relies on) and the
      // app's COMSPEC is not inherited -- cmd.exe sets its own to the pinned
      // path, so nothing the child re-reads names another shell. POSIX keeps
      // `shell: true`: Node resolves /bin/sh by path there, not from the env.
      // nosemgrep: javascript.lang.security.detect-child-process.detect-child-process
      child = cp.spawn(command, { // nosemgrep: javascript.lang.security.detect-child-process.detect-child-process
        shell: managedShell(),
        cwd: "/",
        env: managedEnv(),
        ...(timeout ? { timeout } : {}),
      });
    } catch (err) {
      log.error("[update] managed command spawn threw", err);
      return done(1, true);
    }
    if (child.stdout) child.stdout.on("data", onStdout);
    if (child.stderr) child.stderr.on("data", onStderr);
    child.on("error", (err) => { log.error("[update] managed command error", err); done(1, true); });
    // A timeout kill closes with a null exit code and a signal; treat that as
    // "could not run", not as a non-zero exit.
    child.on("close", (code, signal) => done(code, code === null && signal != null));
  });

  // The command run to APPLY an update, on quit and on explicit install.
  // Bounded by a ceiling timeout so a wedged package manager cannot hang quit.
  const runUpdateCommand = () => runManagedCommand(managed.updateCommand, { timeout: MANAGED_APPLY_TIMEOUT_MS });

  // Fresh-read the auto-download preference; a throwing reader fails toward
  // NOT auto-installing (same direction as the feed path's deferred handler).
  const autoDownloadOn = () => {
    try { return !!getAutoDownloadPreference(); } catch (err) {
      log.error("[update] getAutoDownloadPreference threw — treating as off", err);
      return false;
    }
  };

  // Auto-on-restart: apply the discovered update on the next natural quit.
  // Mirrors deferredInstallOnQuit — pref is re-read FRESH at quit time so a
  // toggle-off between discovery and quit is honored.
  const managedInstallOnQuit = (event) => {
    // Nothing pending (a later check cleared it, or it was already applied):
    // let the quit proceed normally — never relaunch into a withdrawn update.
    if (!foundVersion) {
      log.info("[update] managed quit handler fired with no pending update — not applying");
      return;
    }
    let stillOn = false;
    try { stillOn = !!getAutoDownloadPreference(); } catch (err) {
      log.error("[update] getAutoDownloadPreference threw on quit — not installing", err);
    }
    if (!stillOn) {
      log.info("[update] managed auto-download off at quit — not applying on quit");
      return;
    }
    event.preventDefault();
    (async () => {
      managedInstalling = true;
      emit("installing", { version: foundVersion });
      try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
      try { if (stopGateway) await stopGateway(); } catch (err) {
        log.error("[update] managed stop on quit errored", err);
      }
      log.info("[update] managed deferred install on quit — running update command");
      const { code } = await runUpdateCommand();
      if (code === 0) {
        app.relaunch();
      } else {
        // The apply failed; the user asked to quit, so honor that and exit
        // WITHOUT relaunching into a version that did not install.
        log.error(`[update] managed deferred install failed (exit ${code}) — quitting without relaunch`);
        try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
      }
      app.exit(0);
    })();
  };

  // Undo a quit-time auto-apply armed by an earlier check and forget the
  // discovered version. Called when a later check finds nothing pending, so a
  // normal quit does not relaunch into an update the external manager already
  // applied or withdrew — the feed path clears its deferred state for the
  // same reason.
  const disarmManagedQuit = () => {
    foundVersion = null;
    if (managedQuitArmed) {
      app.removeListener("before-quit", managedInstallOnQuit);
      managedQuitArmed = false;
    }
  };

  async function managedCheck() {
    emit("checking");
    if (!managed.checkCommand) {
      // The marker says how to APPLY an update but gives no way to DISCOVER
      // one. This is a check error, NOT a green "up to date": a silent
      // "latest" would hide every future update for this install forever.
      log.info("[update] managed: no checkCommand — cannot check for updates");
      emitManagedError("check", new Error("this managed install has no checkCommand"));
      return;
    }
    const { code, stdout, failed } = await runManagedCommand(managed.checkCommand, {
      timeout: MANAGED_CHECK_TIMEOUT_MS,
    });
    if (failed) {
      // Could not RUN the command (spawn error or timeout) — an error, not
      // "up to date". Mirrors the sibling CommandProvider, which returns an
      // error verdict for a check it could not execute.
      log.error("[update] managed check could not run");
      emitManagedError("check", new Error("managed check command failed to run"));
      return;
    }
    if (code !== 0) {
      // Ran and exited non-zero: no update available (sibling contract). Undo
      // any quit-time auto-apply armed by an earlier check that DID find one,
      // so a normal quit does not relaunch into a withdrawn/applied update.
      log.info(`[update] managed check: up to date (code=${code})`);
      disarmManagedQuit();
      emit("not-available");
      return;
    }
    // Sibling contract: exit 0 and stdout IS the version (trimmed, capped).
    // Derived from stdout ONLY so a stderr warning is never read as a version.
    const version = stdout.trim().slice(0, MANAGED_VERSION_CAP);
    if (!version) {
      // Exit 0 that prints NO version is a broken command, not an available
      // update: treating it as available would relaunch to the SAME version
      // forever. Fail the check rather than report "latest".
      log.error("[update] managed check: exit 0 but printed no version");
      emitManagedError("check", new Error("managed check command produced no version"));
      return;
    }
    foundVersion = version;
    log.info(`[update] managed check: update available -> ${version}`);
    emit("found", { version });
    // Auto-on-restart: if the user allows auto-download, arm a one-shot
    // before-quit handler that applies on the natural quit.
    if (autoDownloadOn() && !managedQuitArmed) {
      managedQuitArmed = true;
      app.once("before-quit", managedInstallOnQuit);
    }
  }

  async function managedDownload() {
    // Managed download+apply is ONE step (the updateCommand). "download" just
    // lights the UI Install action; it never applies. Discover first if the
    // UI raced the check.
    if (!foundVersion) {
      await managedCheck();
    }
    if (foundVersion) {
      emit("downloaded", { version: foundVersion });
    }
  }

  async function managedInstall() {
    managedInstalling = true;
    emit("installing", { version: foundVersion });
    try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
    try { if (stopGateway) await stopGateway(); } catch (err) {
      log.error("[update] managed stop before install errored", err);
    }
    const { code } = await runUpdateCommand();
    if (code === 0) {
      log.info("[update] managed install succeeded — relaunching");
      app.relaunch();
      app.exit(0);
      return;
    }
    log.error(`[update] managed install failed (exit ${code})`);
    try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
    emitManagedError("install", new Error(`managed update command exited ${code}`));
  }

  // Auto-check on launch and on the same interval as the feed path, so a
  // managed install DISCOVERS updates without the user clicking Check
  // (auto-update is on by default). Background checks only discover — an
  // apply still requires the auto-download preference or an explicit install.
  // The poll skips windows where an apply is already in flight, and both
  // timers are unref'd so they never hold the process open (Electron quit,
  // tests).
  const managedLaunchTimer = setTimeout(() => {
    managedCheck().catch((err) => log.error("[update] managed launch check threw", err));
  }, launchCheckDelayMs);
  const managedPollTimer = setInterval(() => {
    if (!managedInstalling) {
      managedCheck().catch((err) => log.error("[update] managed poll check threw", err));
    }
  }, checkIntervalMs);
  if (typeof managedLaunchTimer.unref === "function") managedLaunchTimer.unref();
  if (typeof managedPollTimer.unref === "function") managedPollTimer.unref();

  return {
    check: () => managedCheck(),
    download: () => managedDownload(),
    install: () => managedInstall(),
    getInfo,
  };
}

module.exports = { createManagedLane };
