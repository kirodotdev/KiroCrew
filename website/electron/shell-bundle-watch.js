"use strict";

/**
 * Notice when the bundle this shell is running from has been deleted under it.
 *
 * A package manager that installs each version side by side and prunes the
 * old ones can remove the .app this process was launched from while it is
 * still running. macOS lets the process keep its mapped pages, so nothing
 * fails at once, but the next file the main process reads lazily from that
 * bundle (a module compiled on first use, an asset) is gone, and the shell
 * aborts inside V8 with no warning and no saved window state. The backend has
 * its own watchdog and the supervisor respawns it; nothing restarts the shell.
 *
 * This watch checks the two paths that identify the running image -- the
 * executable (process.execPath) and the packed app code
 * (<resourcesPath>/app.asar) -- on a slow interval, and on demand when the
 * supervisor has just recovered a backend from a stale bundle. A miss is
 * confirmed by a second probe a few seconds later, so a bundle that is being
 * swapped in place (briefly absent, then back at the same path) never
 * prompts. Once a miss is confirmed `onPruned` runs exactly once and the
 * watch stops: the remedy is a restart, and asking twice helps nobody.
 *
 * It cannot relaunch the shell itself. Electron re-executes process.execPath,
 * which is the path that is gone, and where the replacement bundle lives is
 * known only to the package manager that installed it. The caller therefore
 * asks the user to quit and reopen, while the window and its state are still
 * intact.
 *
 * Only a packaged macOS build is watched. Windows keeps a running image and
 * its mapped files from being deleted, Linux packages are replaced by their
 * own package manager, and a development checkout has no bundle to prune.
 *
 * Pass Electron's `original-fs` as `fs`, not `fs`: Electron's patched fs keeps
 * an archive it has opened cached by path, so asking it whether app.asar exists
 * can answer from that cache after the file is gone.
 */

const DEFAULT_INTERVAL_MS = 60_000;
const DEFAULT_CONFIRM_DELAY_MS = 5_000;

/**
 * The parts of the running image that are no longer on disk.
 *
 * @param {object} o
 * @param {object} o.fs          needs existsSync.
 * @param {object} o.path        needs join.
 * @param {object} o.processObj  needs execPath and resourcesPath.
 * @returns {string[]}  the missing paths; empty when the image is intact or
 *                      when there is nothing to judge.
 */
function missingShellBundleParts({ fs, path, processObj }) {
  const parts = [];
  if (typeof processObj.execPath === "string" && processObj.execPath) {
    parts.push(processObj.execPath);
  }
  if (typeof processObj.resourcesPath === "string" && processObj.resourcesPath) {
    parts.push(path.join(processObj.resourcesPath, "app.asar"));
  }
  return parts.filter((part) => {
    try {
      return !fs.existsSync(part);
    } catch {
      // A probe that cannot answer is not evidence the file is gone.
      return false;
    }
  });
}

/**
 * @param {object} o
 * @param {object} o.fs
 * @param {object} o.path
 * @param {object} o.processObj
 * @param {boolean} o.isPackaged          app.isPackaged.
 * @param {(missing: string[]) => void} o.onPruned  runs once, on a confirmed miss.
 * @param {() => boolean} [o.isQuitting]  skip while the app is shutting down.
 * @param {() => boolean} [o.isUpdating]  skip while an update owns the bundle.
 * @param {(message: string) => void} [o.log]
 * @param {number} [o.intervalMs]
 * @param {number} [o.confirmDelayMs]
 * @param {Function} [o.setIntervalFn]
 * @param {Function} [o.clearIntervalFn]
 * @param {Function} [o.setTimeoutFn]
 * @param {Function} [o.clearTimeoutFn]
 */
function createShellBundleWatch({
  fs,
  path,
  processObj,
  isPackaged,
  onPruned,
  isQuitting = () => false,
  isUpdating = () => false,
  log = () => {},
  intervalMs = DEFAULT_INTERVAL_MS,
  confirmDelayMs = DEFAULT_CONFIRM_DELAY_MS,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}) {
  const enabled = processObj.platform === "darwin" && Boolean(isPackaged);
  let interval = null;
  let confirmTimer = null;
  let fired = false;

  const busy = () => {
    try {
      return Boolean(isQuitting()) || Boolean(isUpdating());
    } catch {
      return false;
    }
  };

  const missingNow = () => missingShellBundleParts({ fs, path, processObj });

  function stop() {
    if (interval !== null) {
      clearIntervalFn(interval);
      interval = null;
    }
    if (confirmTimer !== null) {
      clearTimeoutFn(confirmTimer);
      confirmTimer = null;
    }
  }

  function confirm() {
    confirmTimer = null;
    if (fired || busy()) return;
    const missing = missingNow();
    if (missing.length === 0) {
      log("shell bundle: back on disk at the second probe (swapped in place); nothing to do");
      return;
    }
    fired = true;
    stop();
    log(`shell bundle: this app's own files are gone (${missing.join(", ")}); asking the user to restart`);
    try {
      onPruned(missing);
    } catch (error) {
      log(`shell bundle: restart prompt failed: ${error && error.message}`);
    }
  }

  /** Probe now; a miss arms the confirming probe. Returns whether one is pending. */
  function checkNow() {
    if (!enabled || fired || busy()) return false;
    if (confirmTimer !== null) return true;
    const missing = missingNow();
    if (missing.length === 0) return false;
    log(`shell bundle: missing ${missing.join(", ")}; confirming in ${confirmDelayMs}ms`);
    confirmTimer = setTimeoutFn(confirm, confirmDelayMs);
    return true;
  }

  function start() {
    if (!enabled || fired || interval !== null) return;
    interval = setIntervalFn(() => { checkNow(); }, intervalMs);
    if (interval && typeof interval.unref === "function") interval.unref();
  }

  return Object.freeze({ start, stop, checkNow, enabled });
}

/**
 * Ask the user to quit and reopen the app, once its bundle is gone. Quitting
 * is the user's choice: an open composer draft or an unsent prompt is theirs
 * to finish first, and "Later" keeps the app running exactly as it was.
 * Never rejects.
 *
 * @param {object} o
 * @param {object} o.dialog        Electron dialog.
 * @param {() => void} o.requestQuit
 * @param {(message: string) => void} [o.log]
 * @returns {Promise<boolean>}  true when the user chose to quit.
 */
async function promptRestartForPrunedBundle({ dialog, requestQuit, log = () => {} }) {
  let response = 1;
  try {
    ({ response } = await dialog.showMessageBox({
      type: "warning",
      title: "Restart Kiro Crew",
      message: "Kiro Crew was updated while it was running.",
      detail: "The copy of Kiro Crew this window runs from has been removed, so it can "
        + "close without warning. Your chats and the gateway are not affected. "
        + "Quit and open Kiro Crew again to finish the update.",
      buttons: ["Quit Kiro Crew", "Later"],
      defaultId: 0,
      cancelId: 1,
    }));
  } catch (error) {
    log(`shell bundle: restart prompt could not be shown: ${error && error.message}`);
    return false;
  }
  if (response !== 0) {
    log("shell bundle: user chose to keep running from the removed bundle");
    return false;
  }
  log("shell bundle: user chose to quit and reopen");
  requestQuit();
  return true;
}

module.exports = {
  createShellBundleWatch,
  missingShellBundleParts,
  promptRestartForPrunedBundle,
  DEFAULT_INTERVAL_MS,
  DEFAULT_CONFIRM_DELAY_MS,
};
