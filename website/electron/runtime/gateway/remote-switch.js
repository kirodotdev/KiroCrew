"use strict";

const { getRemoteHostConfig, isSelectablePort, setRemoteHostConfig } = require("../../host-config");
const { setLocalGatewayEnabled } = require("../../local-gateway");
const {
  remoteCrewDraft,
  remoteCrewFieldsError,
  saveRemoteCrewConfig,
} = require("../../remote-crew-setup");

const SWITCHING_STATUS = "Switching to your remote crew…";
const PROMPT_TITLE = "Connect to a crew on another machine";
const PROMPT_SAVE_LABEL = "Connect";
const STOP_FAILED_MESSAGE = "The gateway on this computer did not stop, so Kiro Crew kept using it. "
  + "Try again, or quit Kiro Crew and reopen it.";

/**
 * First-run setup's way out for a user who will only use crews on other
 * machines: collect the crew's address, stop the gateway this app started for
 * the setup screen, and reconnect the main window to that crew with the
 * "Run a local gateway" setting off -- the state a client-only launch starts in.
 *
 * The switch happens in this process rather than through a relaunch because a
 * successor cannot confirm itself here: the confirmed-successor handoff needs
 * the port free before it spawns, and this port is held by the gateway being
 * replaced until that gateway is stopped.
 *
 * Only a gateway this app spawned and still runs is replaced. An adopted
 * gateway (a terminal's, a service's, another install's) is not this app's to
 * stop, so the port would stay held and the crew configured on it would be
 * shadowed by that gateway.
 */
