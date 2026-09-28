"use strict";
//
// Early-boot guard for the desktop shell's main process.
//
// Everything `main.js` does between its `require`s and `app.whenReady()` runs
// synchronously at module load: the settings store opens, the port resolves,
// the single-instance lock is taken, the crash collector and native logging
// arm, and the gateway/window/IPC factories construct. No window, tray, or
// crash reporter exists for most of that span, so an exception there has no
// surface to land on — the process dies with nothing on screen and, before the
// log sink is armed, nothing on disk. The user sees an app that "just does not
// open".
//
// This module gives that span ONE catch. Installed as the first statement
// after the requires, it listens for `uncaughtException` and
// `unhandledRejection` and, on either, writes the stack through the launch log,
// shows a native error box (`dialog.showErrorBox` is synchronous and usable
// before ready, unlike `showMessageBox`), and exits with status 1. It is
// released at the top of the ready handler, where the shell's post-ready safety
// net — which deliberately keeps the process ALIVE so renderer and gateway
// recovery can run — takes over.
//
// One mechanism rather than per-block `try`/`catch`: a wrapper around each boot
// statement has to be maintained statement by statement and turns every
// `const` it encloses into a block-scoped binding, while a process-level
// listener covers the whole span with no change to the code it guards.
//
// Pure logic + injected dependencies, so a unit test drives it with a fake
// `app`, `dialog`, `glog` and event emitter (same pattern as disable-gpu.js and
// native-logging.js).
//

/** Message plus stack when available; the bare value otherwise. */
function describeFailure(error) {
  return error && error.stack ? String(error.stack) : String(error);
}

/**
 * Report a pre-ready failure and exit. Never throws.
 *
 * Order matters: the log line goes first because it is the one channel that
 * needs nothing from Chromium; the dialog is best-effort and wrapped so a
 * display that cannot show it does not stop the exit below.
 *
 * @param {object} deps
 * @param {{ exit(code: number): void }} deps.app
 * @param {{ showErrorBox(title: string, content: string): void }} deps.dialog
 * @param {(line: string) => void} deps.glog
 * @param {() => string} [deps.logPath]
 * @param {string} stage   Which listener fired, for the log line.
 * @param {unknown} error  The thrown value or rejection reason.
 */
function failEarlyBoot({ app, dialog, glog, logPath }, stage, error) {
  const detail = describeFailure(error);
  try {
    glog("early boot failed (" + stage + "): " + detail);
  } catch {
    // The logger must never mask the failure it is reporting.
  }
  let where = "";
  try {
    where = typeof logPath === "function" ? String(logPath() || "") : "";
  } catch {
    // The dialog is still useful without the path.
  }
  try {
    dialog.showErrorBox(
      "Kiro Crew failed to start",
      "An error occurred before the app could open a window:\n\n" + detail
        + (where ? "\n\nA log was written to: " + where : ""),
    );
  } catch {
    // A dialog that cannot be shown must not block the exit; the log entry
    // above already captured the failure.
  }
  app.exit(1);
}

/**
 * Install the guard on *emitter* (the process by default).
 *
 * Only the first failure is reported in full: `app.exit` is asynchronous in
 * Electron, so a second exception racing the shutdown would otherwise stack a
 * second error box on the first. Later failures are logged and re-request the
 * exit.
 *
 * @param {object} deps
 * @param {{ exit(code: number): void }} deps.app
 * @param {{ showErrorBox(title: string, content: string): void }} deps.dialog
 * @param {(line: string) => void} deps.glog
 * @param {() => string} [deps.logPath]
 * @param {NodeJS.EventEmitter} [deps.emitter]  Defaults to `process`.
 * @returns {() => void} Removes both listeners.
 */
function installEarlyBootGuard({ app, dialog, glog, logPath, emitter = process }) {
  const deps = { app, dialog, glog, logPath };
  let reported = false;

  const report = (stage) => (error) => {
    if (reported) {
      try {
        glog("early boot failed again (" + stage + "): " + describeFailure(error));
      } catch {
        // Same rule as failEarlyBoot: logging never masks the failure.
      }
      app.exit(1);
      return;
    }
    reported = true;
    failEarlyBoot(deps, stage, error);
  };

  const onException = report("uncaughtException");
  const onRejection = report("unhandledRejection");
  emitter.on("uncaughtException", onException);
  emitter.on("unhandledRejection", onRejection);

  return function releaseEarlyBootGuard() {
    emitter.removeListener("uncaughtException", onException);
    emitter.removeListener("unhandledRejection", onRejection);
  };
}

module.exports = {
  installEarlyBootGuard,
  failEarlyBoot,
  describeFailure,
};
