/**
 * The hardened executor for an EXTERNALLY-MANAGED marker's own commands.
 *
 * Shared by the managed lane (check, quit-time apply) and the start-time apply
 * of an armed update, which runs in Electron main before any window opens. The
 * marker already passed readExternallyManaged's provenance test before this is
 * built; what this owns is the ENVIRONMENT the commands run in.
 */
function createManagedExec({ managed, managedPath, log }) {
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
  // (IFS), and exported shell FUNCTIONS (BASH_FUNC_* - a function shadows a
  // command name outright, beating managedPath() rather than evading it).
  // That namespace is open-ended and differs by shell and by version, so no
  // denylist over it is provably complete; successive review rounds just find
  // the next name.
  //
  // Naming what the child DOES get inverts that: anything absent from this
  // list is gone by construction, so every present and future injection
  // variable is already handled and there is no enumeration to keep current.
  // The list carries what a packager's own updater plausibly needs - locale,
  // temp dir, proxy - and nothing a shell or an interpreter treats as code. A
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
  // {code, out, stdout, failed, timedOut, signal} (`out` is combined
  // stdout+stderr, capped). Never rejects: spawn errors and timeouts resolve
  // with a non-zero code so callers treat them uniformly.
  // Hardened like the Python CommandProvider: narrowed PATH, cwd="/", a
  // timeout, and bounded retained output.
  //
  // With a `label`, the run is logged: one line after spawn naming the pid, and
  // one when it ends naming the exit code, signal or timeout, the run time and
  // the size of `out`. The output TEXT is never logged: an update tool can
  // print credential URLs or tokens, and no redactor here covers them. Check
  // runs pass no label, so the periodic check poll adds no lines.
  const runManagedCommand = (command, { timeout, label } = {}) => new Promise((resolve) => {
    const cp = require("child_process");
    let out = "";        // combined stdout+stderr; its size is logged for a labelled run
    let outStdout = "";  // stdout ONLY, for deriving the check's version
    let settled = false;
    let timedOut = false;
    let timer = null;
    const startedAt = Date.now();
    // `failed` marks that the command could not be RUN to completion (spawn
    // error, timeout kill or another signal), as distinct from running and
    // exiting non-zero. The check path treats these differently: a run that
    // exits non-zero is "no update", but a command that could not run at all
    // is an error.
    const done = (code, failed, signal = null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      const result = {
        code: typeof code === "number" ? code : 1,
        out,
        stdout: outStdout,
        failed: !!failed,
        timedOut,
        signal: signal || null,
      };
      if (label) {
        let outcome = `exit ${result.code}`;
        if (timedOut) outcome = `timed out after ${timeout} ms`;
        else if (signal) outcome = `killed by ${signal}`;
        else if (failed) outcome = "could not run";
        const line = `[update] managed ${label} ended: ${outcome}, ran ${Date.now() - startedAt} ms, ${Buffer.byteLength(out)} bytes of output`;
        if (result.code === 0 && !result.failed) log.info(line);
        else log.error(line);
      }
      resolve(result);
    };
    // Keep consuming BOTH streams (so the pipe never blocks the child) but
    // stop RETAINING once capped. stdout is captured separately because the
    // version is derived from stdout ONLY - a warning printed to stderr must
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
      });
    } catch (err) {
      log.error("[update] managed command spawn threw", err);
      return done(1, true);
    }
    if (label) log.info(`[update] managed ${label} started (pid ${child.pid ?? "unknown"})`);
    // The timeout is armed here rather than through spawn's own `timeout`
    // option so a kill it causes is KNOWN to be a timeout: spawn's option
    // kills with the same SIGTERM any other sender could, and the close
    // event cannot tell them apart. Same signal, same ceiling.
    // The timer is cancelled when the process EXITS, as spawn's option does,
    // not when its pipes close: a background helper the command left behind
    // can hold the pipes open past the deadline after a clean exit, and that
    // run finished in time.
    if (timeout) {
      timer = setTimeout(() => { timedOut = true; child.kill(); }, timeout);
      child.on("exit", () => clearTimeout(timer));
    }
    if (child.stdout) child.stdout.on("data", onStdout);
    if (child.stderr) child.stderr.on("data", onStderr);
    child.on("error", (err) => { log.error("[update] managed command error", err); done(1, true); });
    // A kill closes with a null exit code and a signal; treat that as
    // "could not run", not as a non-zero exit. A timeout kill is "could not
    // run" whatever exit code the platform reports for it.
    child.on("close", (code, signal) => done(code, timedOut || (code === null && signal != null), signal));
  });

  // The command run to APPLY an update, on quit and on explicit install.
  // Bounded by a ceiling timeout so a wedged package manager cannot hang quit.
  const runUpdateCommand = () => runManagedCommand(managed.updateCommand, {
    timeout: MANAGED_APPLY_TIMEOUT_MS,
    label: "update command",
  });

  // An update command that exits 0 but leaves the install as it was would
  // relaunch into the same check and the same apply on every quit. So the
  // check runs again after an apply: true only when it still offers the version
  // the apply was for. A re-check that cannot run, finds nothing, or offers
  // another version is not that loop, and the relaunch goes ahead.
  const applyChangedNothing = async (appliedVersion) => {
    if (!appliedVersion || !managed.checkCommand) return false;
    const { code, stdout, failed } = await runManagedCommand(managed.checkCommand, {
      timeout: MANAGED_CHECK_TIMEOUT_MS,
    });
    if (failed || code !== 0) return false;
    return stdout.trim().slice(0, MANAGED_VERSION_CAP) === appliedVersion;
  };
  const unchangedMessage = (version) =>
    `the update command finished, but the check still offers ${version}: the install did not change`;

  return {
    runManagedCommand,
    runUpdateCommand,
    applyChangedNothing,
    unchangedMessage,
    MANAGED_APPLY_TIMEOUT_MS,
    MANAGED_CHECK_TIMEOUT_MS,
    MANAGED_VERSION_CAP,
  };
}

module.exports = { createManagedExec };
