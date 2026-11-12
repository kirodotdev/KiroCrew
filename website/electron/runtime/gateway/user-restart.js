"use strict";

const RESTARTING_STATUS = "Restarting gateway…";

/**
 * Settings → About's "Restart gateway" in the desktop app: stop the gateway
 * this app spawned, start a fresh one, and reconnect the main window to it.
 *
 * The restart goes through the app instead of the gateway's own
 * `POST /api/restart` because that endpoint replaces the process with
 * `os.execv`, which this supervisor cannot follow. On macOS and Linux the pid
 * stays the same while the port is down until the successor binds, and the
 * liveness monitor reads three missed probes (about 20-30s) as a wedge and
 * SIGKILLs a successor that is still booting. On Windows CPython's `execv`
 * starts a new process and exits the old one, so the supervisor sees its child
 * exit, drops it, and quitting the app no longer stops the gateway. Stopping
 * and respawning here keeps the child, its liveness monitor and its quit stop
 * owned by the supervisor throughout.
 *
 * Only a gateway this app spawned and still runs is restarted, and only from
 * the main window while it is connected to it (an armed liveness monitor). An
 * adopted gateway (a terminal's, a service's) and a remote crew are not this
 * app's to stop, and a connect or recovery already in flight owns the gateway.
 */
function createUserRestart({
  backendUrl,
  log = () => {},
  mainWindow,
  quitting,
  isInstallingUpdate,
  paintLoadingScreen,
  sendStatus,
  getGatewayProcess,
  getGatewayOwnership,
  getLivenessMonitor,
  setLivenessMonitor,
  setRestartingGateway,
  setGatewayStartFailure,
  stopGatewayGracefully,
  respawnGateway,
  respawnSuperseded,
  showLoadingThenConnect,
}) {
  let inFlight = false;

  /**
   * Whether `window` may restart the gateway right now.
   *
   * @param {object} window  the sender's window, resolved by the trusted process.
   * @returns {boolean}
   */
  function canRestartGateway(window) {
    if (inFlight || quitting() || isInstallingUpdate()) return false;
    if (!window || window.isDestroyed() || window !== mainWindow()) return false;
    if (getGatewayOwnership() !== "spawned") return false;
    const child = getGatewayProcess();
    if (!child || child.exitCode !== null) return false;
    return !!getLivenessMonitor();
  }

  /**
   * Start the restart. Resolves before it finishes: the restart replaces the
   * page that asked, so that page cannot wait for its answer.
   *
   * @param {object} window
   * @returns {"restarting"|"unavailable"}
   */
  function restartGateway(window) {
    if (!canRestartGateway(window)) return "unavailable";
    inFlight = true;
    void performRestart(window)
      .catch((error) => log(`gateway restart: failed: ${error && error.message}`))
      .finally(() => { inFlight = false; });
    return "restarting";
  }

  // The page the user was on, so the reconnect lands them back on it.
  function currentPath(window) {
    try {
      const url = new URL(window.webContents.getURL());
      return url.origin === backendUrl ? `${url.pathname}${url.search}${url.hash}` : "";
    } catch {
      return "";
    }
  }

  async function performRestart(window) {
    log("gateway restart: requested from Settings — stopping the gateway this app runs");
    const initialPath = currentPath(window);
    // The stop below is deliberate; the monitor would read it as a wedge.
    const monitor = getLivenessMonitor();
    if (monitor) {
      monitor.stop();
      setLivenessMonitor(null);
    }
    setRestartingGateway(true);
    paintLoadingScreen(window);
    sendStatus(RESTARTING_STATUS);
    try {
      await stopGatewayGracefully();
    } catch (error) {
      // The connect below still runs: a gateway left holding the port is then
      // met by the launch's own holder checks and dialogs, never a blank splash.
      log(`gateway restart: stopping the gateway failed: ${error && error.message}`);
    }
    // The stopped child's exit filed a start failure for it; the respawn must
    // not read that as its own.
    setGatewayStartFailure(null);
    setRestartingGateway(false);
    // Respawned even when the window closed meanwhile (the app lives on in the
    // tray), so the next window finds a gateway; only the connect needs it.
    if (quitting()) return;
    // Through the respawn owner, so a dialog Retry or an update install that
    // took over meanwhile is joined or wins instead of racing a second launch.
    if ((await respawnGateway()) === respawnSuperseded) return;
    if (window.isDestroyed() || quitting()) return;
    await showLoadingThenConnect(window, backendUrl, { initialPath });
  }

  return { canRestartGateway, restartGateway };
}

module.exports = { RESTARTING_STATUS, createUserRestart };
