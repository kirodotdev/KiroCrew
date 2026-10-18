"use strict";

const { nextInstallingDialogState } = require("../../bundle-integrity");
const { tailLines } = require("../../gateway-wait");
const { unrecoverableGatewayDialog } = require("../../gateway-recovery");

// How often the "installation still finishing" dialog re-probes the bundle.
// Extraction runs for minutes (about eight in the install that motivated the
// dialog), so a tick this size costs nothing while still landing the retry
// within seconds of the last part arriving.
const INSTALLING_PROBE_MS = 5_000;
// How long the dialog shows "Installation finished" before it closes itself.
// Painting and closing in the same tick would give the line at most one frame.
const INSTALLING_COMPLETE_LINGER_MS = 700;

/**
 * The windows a gateway failure puts in front of a person: the bounded failure
 * dialog with its scrollable launch-log pane and action row (including the
 * installing variant that re-probes the bundle and retries by itself), the
 * restart-required dialog that ends a recovery the supervisor cannot finish,
 * and the reveal that runs before the token prompt, the failure dialog and the
 * restart-required dialog.
 *
 * For the failure dialog the supervisor words the failure and acts on the
 * action the user picks; this module renders the choice and resolves that
 * action. The restart-required dialog is complete here: its copy comes from
 * unrecoverableGatewayDialog, and it reveals the window, offers the log and
 * then quits the app or closes the connection window.
 *
 * @param {object} deps
 * @param {() => boolean} deps.getInstallingUpdate  whether an update install
 *        has been dispatched, re-read at every probe tick and at the end of the
 *        linger, so the installing dialog never retries into a bundle the
 *        updater is replacing.
 */