function createRemoteSwitch({
  store,
  port,
  backendUrl,
  dialog,
  path,
  dirname,
  isWindows = false,
  log = () => {},
  mainWindow,
  quitting,
  promptRemoteCrew,
  splashQuery,
  currentThemeAccent,
  sendStatus,
  getGatewayProcess,
  getGatewayOwnership,
  setGatewayOwnership,
  getLivenessMonitor,
  setLivenessMonitor,
  setSwitchingToRemote,
  setRunLocalGateway,
  setGatewayStartFailure,
  stopGatewayGracefully,
  waitForPortFree,
  startGateway,
  showLoadingThenConnect,
}) {
  // One switch at a time: a second request while the form is open, or while
  // the gateway is stopping, answers "unavailable" instead of opening another.
  let inFlight = false;

  /**
   * Whether `window` may switch to a remote crew right now.
   *
   * @param {object} window  the sender's window, resolved by the trusted process.
   * @returns {boolean}
   */
  function canSwitchToRemoteCrew(window) {
    if (inFlight || quitting()) return false;
    if (!window || window.isDestroyed() || window !== mainWindow()) return false;
    if (getGatewayOwnership() !== "spawned") return false;
    const child = getGatewayProcess();
    if (!child || child.exitCode !== null) return false;
    // The crew is saved for this port, so the port has to be one a crew can be
    // saved for, and one no crew already claims (that state has its own form).
    if (!isSelectablePort(Number(port))) return false;
    if (getRemoteHostConfig(store, port)?.host) return false;
    return true;
  }

  /**
   * Ask for the crew and, once the form is saved, start the switch.
   *
   * Resolves before the switch finishes: the switch replaces the page that
   * asked, so that page cannot wait for its answer.
   *
   * @param {object} window
   * @returns {Promise<"switching"|"cancelled"|"unavailable">}
   */
  async function switchToRemoteCrew(window) {
    if (!canSwitchToRemoteCrew(window)) return "unavailable";
    inFlight = true;
    let fields = null;
    try {
      // The form opens with the tunnel option on: a first-run user has no
      // forward of their own, and only this app can keep one open for them.
      // Windows has no managed tunnel, so the option is not offered there.
      let draft = remoteCrewDraft({ manageTunnel: !isWindows });
      for (;;) {
        fields = await promptRemoteCrew(window, port, draft, {
          title: PROMPT_TITLE,
          saveLabel: PROMPT_SAVE_LABEL,
        });
        if (!fields || window.isDestroyed()) {
          inFlight = false;
          return "cancelled";
        }
        // Checked before anything is stopped, so a refused field costs the user
        // a second try at the form and never the gateway they are using.
        const error = remoteCrewFieldsError(port, fields);
        if (!error) break;
        draft = fields;
        await dialog.showMessageBox(window, { type: "error", title: "Invalid Input", message: error });
        if (window.isDestroyed()) {
          inFlight = false;
          return "cancelled";
        }
      }
    } catch (error) {
      inFlight = false;
      throw error;
    }
    // The form can stay open for as long as the user likes, and the gateway can
    // exit or the app can start quitting meanwhile.
    inFlight = false;
    if (!canSwitchToRemoteCrew(window)) return "unavailable";
    inFlight = true;
    void performSwitch(window, fields)
      .catch((error) => log(`remote switch: failed: ${error && error.message}`))
      .finally(() => { inFlight = false; });
    return "switching";
  }

  async function showSwitchFailure(window, message) {
    try {
      await dialog.showMessageBox(window, {
        type: "error",
        title: "Kiro Crew — could not switch",
        message,
      });
    } catch (error) {
      log(`remote switch: showing the failure dialog failed: ${error && error.message}`);
    }
  }

  async function performSwitch(window, fields) {
    log(`remote switch: replacing the local gateway on :${port} with a remote crew`);
    // The stop below is deliberate, and the liveness monitor would read it as
    // a wedge and restart the gateway being stopped.
    const monitor = getLivenessMonitor();
    if (monitor) {
      monitor.stop();
      setLivenessMonitor(null);
    }
    setSwitchingToRemote(true);
    try {
      window.webContents.loadFile(path.join(dirname, "loading.html"), {
        query: splashQuery(window, { accent: currentThemeAccent() }),
      });
    } catch { /* window may be tearing down */ }
    sendStatus(SWITCHING_STATUS);

    let freed = false;
    try {
      await stopGatewayGracefully();
      freed = await waitForPortFree();
    } catch (error) {
      log(`remote switch: stopping the local gateway failed: ${error && error.message}`);
    }
    // The stopped child's exit filed a start failure for it; the next connect
    // must not read that as this launch's own.
    setGatewayStartFailure(null);
    setSwitchingToRemote(false);
    if (window.isDestroyed() || quitting()) return;

    if (!freed) {
      log(`remote switch: :${port} is still held; keeping the local gateway`);
      await showSwitchFailure(window, STOP_FAILED_MESSAGE);
    } else {
      // Written only once the port is free: until then the gateway on it is this
      // app's own, and a crew recorded for the port would make every reader treat
      // that gateway as the crew.
      try {
        const { saved, error } = saveRemoteCrewConfig(store, port, fields);
        if (!saved) throw new Error(error);
        setLocalGatewayEnabled(store, false);
        setRunLocalGateway(false);
        setGatewayOwnership("none");
        log(`remote switch: crew ${fields.host} saved for :${port}; local gateway turned off`);
      } catch (error) {
        log(`remote switch: saving the crew failed after the stop: ${error && error.message}`);
        // A store write can throw after changing its value. Clear this port's
        // crew before allowing any local restart, preserving names/other ports.
        try {
          setRemoteHostConfig(store, port, {});
        } catch (restoreError) {
          log(`remote switch: clearing the crew failed: ${restoreError && restoreError.message}`);
        }
        try {
          setLocalGatewayEnabled(store, true);
        } catch (restoreError) {
          log(`remote switch: restoring the local gateway setting failed: ${restoreError && restoreError.message}`);
        }
        setRunLocalGateway(true);
        await showSwitchFailure(window, "Could not save the remote crew settings. "
          + "Kiro Crew will try to reconnect to the gateway on this computer. "
          + "Check that your settings folder is writable and has free space, then try again.");
      }
    }
    if (window.isDestroyed() || quitting()) return;
    // If clearing the crew failed, startGateway's existing crew-on-port guard
    // still prevents a local spawn there; reconnect exposes its recovery dialog.
    try {
      await startGateway();
    } catch (error) {
      log(`remote switch: starting the gateway failed: ${error && error.message}`);
    }
    if (window.isDestroyed() || quitting()) return;
    try {
      await showLoadingThenConnect(window, backendUrl);
    } catch (error) {
      log(`remote switch: reconnecting failed: ${error && error.message}`);
      if (window.isDestroyed() || quitting()) return;
      await showSwitchFailure(window, "Kiro Crew could not reconnect. Quit Kiro Crew and reopen it.");
    }
  }

  return { canSwitchToRemoteCrew, switchToRemoteCrew };
}

module.exports = {
  PROMPT_SAVE_LABEL,
  PROMPT_TITLE,
  STOP_FAILED_MESSAGE,
  SWITCHING_STATUS,
  createRemoteSwitch,
};
