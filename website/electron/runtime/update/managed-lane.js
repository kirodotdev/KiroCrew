const { createManagedExec } = require("./managed-exec");
const { createUpdateArm } = require("./update-arm");
const { createRelaunch, runArmedApply } = require("./armed-apply");
const { isGatewayStillRunning: gatewayStillRunning } = require("../../gateway-stop");

/**
 * The marker-driven update lane: an EXTERNALLY-MANAGED marker that carries an
 * `updateCommand` (and a required `checkCommand`) owns this install's
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
  // When a security policy provider governs this install's updates, the managed
  // lane must also stand down: the policy provider and the EXTERNALLY-MANAGED
  // command would otherwise both try to apply updates on the same install.
  isPolicyGoverned = () => false,
  // On Windows, BaseWindow emits 'session-end' but App does not, so the
  // session-end notice listens on the main window this provider returns.
  getMainWindow = () => null,
  Notification,
  osPlatform = process.platform,
  log,
  // The desktop's record of an armed update (update-arm.js), kept in the app's
  // store so it survives a quit whose apply was interrupted. Memory-only when a
  // host injects none.
  updateArm = createUpdateArm({ store: (() => { const m = new Map(); return { get: (k, d) => (m.has(k) ? m.get(k) : d), set: (k, v) => m.set(k, v), delete: (k) => m.delete(k) }; })() }),
  // Best-effort client for the gateway's own armed record (gateway-arm.js).
  gatewayArm = null,
  // A start-time apply that failed (armed-apply.js): shown once, as an install error.
  startupApplyError = null,
}) {
  // Whether a security policy provider owns this install's updates right now.
  // A live verdict is also recorded in the app's store, so a start-time apply
  // (startup-apply.js), which runs before any window can report it, sees the last
  // one. A reader that returns null has no live verdict yet (the dashboard has not
  // reported): the recorded one stands and is not overwritten. A throwing reader
  // fails toward "governed": running the marker's command over a provider that may
  // own the install is the unsafe direction.
  const governed = () => {
    let g = true;
    try {
      const v = isPolicyGoverned();
      if (v === null || v === undefined) {
        let last = true;
        try { last = updateArm.wasGoverned(); } catch { /* unreadable: fail toward governed */ }
        return last;
      }
      g = !!v;
    } catch (err) {
      log.error("[update] isPolicyGoverned threw - treating the install as governed", err);
    }
    try { updateArm.recordGoverned(g); } catch { /* an unwritable store keeps the last verdict */ }
    return g;
  };

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

  // A relaunch that comes back on the NEW bundle: a bare app.relaunch() re-runs
  // the command line this process started with, which for a Dock-opened macOS
  // app is its own version's payload path (#22774). Only a user-asked restart
  // uses it (updateAPI.relaunch); a quit-time apply never relaunches.
  const relaunchAfterApply = createRelaunch({ managed, app, log });

  let foundVersion = null; // last version discovered by the checkCommand, awaiting apply
  let stagedVersion = null; // version the user took Download for (the ready dialog's version)
  let managedQuitArmed = false; // is a before-quit auto-apply handler installed?
  let managedInstalling = false; // an apply is in progress — pause the poll
  let relaunchScheduled = false; // a Restart now already asked the app to come back
  let sessionEndWindow = null; // window holding the session-end notice listener

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

  const exec = createManagedExec({ managed, managedPath, log });
  const { runManagedCommand, MANAGED_CHECK_TIMEOUT_MS, MANAGED_VERSION_CAP } = exec;

  // Fresh-read the auto-download preference; a throwing reader fails toward
  // NOT auto-installing (same direction as the feed path's deferred handler).
  const autoDownloadOn = () => {
    try { return !!getAutoDownloadPreference(); } catch (err) {
      log.error("[update] getAutoDownloadPreference threw — treating as off", err);
      return false;
    }
  };

  // The update the user's quit applies, or null. A user-armed update always
  // applies. A discovered one applies only while auto-download is on, re-read
  // FRESH here so a toggle-off between discovery and quit is honored.
  const versionToApplyOnQuit = () => {
    const rec = updateArm.read();
    if (rec) return rec.version;
    if (foundVersion && autoDownloadOn()) return foundVersion;
    return null;
  };

  // The quit-time apply (D-b). It is the ONE point an update is applied while
  // the app is running at all: the user quit, the gateway is stopped, and only
  // then does the update command run. The app then EXITS and stays down; the
  // user's next start is the new version. An apply that was interrupted or that
  // failed leaves the arm in place, and the next app start finishes it before
  // the gateway spawns (startup-apply.js).
  // What the app does once the quit-time apply ends: a Restart now scheduled a
  // relaunch before the quit, any other quit just exits.
  const exitOutcome = () => (relaunchScheduled ? "relaunching" : "exiting without relaunch");

  const managedInstallOnQuit = (event) => {
    // A further quit request while the apply is in progress (main.js is already
    // stopping the gateway, and a second Cmd+Q, tray Quit or window close lands
    // here): hold it. The listener was registered with app.once, so without the
    // re-arm below Electron would exit mid-stop and skip the apply. Every end of
    // the apply is an explicit app.exit, so nothing needs to release the hold.
    if (managedInstalling) {
      event.preventDefault();
      app.once("before-quit", managedInstallOnQuit);
      return;
    }
    const appliedVersion = versionToApplyOnQuit();
    // Nothing pending (a later check cleared it, or it was already applied):
    // let the quit proceed normally.
    if (!appliedVersion) {
      log.info("[update] managed quit handler fired with no pending update - not applying");
      return;
    }
    // A policy provider owns this install's updates, so the marker's command is
    // not run for it, whatever was armed before governance began. The arm stays
    // for the day governance lifts; the quit proceeds untouched.
    if (governed()) {
      log.info("[update] policy governed: the managed apply on quit is left to the policy provider");
      return;
    }
    event.preventDefault();
    managedInstalling = true;
    app.once("before-quit", managedInstallOnQuit);
    (async () => {
      emit("installing", { version: appliedVersion });
      try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
      // Read the gateway's own armed record while it is still up, so an arm made
      // from the dashboard (not through this lane) is not lost with it.
      if (gatewayArm && !updateArm.read()) {
        try {
          const v = await gatewayArm.armedVersion();
          if (v) updateArm.arm(v);
        } catch { /* advisory */ }
      }
      try { if (stopGateway) await stopGateway(); } catch (err) {
        if (gatewayStillRunning(err)) {
          // The user asked to quit: honor that, without replacing files a live
          // gateway is still running from. The arm stays for the next start.
          log.error("[update] managed deferred install refused: a gateway from this app's bundle is still running - quitting without applying", err);
          app.exit(0);
          return;
        }
        log.error("[update] managed stop on quit errored", err);
      }
      // Governance can arrive while the gateway shuts down; re-check before the
      // marker's command is spent.
      if (governed()) {
        log.info("[update] policy governed (post-gateway-stop managed): quitting without applying");
        app.exit(0);
        return;
      }
      const version = updateArm.read() ? updateArm.read().version : appliedVersion;
      log.info(`[update] managed apply on quit - running update command for ${version}`);
      if (updateArm.read()) updateArm.markAttempt();
      const result = await runArmedApply({ exec, version, log });
      if (result.ok) {
        updateArm.clear();
        log.info(`[update] managed apply on quit finished - ${exitOutcome()}`);
      } else {
        // The arm stays: the next app start runs the apply again before the
        // gateway spawns, and shows the error if that fails too.
        log.error(`[update] managed apply on quit did not finish (${result.message}) - the arm stays, ${exitOutcome()}`);
      }
      app.exit(0);
    })();
  };

  // The desktop main reads the armed state. The dashboard arms an update through
  // the gateway's own POST /api/update (the pill and UpdateFoundModal do), which
  // main is not party to, so while an update is on offer main asks the gateway
  // and adopts what it finds into its own record. It is the record the quit
  // reads, because the gateway is gone by the time the apply runs. A gateway that
  // cannot be asked changes nothing; a gateway that answers "none" withdraws an
  // arm main had only adopted from it. A governed install adopts nothing.
  let adoptedFromGateway = false;
  const GATEWAY_ARM_POLL_MS = 30 * 1000;
  async function syncArmFromGateway() {
    if (!gatewayArm || managedInstalling) return;
    // A governed install's arm belongs to the policy provider; main would never
    // apply it, so it does not adopt (or withdraw) one.
    if (governed()) return;
    let v = null;
    try { v = await gatewayArm.armedVersion(); } catch { v = null; }
    if (v === null) return;
    if (v) {
      if (!updateArm.read()) {
        try { updateArm.arm(v); } catch (err) {
          log.error("[update] could not record the gateway's armed version", err);
          return;
        }
        adoptedFromGateway = true;
        armQuitHandler();
        log.info(`[update] the gateway holds ${v} as armed - it applies when you quit`);
        emit("downloaded", downloadedPayload(v));
      }
    } else if (adoptedFromGateway && updateArm.read()) {
      adoptedFromGateway = false;
      updateArm.clear();
      log.info("[update] the gateway withdrew the arm - main drops the one it adopted");
    }
  }

  // Install the one-shot quit handler (and the Windows session-end notice).
  const armQuitHandler = () => {
    if (!managedQuitArmed) {
      managedQuitArmed = true;
      app.once("before-quit", managedInstallOnQuit);
    }
    if (!sessionEndWindow) armSessionEndNotice();
  };

  // Undo a quit-time auto-apply armed by an earlier check and forget the
  // discovered version. Called when a later check finds nothing pending, so a
  // normal quit does not relaunch into an update the external manager already
  // applied or withdrew — the feed path clears its deferred state for the
  // same reason.
  const disarmManagedQuit = ({ keepArm = false } = {}) => {
    foundVersion = null;
    stagedVersion = null;
    // The check no longer offers anything, so an arm for it is moot too. A
    // policy stand-down says nothing about the arm, so it keeps it.
    if (!keepArm) updateArm.clear();
    // A stand-down that keeps the arm keeps the quit handler that applies it.
    if (managedQuitArmed && !(keepArm && updateArm.read())) {
      app.removeListener("before-quit", managedInstallOnQuit);
      managedQuitArmed = false;
    }
    disarmSessionEndNotice();
  };

  // Windows session-end notice. Windows shutdown, restart and sign-out never
  // emit before-quit, so managedInstallOnQuit is unreachable on those paths.
  // The notice tells the user the found update is waiting and never runs
  // updateCommand: Windows gives a process a few seconds at session end, an
  // apply may run up to MANAGED_APPLY_TIMEOUT_MS, and an external command
  // killed partway can leave a half-applied install. The toast persists in
  // Action Center across the session boundary.
  function armSessionEndNotice() {
    if (osPlatform !== "win32") return;
    disarmSessionEndNotice();
    let win = null;
    try { win = getMainWindow(); } catch { win = null; }
    if (!win || (typeof win.isDestroyed === "function" && win.isDestroyed())) return;
    win.once("session-end", sessionEndPendingNotice);
    sessionEndWindow = win;
  }

  function disarmSessionEndNotice() {
    const win = sessionEndWindow;
    sessionEndWindow = null;
    if (!win) return;
    try {
      if (!(typeof win.isDestroyed === "function" && win.isDestroyed())) {
        win.removeListener("session-end", sessionEndPendingNotice);
      }
    } catch { /* a torn-down window holds no listener */ }
  }

  function sessionEndPendingNotice() {
    // Registered with once(): the window has already dropped it.
    sessionEndWindow = null;
    if (managedInstalling) return;
    // Same rule as managedInstallOnQuit: say so only when a quit would apply it.
    const pending = versionToApplyOnQuit();
    if (!pending || governed()) return;
    try {
      new Notification({
        title: "Kiro Crew update ready",
        body: `An update (version ${pending}) was found but not installed, because Windows was closing. Quit Kiro Crew from the tray to install it.`,
      }).show();
    } catch { /* notifications optional */ }
    log.info("[update] managed session-end — notified user of pending update (install on next quit)");
  }

  // ONE check at a time. The launch timer, the poll and the renderer's Check
  // button all land here, and the commands are slow (up to
  // MANAGED_CHECK_TIMEOUT_MS). Two overlapping runs each emit their own
  // verdict, so the one that finishes second overwrites whatever the user did
  // in between -- a "found" landing after Download replaces "downloaded" and
  // closes the ready dialog under the Install click. Overlapping callers share
  // the run in flight instead, mirroring the feed lane's `checking` guard while
  // still resolving only when that run has finished.
  let checkInFlight = null;
  function managedCheck() {
    // Policy stand-down: if a security policy provider governs this install,
    // the managed lane's own check/apply must not run — both would otherwise
    // try to apply updates on the same install.
    if (governed()) {
      log.info("[update] policy governed: managed check suppressed");
      disarmManagedQuit({ keepArm: true });
      emit("not-available");
      return Promise.resolve();
    }
    // An install owns the state until it relaunches or fails, as in the feed
    // lane: a check requested now is skipped, re-surfacing "installing" for a
    // caller (the Check button) that cleared the card first.
    if (managedInstalling) {
      log.info("[update] managed check skipped: an install is in progress");
      emit("installing", { version: foundVersion });
      return Promise.resolve();
    }
    if (!checkInFlight) {
      checkInFlight = runManagedCheck().finally(() => { checkInFlight = null; });
    }
    return checkInFlight;
  }

  async function runManagedCheck() {
    // The ready dialog renders only while the state is "downloaded", so a
    // "checking" here would close it for the whole run (and swallow an Install
    // click) even when the check then re-surfaces the same version. With a
    // version readied, check quietly: the verdict below still re-surfaces it,
    // supersedes it, or withdraws it.
    if (!stagedVersion) emit("checking");
    // A check that cannot reach a verdict says nothing about the version the
    // user already readied, and the updateCommand will discover for itself on
    // Install. Replacing "downloaded" with a check error would close the ready
    // dialog the same way, so with a version readied the failure is logged and
    // the ready state re-surfaced: the Check button clears the card before it
    // calls in, and nothing else would bring it back.
    const checkFailed = (err) => {
      if (stagedVersion) {
        log.error(`[update] managed check failed with ${stagedVersion} readied — keeping it: ${err.message}`);
        emit("downloaded", downloadedPayload(stagedVersion));
        return;
      }
      emitManagedError("check", err);
    };
    if (!managed.checkCommand) {
      // The marker says how to APPLY an update but gives no way to DISCOVER
      // one. This is a check error, NOT a green "up to date": a silent
      // "latest" would hide every future update for this install forever.
      log.info("[update] managed: no checkCommand — cannot check for updates");
      checkFailed(new Error("this managed install has no checkCommand"));
      return;
    }
    const { code, stdout, failed } = await runManagedCommand(managed.checkCommand, {
      timeout: MANAGED_CHECK_TIMEOUT_MS,
    });
    // A check already running when Install was clicked (the launch timer, the
    // poll) must not replace "installing" with its verdict, nor change the
    // version being applied.
    if (managedInstalling) {
      log.info("[update] managed check finished during an install — dropping its verdict");
      return;
    }
    if (failed) {
      // Could not RUN the command (spawn error or timeout) — an error, not
      // "up to date". Mirrors the sibling CommandProvider, which returns an
      // error verdict for a check it could not execute.
      log.error("[update] managed check could not run");
      checkFailed(new Error("managed check command failed to run"));
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
      checkFailed(new Error("managed check command produced no version"));
      return;
    }
    foundVersion = version;
    // Re-check governance: if the policy provider became active while the
    // check command was running, discard the result so two update paths do
    // not compete on the same install.
    if (governed()) {
      log.info("[update] policy governed (post-command): discarding managed check result");
      foundVersion = null;
      disarmManagedQuit({ keepArm: true });
      emit("not-available");
      return;
    }
    if (stagedVersion === version) {
      // The user already took Download for this version and the ready dialog
      // is (or was) up. Re-surface it rather than demoting the card back to
      // "found", exactly as the feed lane does for a stage that is still
      // latest. A DIFFERENT version supersedes the stage and is offered anew.
      log.info(`[update] managed check: ${version} already readied — awaiting install`);
      emit("downloaded", downloadedPayload(version));
    } else {
      stagedVersion = null;
      log.info(`[update] managed check: update available -> ${version}`);
      emit("found", { version });
    }
    // Auto-on-restart: if the user allows auto-download, arm a one-shot
    // before-quit handler that applies on the natural quit, and on Windows the
    // session-end notice for the shutdown, restart and sign-out paths that
    // skip before-quit.
    if (autoDownloadOn() || updateArm.read()) armQuitHandler();
    syncArmFromGateway().catch(() => {});
  }

  async function managedDownload() {
    // Managed download+apply is ONE step (the updateCommand). "download" just
    // lights the UI Install action; it never applies. Discover first if the
    // UI raced the check.
    if (!foundVersion) {
      await managedCheck();
    }
    if (foundVersion) {
      stagedVersion = foundVersion;
      emit("downloaded", downloadedPayload(foundVersion));
    }
  }

  // The Install click ARMS (D-a). It never runs the update command and never
  // relaunches: an update is applied on the user's own quit (managedInstallOnQuit)
  // and, if that was interrupted, at the next app start (startup-apply.js). The
  // arm is recorded here in main, and told to the gateway when one is up so its
  // status page can say so; the second part may fail without changing anything.
  async function managedInstall() {
    const rec = updateArm.read();
    const version = foundVersion || (rec && rec.version) || null;
    if (!version) {
      log.info("[update] managed arm requested with nothing found - ignoring");
      emit("not-available");
      return;
    }
    if (managedInstalling) {
      log.info("[update] managed arm ignored: an apply is already in progress");
      return;
    }
    updateArm.arm(version);
    stagedVersion = version;
    armQuitHandler();
    log.info(`[update] armed ${version}: it applies when you quit, not before`);
    emit("downloaded", downloadedPayload(version));
    if (gatewayArm) {
      try {
        const told = await gatewayArm.arm();
        log.info(told ? "[update] the running gateway recorded the arm" : "[update] no running gateway recorded the arm - main holds it");
      } catch (err) {
        log.info(`[update] telling the gateway about the arm failed: ${(err && err.message) || err}`);
      }
    }
  }

  // The `downloaded` payload, carrying the armed flag once the user armed it.
  function downloadedPayload(version) {
    return { version, ...(updateArm.read() ? { armed: true } : {}) };
  }

  // D-d: a plain quit and relaunch with no update command of its own. An armed
  // update is applied by the quit it triggers, exactly as for any other quit,
  // and the relaunch below brings the app back (see relaunchAfterApply).
  function managedRelaunch() {
    relaunchScheduled = true;
    relaunchAfterApply();
    app.quit();
  }

  // Auto-check on launch and on the same interval as the feed path, so a
  // managed install DISCOVERS updates without the user clicking Check
  // (auto-update is on by default). Background checks only discover — an
  // apply still requires the auto-download preference or an explicit install.
  // The poll skips windows where an apply is already in flight, and both
  // timers are unref'd so they never hold the process open (Electron quit,
  // tests).
  // An arm that survived from an earlier run (the quit-time apply was
  // interrupted and the start-time apply has not cleared it) still applies on
  // the next quit.
  if (updateArm.read()) armQuitHandler();
  // A start-time apply that failed: the old version is running, and says why.
  if (startupApplyError) {
    emitManagedError("install", new Error(startupApplyError.message || "the armed update could not be applied at start"));
  }

  const managedLaunchTimer = setTimeout(() => {
    managedCheck().catch((err) => log.error("[update] managed launch check threw", err));
  }, launchCheckDelayMs);
  if (gatewayArm) {
    const gatewayArmTimer = setInterval(() => {
      // Unconditional: it also records the governance verdict for the next start.
      syncArmFromGateway().catch(() => {});
    }, GATEWAY_ARM_POLL_MS);
    if (typeof gatewayArmTimer.unref === "function") gatewayArmTimer.unref();
  }
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
    relaunch: () => managedRelaunch(),
    getInfo,
  };
}

module.exports = { createManagedLane };