function createFailureDialog({
  app,
  BrowserWindow,
  nativeTheme,
  shell,
  fs,
  isMac: IS_MAC,
  log: glog,
  gatewayLogPath,
  mainWindow,
  quitting,
  quitApp,
  cancelTrayHide,
  getInstallingUpdate,
  setTimeoutFn,
  setIntervalFn,
  clearIntervalFn,
}) {
  /**
   * Gateway failures need a bounded window with a scrollable launch-log pane;
   * native message boxes grow vertically with the entire detail string.
   */
  function showGatewayErrorDialog(parentWindow, options) {
    const {
      title: configuredTitle,
      message: configuredMessage,
      logTail,
      logPath: displayedLogPath,
      portConflict,
      noRetry = false,
      localGatewayOff = false,
      offerLocalStart = false,
      crewAction = null,
      primaryAction: configuredPrimaryAction,
      primaryLabel: configuredPrimaryLabel,
      retryLabel: configuredRetryLabel,
      accentQuit: accentQuitAction = false,
      showQuitButton: configuredShowQuitButton,
      // Installing-kind failures only. Re-run every INSTALLING_PROBE_MS while the
      // dialog is up; the message tracks the falling part count and, once the
      // probe reports complete, the dialog fires its own Retry. That retry is the
      // SAME action a click produces, resolved through the same closed handshake,
      // so it re-enters startGateway exactly once and by the caller's path -- no
      // second respawn owner beside recoverWedgedGateway or the liveness monitor.
      // The probe stands down for good, without firing, once the app is quitting
      // or an update install is dispatched: the updater deliberately stops the
      // gateway while it swaps the bundle, and a retry then would relaunch what
      // it is replacing.
      autoRetryProbe,
      // The refusal followed a stale-bundle incident: an unresolvable launcher
      // means the backend was removed, and the probe's copy says so.
      backendPruned: prunedBackend = false,
    } = options;
    const showQuitButton = configuredShowQuitButton ?? !noRetry;
    // The installing dialog opens on what the bundle looks like NOW, not on the
    // refusal-time text: Reveal Log closes and reopens this dialog through the
    // caller's loop, and the hoisted message would paint the original count
    // again after the probe had already shown it falling.
    const opening = typeof autoRetryProbe === "function"
      ? (() => {
        try { return nextInstallingDialogState(autoRetryProbe(), { pruned: prunedBackend }); }
        catch { return null; }
      })()
      : null;
    const title = opening && !opening.complete ? opening.title : configuredTitle;
    const message = opening && !opening.complete ? opening.message : configuredMessage;
    // The other escape hatch from the client-only state: name the crew on another
    // machine, or correct the address already stored. Without it this dialog
    // offers no way to reach a crew, so a launch that finds nothing can only
    // retry the same state or quit.
    const remoteCrew = noRetry ? null : crewAction;
    // Client-only mode launched nothing, so there is no launch to diagnose:
    // the log on disk belongs to earlier runs, and rendering that tail is what
    // made a state the user asked for read as a crash report.
    const showLog = !localGatewayOff;

    return new Promise((resolve) => {
      const dark = nativeTheme.shouldUseDarkColors;
      const hasParent = parentWindow && !parentWindow.isDestroyed();
      const errorWindow = new BrowserWindow({
        // Four actions share this row when a crew can be named here, and each
        // label is one line only if the row has room for it.
        width: remoteCrew ? 700 : 620,
        // Without the log pane there is nothing to scroll, so the tall window
        // would open mostly empty under a two-line message.
        height: showLog ? 460 : 260,
        minWidth: 460,
        minHeight: showLog ? 320 : 200,
        resizable: true,
        useContentSize: true,
        parent: hasParent ? parentWindow : undefined,
        modal: !!hasParent,
        backgroundColor: dark ? "#1e293b" : "#f8fafc",
        webPreferences: { nodeIntegration: false, contextIsolation: true },
      });
      errorWindow.setMenu(null);

      const escapeHtml = (value) => String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      // Only the Enter key binding reads this: the row builds each button's own
      // label below, so a separate primary label would be a second place for the
      // rendered text to drift from the action it fires.
      // A pruned dialog accents Quit, so Enter presses Quit too: the keyboard
      // default is the highlighted button.
      const primaryAction = configuredPrimaryAction
        || (noRetry || prunedBackend ? "quit" : (portConflict ? "force-retry" : "retry"));
      // Client-only mode cannot reach dashboard Settings to reverse the choice,
      // so this pre-dashboard UI carries the only in-app way back. The spawn
      // binds THIS port: on a crew's port a gateway started in place would
      // shadow that crew, so there the action re-runs port selection in a fresh
      // process instead. The caller decides whether that route is available.
      //
      // Both routes stay on screen whenever both exist, and ONLY the accent
      // moves -- which means the row cannot be built primary-first. Ordering by
      // emphasis moved Start Local Gateway from the third slot to the first and
      // pushed Retry the other way, so between two openings of a dialog carrying
      // the same title the leftmost button did two different things and a habit
      // press landed on the wrong recovery. Each action keeps its own slot and
      // the accent travels between them: the retry-family action first, then the
      // crew form, then the local-start route.
      const promotedLocalStart = primaryAction === "enable-retry";
      // Demoted, the retry slot is the one control whose target the surrounding
      // sentence does not give -- that paragraph is about a local start -- so the
      // caller may name the crew in its label. It may name it in the UNPROMOTED
      // states too, and does: labelling only the promoted ones made one control
      // read as two, "Retry" in one opening of this dialog and "Retry <host>" in
      // the next. Only where the slot really is a plain retry, though -- a
      // force-stop or a quit is a different action and keeps its own words.
      const retrySlotAction = promotedLocalStart
        ? "retry"
        : (configuredPrimaryAction
          || (noRetry ? "quit" : (portConflict ? "force-retry" : "retry")));
      const retrySlotLabel = promotedLocalStart
        ? (configuredRetryLabel || "Retry")
        : (configuredPrimaryLabel
          || (noRetry
            ? "Quit"
            : (portConflict ? "Force-stop & Retry" : (configuredRetryLabel || "Retry"))));
      // One state's paragraph asks for Quit: the ownership probe could not run, so
      // clicking either of the two action buttons reaches the same check, and
      // quitting and reopening is what recovers. The accent follows the sentence
      // in this dialog, so here it sits on Quit and every other slot is a
      // secondary -- an orange Retry under text that says to quit is the
      // contradiction this removes.
      const accentQuit = !!accentQuitAction && showQuitButton;
      const retrySlotButton = `<button class="${promotedLocalStart || accentQuit ? "cancel" : "ok"}" `
        + `onclick="act('${retrySlotAction}')">${escapeHtml(retrySlotLabel)}</button>`;
      // Present whenever the route exists, accented only when it is the primary,
      // so it is never rendered twice and never silently withdrawn.
      const enableButton = offerLocalStart && !noRetry
        ? `<button class="${promotedLocalStart && !accentQuit ? "ok" : "cancel"}" `
          + "onclick=\"act('enable-retry')\">Start Local Gateway</button>"
        : "";
      // The label names which of the two this is, because correcting a stored
      // address and naming a first one are the same form and different intents.
      const remoteSetupButton = remoteCrew
        ? `<button class="cancel" onclick="act('configure-remote')">`
          + `${remoteCrew === "edit" ? "Edit" : "Add"} Remote Crew…</button>`
        : "";
      const foreground = dark ? "#e2e8f0" : "#1e293b";
      const muted = dark ? "#94a3b8" : "#64748b";
      // One blank line in the message is a paragraph break. These messages carry
      // two stories at once -- what happened to the local start, and what to do
      // about the crew -- and run together they read as a single wall a reader
      // gives up on partway through. The branches join the two with a blank line
      // and the rendering honours it; a message with no blank line is unchanged.
      const renderParagraphs = (text) => String(text)
        .split(/\n{2,}/)
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => `<p>${escapeHtml(part)}</p>`)
        .join("");
      const messageParagraphs = renderParagraphs(message);
      const logPane = showLog
        ? `<div class="pathline">${escapeHtml(displayedLogPath)}</div>`
          + `<pre class="log">${escapeHtml(logTail || "(launch log is empty)")}</pre>`
        : "";
      const revealButton = showLog
        ? "<button class=\"cancel\" onclick=\"act('reveal')\">Reveal Log</button>"
        : "";
      const html = `<!DOCTYPE html><html><head><style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,sans-serif; padding:20px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${foreground};
          display:flex; flex-direction:column; height:100vh; }
        .title { font-size:15px; font-weight:700; margin-bottom:6px; }
        .msg { font-size:13px; line-height:1.45; margin-bottom:10px; }
        .msg p + p { margin-top:8px; }
        .pathline { font-size:11px; color:${muted}; margin-bottom:6px; word-break:break-all; }
        pre.log { flex:1 1 auto; min-height:120px; overflow:auto; white-space:pre;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; line-height:1.45;
          padding:10px; border-radius:6px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;
          margin-bottom:14px; }
        .row { display:flex; gap:8px; flex:0 0 auto; }
        button { flex:1; padding:9px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
        .ok { background:#f97316; color:#fff; } .ok:hover { background:#ea580c; }
        .cancel { background:${dark ? "#334155" : "#e2e8f0"}; color:${dark ? "#94a3b8" : "#475569"}; }
        .cancel:hover { background:${dark ? "#475569" : "#cbd5e1"}; }
        button:disabled { opacity:.45; cursor:default; }
      </style></head><body>
        <div class="title">${escapeHtml(title)}</div>
        <div class="msg">${messageParagraphs}</div>
        ${logPane}
        <div class="row">
          ${retrySlotButton}
          ${remoteSetupButton}
          ${enableButton}
          ${revealButton}
          ${showQuitButton ? `<button class="${accentQuit ? "ok" : "cancel"}" onclick="act('quit')">Quit</button>` : ""}
        </div>
        <script>
          function act(a){ document.title = 'mc-action:' + a; window.close(); }
          // Enter presses the primary BUTTON rather than calling act() directly,
          // so a button the completion paint has disabled stays disabled for the
          // keyboard too; Escape maps to Quit, which is never disabled. The
          // completion paint reassigns primaryAct when it moves the accent.
          var primaryAct = "act('${primaryAction}')";
          document.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
              for (const b of document.querySelectorAll('button')) {
                if (b.getAttribute('onclick') !== primaryAct) continue;
                if (!b.disabled) b.click();
                break;
              }
            }
            if (e.key === 'Escape') act('quit');
          });
        </script>
      </body></html>`;

      let action = null;
      let probeTimer = null;
      const stopProbe = () => {
        if (probeTimer === null) return;
        clearIntervalFn(probeTimer);
        probeTimer = null;
      };
      if (typeof autoRetryProbe === "function") {
        let paintedMessage = message;
        probeTimer = setIntervalFn(() => {
          if (errorWindow.isDestroyed() || quitting() || getInstallingUpdate()) {
            stopProbe();
            return;
          }
          let state;
          try {
            state = nextInstallingDialogState(autoRetryProbe(), { pruned: prunedBackend });
          } catch (probeError) {
            glog(`install probe failed (${probeError && probeError.message}); will probe again`);
            return;
          }
          if (state.complete) {
            // Stop before closing: the closed handler also stops, but a tick
            // must never be able to fire twice for one dialog.
            stopProbe();
            glog("bundle complete — retrying the gateway from the install dialog after the linger");
          }
          if (state.message !== paintedMessage) {
            paintedMessage = state.message;
            // Same paragraph rendering as the first paint; the markup is built
            // here from escaped text, so the page only ever receives HTML this
            // process composed.
            errorWindow.webContents
              .executeJavaScript(`document.querySelector(".msg").innerHTML = ${JSON.stringify(renderParagraphs(state.message))}`)
              .catch(() => { /* window closing under the paint */ });
          }
          if (state.complete) {
            // The whole frame says "finished", not just the paragraph: the title
            // moves with it, Retry is disabled and relabelled because the retry
            // is already under way (Enter routes through the same button, so the
            // keyboard cannot fire what the mouse cannot), and Quit says what it
            // now does. A click during the linger still wins: it sets the action
            // and closes first, and this close finds the window already gone.
            errorWindow.webContents
              .executeJavaScript(
                `document.querySelector(".title").textContent = ${JSON.stringify(state.title)};`
                + " for (const b of document.querySelectorAll('button')) {"
                + " if (b.getAttribute('onclick') === \"act('retry')\") { b.disabled = true; b.textContent = 'Starting…'; }"
                + " if (b.getAttribute('onclick') === \"act('quit')\") b.textContent = 'Quit anyway';"
                // A pruned dialog opened with Quit accented; the finished frame
                // puts the accent back on the busy Retry, so "Quit anyway" never
                // reads as the recommended action.
                + " if (b.getAttribute('onclick') === \"act('retry')\") b.className = 'ok';"
                + " if (b.getAttribute('onclick') === \"act('quit')\") b.className = 'cancel'; }"
                + " primaryAct = \"act('retry')\";",
              )
              .catch(() => { /* window closing under the paint */ });
            setTimeoutFn(() => {
              if (errorWindow.isDestroyed()) return;
              // The retry is decided HERE, not at the tick: a quit or an update
              // install that began during the linger must find no retry armed,
              // or startGateway would spawn into the bundle the updater is
              // swapping. The dialog then simply stays, with its buttons live.
              if (quitting() || getInstallingUpdate()) {
                glog("bundle complete but the app is quitting or installing an update — leaving the dialog to the user");
                return;
              }
              // A click lands as a title update first and the close a beat
              // later; in that gap the window is not destroyed yet, and the
              // user's choice must not be overwritten by the auto-retry.
              if (action !== null) return;
              action = "retry";
              errorWindow.close();
            }, INSTALLING_COMPLETE_LINGER_MS);
          }
        }, INSTALLING_PROBE_MS);
      }
      errorWindow.on("page-title-updated", (_event, updatedTitle) => {
        if (updatedTitle && updatedTitle.startsWith("mc-action:")) {
          // The user chose; a probe landing after this must not overrule them.
          stopProbe();
          action = updatedTitle.slice("mc-action:".length);
        }
      });
      errorWindow.on("closed", () => {
        stopProbe();
        resolve(action || "quit");
      });
      errorWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    });
  }

  /** Reveal only states which need a human decision. */
  function revealForUserDecision(window) {
    if (!window || window.isDestroyed() || quitting()) return;
    // The main window can own an app-level fullscreen hide while this
    // needs-user state belongs to a connection window. Disarm that owner before
    // app.show() or its reassertion hides every window again.
    const primaryWindow = mainWindow();
    if (
      primaryWindow
      && primaryWindow !== window
      && !primaryWindow.isDestroyed()
    ) {
      cancelTrayHide(primaryWindow);
    }
    // Cancel before leaving fullscreen: the fullscreen-exit event can fire the
    // deferred hide listener and immediately undo this reveal.
    cancelTrayHide(window);
    // A fullscreen tray-close hides the whole APP (hide-to-tray.js), and a
    // hidden app ignores a window-level show. Unhide it first or the dialog
    // this reveal precedes parks invisibly. Harmless when the app is visible.
    if (IS_MAC && typeof app.show === "function") app.show();
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
    if (IS_MAC) app.focus({ steal: true });
  }

  async function showUnrecoverableGatewayError(window, failedPort, options = {}) {
    const { variant = "wedged", probeFailed = false } = typeof options === "string"
      ? { variant: options }
      : options;
    if (!window || window.isDestroyed()) return;
    revealForUserDecision(window);
    let logTail = "";
    try { logTail = tailLines(fs.readFileSync(gatewayLogPath(), "utf8"), 60); }
    catch { /* no log yet */ }
    const action = await showGatewayErrorDialog(window, {
      ...unrecoverableGatewayDialog({
        port: failedPort,
        variant,
        probeFailed,
        isPrimaryWindow: window === mainWindow(),
      }),
      logTail,
      logPath: gatewayLogPath(),
      port: failedPort,
      noRetry: true,
    });
    if (window.isDestroyed()) return;
    if (action === "reveal") {
      try { shell.showItemInFolder(gatewayLogPath()); }
      catch { /* best effort */ }
    }
    if (window === mainWindow()) quitApp();
    else window.destroy();
  }

  return {
    showGatewayErrorDialog,
    revealForUserDecision,
    showUnrecoverableGatewayError,
  };
}

module.exports = { createFailureDialog };
