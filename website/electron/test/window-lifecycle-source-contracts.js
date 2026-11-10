"use strict";

function register(deps) {
(0, deps.describe)("window lifecycle source contracts", () => {
  (0, deps.it)("keys the remote-host writes with the same port the local-gateway gate reads", () => {
    // The gate and the forms that write `remoteHosts` must agree on the key, or
    // a crew the user records here classifies as a gateway on this machine. They
    // agree by both taking their port from the window's own backendUrl through
    // the normalizer, so pin that each of the three sites does.
    //
    // The rename dialog's body moved into `renameFocusedWindow` in
    // runtime/window/prompts.js when the modal prompts were extracted; the
    // facade's `renameCurrentWindow` now just delegates to it. Pin the
    // invariant where the code lives.
    const start = deps.SOURCE.indexOf("function promptRemoteHost(");
    deps.assert.notEqual(start, -1, "promptRemoteHost must exist");
    const body = deps.SOURCE.slice(start, deps.SOURCE.indexOf("\n  }\n", start));
    deps.assert.match(
      body,
      /defaultedPort\(focused\._mcBackendUrl\)/,
      "promptRemoteHost must key remoteHosts by the window's own normalized port",
    );
    const renameStart = deps.PROMPTS_SOURCE.indexOf("function renameFocusedWindow(");
    deps.assert.notEqual(renameStart, -1, "renameFocusedWindow must exist in prompts.js");
    const renameBody = deps.PROMPTS_SOURCE.slice(
      renameStart,
      deps.PROMPTS_SOURCE.indexOf("\n  }\n", renameStart),
    );
    deps.assert.match(
      renameBody,
      /defaultedPort\(focused\._mcBackendUrl\)/,
      "renameFocusedWindow must key remoteHosts by the window's own normalized port",
    );
    const gateStart = deps.SOURCE.indexOf("function isGatewayLocalForWindow(");
    deps.assert.notEqual(gateStart, -1);
    const gate = deps.SOURCE.slice(gateStart, deps.SOURCE.indexOf("\n  }\n", gateStart));
    deps.assert.match(
      gate,
      /getRemoteHostConfigForUrl\(store, url\)/,
      "the gate must read remoteHosts through the URL-aware resolver",
    );
  });

  (0, deps.it)("only claims SSH failed when an SSH attempt reported one", () => {
    // `fetchRemoteToken` keys its own lookup by port, so on a record the resolver
    // reached under the empty key it returns without running ssh at all. Naming
    // that "SSH to <host> failed" describes an attempt that never happened and
    // sends the reader to check a connection nothing used.
    const start = deps.SOURCE.indexOf("async function refreshToken(");
    deps.assert.notEqual(start, -1);
    const body = deps.SOURCE.slice(start, deps.SOURCE.indexOf("\n  }\n", start));
    deps.assert.match(body, /detail: sshError\s*\n\s*\? `SSH to \$\{config\?\.host/, "the SSH wording must be gated on sshError");
    deps.assert.match(body, /no SSH attempt was made/, "the no-attempt state must say so");
    deps.assert.doesNotMatch(
      body,
      /sshError \|\| "Check your connection\."/,
      "a falsy sshError must not be papered over with generic advice under an SSH heading",
    );
    // The selectability predicate must coerce to a number: `defaultedPort`
    // returns a STRING and `isSelectablePort` gates on `Number.isInteger`, so a
    // raw-string test is always false and sends every port down the unselectable
    // branch -- which then (below) prescribes an action that reopens the leak.
    deps.assert.match(
      body,
      /isSelectablePort\(Number\(targetPort\)\)/,
      "the selectability test must coerce the string port with Number(...)",
    );
    // The unselectable-port remedy must NOT tell the user to Clear: on a live
    // ssh -L tunnel the crew record is the only thing marking the window remote,
    // and clearing it makes isGatewayLocalForWindow read the loopback window as a
    // local gateway -- putting X-Internal-Secret through the tunnel, the exact
    // exposure this PR closes. Direct them to reopen on another port instead.
    const unselectableBranch = body.slice(body.indexOf("can't be used to save one"));
    deps.assert.doesNotMatch(
      unselectableBranch,
      /choose Clear|and clear the host/,
      "the unselectable-port remedy must not prescribe Clear (it removes the record marking a live tunnel remote)",
    );
    deps.assert.match(
      unselectableBranch,
      /Reconnect the crew on a different local/,
      "the unselectable-port remedy must direct the user to reconnect on a different local port",
    );
  });

  (0, deps.it)("retires a superseded empty-key record only after the write is durable", () => {
    // Order is the whole point. Retiring FIRST erases the record on a write that
    // is refused -- and `saveRemoteCrewConfig` refuses an unselectable port, so
    // on an http window resolving to :80 no write ever succeeds. The crew would
    // then read as this machine's own gateway with no way back. Pinned on the
    // source because the write runs inside a BrowserWindow's `closed` handler.
    const start = deps.SOURCE.indexOf("function promptRemoteHost(");
    deps.assert.notEqual(start, -1);
    const body = deps.SOURCE.slice(start, deps.SOURCE.indexOf("\n  }\n", start));
    deps.assert.match(
      body,
      /const retireLegacy = \(\) => \{\s*\n\s*if \(portIsSchemeDefault\(focused\._mcBackendUrl\)\) \{\s*\n\s*retireLegacyEmptyPortHost\(store, focusedPort\);/,
      "retire only for a URL whose port is its scheme default, keyed to the resolved port so the pinned name migrates",
    );
    const save = body.indexOf("saveRemoteCrewConfig(store, focusedPort, fields)");
    const refusedReturn = body.indexOf("return;", body.indexOf("title: \"Invalid Input\""));
    const calls = [...body.matchAll(/retireLegacy\(\);/g)].map((m) => m.index);
    deps.assert.ok(save !== -1 && refusedReturn !== -1, "the save write and the refusal must exist");
    // Retirement runs on the SAVE path only. The clear path never retires: a
    // scheme-default host-bearing record is intercepted by the Clear refusal
    // above, and on any other port there is no empty-key record to retire.
    deps.assert.equal(calls.length, 1, "one retirement, on the save path only");
    deps.assert.ok(
      calls[0] > refusedReturn,
      "the save path retires only past the refusal guard, so a refused save keeps the record",
    );
  });

  (0, deps.it)("refuses a Clear on a scheme-default port that still resolves to a legacy remote record", () => {
    // A Clear on a scheme-default port (:80/:443) can't write a per-port record,
    // so the only record marking this crew remote is the empty-key legacy one.
    // Deleting the resolved-key record AND retiring the legacy record here would
    // leave a FRESH default-port window later reading no crew --
    // isGatewayLocalForWindow then classifies the loopback window as a local
    // gateway and the host-presence heartbeat puts X-Internal-Secret through the
    // still-open ssh tunnel, the exact exposure this PR closes, re-opened across
    // windows past the per-window latch. With no in-app probe that a scheme-default
    // port is positively local (a :80 write is refused outright), the Clear must
    // REFUSE and keep BOTH records (a future default-port window still resolves
    // REMOTE from the store = fail-closed). Pinned on source: the write runs
    // inside a BrowserWindow `closed` handler that node:test cannot drive.
    const start = deps.SOURCE.indexOf("function promptRemoteHost(");
    deps.assert.notEqual(start, -1, "promptRemoteHost must exist");
    const body = deps.SOURCE.slice(start, deps.SOURCE.indexOf("\n  }\n", start));
    const clearBranch = body.indexOf("if (!host) {");
    deps.assert.notEqual(clearBranch, -1, "the Clear branch must exist");
    const guard = body.indexOf("portIsSchemeDefault(focused._mcBackendUrl)", clearBranch);
    const clearWrite = body.indexOf("setRemoteHostConfig(store, focusedPort, {})", clearBranch);
    const retire = body.indexOf("retireLegacy();", clearBranch);
    deps.assert.ok(
      guard !== -1 && clearWrite !== -1 && retire !== -1,
      "the scheme-default guard and the clear write/retire must all exist in the Clear branch",
    );
    // The refusal must be decided BEFORE the Clear deletes the resolved-key
    // record or retires the legacy one -- otherwise the exposure has already
    // been re-opened by the time the dialog shows.
    deps.assert.ok(
      guard < clearWrite && guard < retire,
      "the scheme-default refusal must be checked before the clear deletes or retires anything",
    );
    // The guard reads a HOST-BEARING legacy empty-key record (a hostless record
    // names no crew, so clearing it is safe) and refuses with an error dialog
    // that returns before the deletes.
    const guardBlock = body.slice(guard, clearWrite);
    deps.assert.match(
      guardBlock,
      /getRemoteHostConfigForUrl\(store, focused\._mcBackendUrl\)\?\.host/,
      "the refusal must be conditioned on the URL resolver so a host under the empty key OR the resolved 80/443 key is caught",
    );
    deps.assert.match(guardBlock, /type: "error"/, "the refusal must be an error dialog");
    // The remedy must NOT prescribe a route that cannot clear the record: the
    // default-port record lives under the resolved/empty key, so reconnecting on
    // a different selectable port and clearing THERE removes a different record
    // and leaves this one intact -- a dead end. It must tell the truth (the
    // setting is kept on purpose) and give a real way to stop reaching the crew.
    deps.assert.doesNotMatch(
      guardBlock,
      /clear it there|then open that tab and clear/,
      "the refusal must not send the user to clear the record on another tab (that clears a different record)",
    );
    deps.assert.match(
      guardBlock,
      /kept on purpose/,
      "the refusal must explain the setting is retained deliberately, not present clearing as pending",
    );
    deps.assert.match(
      guardBlock,
      /close this tab/,
      "the refusal must give a real way to stop reaching the crew (close the tab / stop the tunnel)",
    );
    deps.assert.match(
      guardBlock,
      /\breturn;/,
      "the refusal must return before setRemoteHostConfig / retireLegacy run",
    );
    // A selectable-port Clear is unaffected: portIsSchemeDefault is false there,
    // so control falls through the guard to the existing delete + retire + info
    // dialog. Pin that the normal clear path still follows the refusal guard.
    const clearedInfo = body.indexOf("cleared (using local token)", guard);
    deps.assert.ok(
      clearedInfo > guard && clearedInfo > clearWrite,
      "the selectable-port Clear must still delete the record and show the cleared info dialog",
    );
  });

  (0, deps.it)("tears embedded panels down before closing dashboard contents", () => {
    const setupStart = deps.SOURCE.indexOf("function setupWindowContents");
    const setupEnd = deps.SOURCE.indexOf("function applyDashboardChrome", setupStart);
    deps.assert.notEqual(setupStart, -1);
    deps.assert.notEqual(setupEnd, -1);
    const setup = deps.SOURCE.slice(setupStart, setupEnd);

    const destroyPanels = setup.indexOf("win._mcDestroyBrowserPanel(id)");
    const closeDashboard = setup.indexOf("view.webContents.close()");
    deps.assert.ok(destroyPanels !== -1, "browser panel cleanup missing");
    deps.assert.ok(closeDashboard !== -1, "dashboard WebContents cleanup missing");
    deps.assert.ok(
      destroyPanels < closeDashboard,
      "cleanup order must be panels -> dashboard contents",
    );
  });

  (0, deps.it)("registers the dashboard view as a capture surface on its own gateway origin", () => {
    // Screen capture is authorized against this registry, so a dashboard that is
    // never registered silently loses the chat composer's snip and the
    // web-preview crop. Pinned on the source because the security harness
    // registers a surface of its own, which would mask the call going missing.
    const setupStart = deps.SOURCE.indexOf("function setupWindowContents");
    const setupEnd = deps.SOURCE.indexOf("function applyDashboardChrome", setupStart);
    deps.assert.notEqual(setupStart, -1);
    deps.assert.notEqual(setupEnd, -1);
    const setup = deps.SOURCE.slice(setupStart, setupEnd);
    deps.assert.match(
      setup,
      /registerCaptureSurface\(view\.webContents, windowBackendUrl\)/,
      "the dashboard view must be registered against THIS window's gateway origin",
    );
  });

  (0, deps.it)("hands keyboard focus from a hidden or released browser view back to the dashboard view", () => {
    // A BaseWindow routes keystrokes to exactly one child view. The manager
    // decides WHEN to hand focus back (browser-view.test.js); this pins that
    // the window wires the hand-back to the dashboard view, and that a window
    // re-activation asks every panel to heal a hidden-yet-focused view. Without
    // the first, every dashboard text input goes deaf after a modal opens over
    // the panel; without the second, the platform can put focus back onto the
    // hidden view when the window is re-activated.
    const setupStart = deps.SOURCE.indexOf("function setupWindowContents");
    const setupEnd = deps.SOURCE.indexOf("function applyDashboardChrome", setupStart);
    deps.assert.notEqual(setupStart, -1);
    deps.assert.notEqual(setupEnd, -1);
    const setup = deps.SOURCE.slice(setupStart, setupEnd);

    // The panel registry is its own owner; the window hands it the dashboard view.
    deps.assert.match(setup, /const browserPanels = attachBrowserPanels\(win, view, \{/);
    const manager = deps.PANELS_SOURCE.match(/createBrowserViewManager\(\{([\s\S]*?)\n    \}\);/);
    deps.assert.ok(manager, "browser view manager wiring missing");
    deps.assert.match(
      manager[1],
      /focusHost:\s*\(\)\s*=>\s*\{[\s\S]*?view\.webContents\.focus\(\)/,
      "focusHost must give the DASHBOARD view (the window's `view`) keyboard focus",
    );
    deps.assert.match(
      manager[1],
      /focusHost:[\s\S]*?!view\.webContents\.isDestroyed\(\)[\s\S]*?view\.webContents\.focus\(\)/,
      "focusHost must not touch a dashboard WebContents that is already gone",
    );
    deps.assert.match(
      setup,
      /win\.on\("focus",\s*\(\)\s*=>\s*\{\s*for \(const entry of browserPanels\.values\(\)\) entry\.manager\.reclaimFocus\(\);/,
      "window re-activation must ask every panel to reclaim focus from a hidden view",
    );
  });

  (0, deps.it)("keeps immediate fullscreen bounds updates plus bounded settle passes", () => {
    deps.assert.match(
      deps.SOURCE,
      /const FULLSCREEN_SETTLE_MS = \[250, 1500\]/,
      "both the quick pass and slow-window-manager backstop are required",
    );
    for (const event of ["enter-full-screen", "leave-full-screen"]) {
      const match = deps.SOURCE.match(
        new RegExp(`win\\.on\\("${event}", \\(\\) => \\{([\\s\\S]*?)\\}\\);`),
      );
      deps.assert.ok(match, `${event} handler missing`);
      const body = match[1];
      const immediate = body.indexOf("updateViewBounds()");
      const notify = body.indexOf("sendFullScreen()");
      const settle = body.indexOf("scheduleFullscreenSettle()");
      deps.assert.ok(
        immediate !== -1 && notify !== -1 && settle !== -1,
        `${event} must update, notify and settle`,
      );
      deps.assert.ok(
        immediate < notify && notify < settle,
        `${event} ordering changed`,
      );
    }
    deps.assert.match(
      deps.SOURCE,
      /win\.on\("closed", \(\) => \{[\s\S]*?fullscreenSettleTimers[\s\S]*?clearTimeout/,
      "pending settle timers must be cleared at teardown",
    );
  });

  (0, deps.it)("reflows the dashboard view on a display rearrange and unbinds the screen listeners at teardown", () => {
    // A monitor hot-plug or a drag to a screen with a different backing scale
    // factor changes the window's content bounds without a reliable
    // "resize"/"move", so the child view keeps its old-scale geometry and the
    // dashboard ghosts across both displays. The window must re-run the reflow
    // on the display events, and — the pet-overlay leak (#4673) discipline — a
    // screen listener that outlives its window keeps firing against a destroyed
    // view, so bind and unbind must be paired.
    const setupStart = deps.SOURCE.indexOf("function setupWindowContents");
    const setupEnd = deps.SOURCE.indexOf("function applyDashboardChrome", setupStart);
    deps.assert.notEqual(setupStart, -1);
    deps.assert.notEqual(setupEnd, -1);
    const setup = deps.SOURCE.slice(setupStart, setupEnd);

    deps.assert.match(
      deps.SOURCE,
      /const DISPLAY_SETTLE_MS = \[0, 250, 1500\]/,
      "an immediate recompute plus bounded deferred passes are required for the delayed macOS scale-factor report",
    );

    const events = /const DISPLAY_EVENTS = \["display-metrics-changed", "display-added", "display-removed"\]/;
    deps.assert.match(setup, events, "all three display-rearrange events must drive the reflow");

    deps.assert.match(
      setup,
      /for \(const event of DISPLAY_EVENTS\) screen\.on\(event, onDisplayReflow\)/,
      "the display listeners must be bound to the reflow handler",
    );

    // The unbind must live in the same "closed" handler that clears the timers,
    // so a torn-down window leaves no screen listener behind.
    const closedMatch = setup.match(
      /win\.on\("closed", \(\) => \{([\s\S]*?)\n {4}\}\);/g,
    );
    deps.assert.ok(closedMatch, "a closed handler is required");
    const unbindHandler = closedMatch.find((block) => block.includes("displaySettleTimers"));
    deps.assert.ok(unbindHandler, "the display-timer cleanup must live in a closed handler");
    deps.assert.match(
      unbindHandler,
      /for \(const event of DISPLAY_EVENTS\) screen\.removeListener\(event, onDisplayReflow\)/,
      "every bound display listener must be removed at teardown (bind/unbind pairing, #4673)",
    );
  });

  (0, deps.it)("gives the dashboard's context menu the app origin and the browser panel none", () => {
    deps.assert.match(
      deps.SOURCE,
      /attachContextMenu\(view\.webContents, \{ getAppOrigin: \(\) => windowBackendUrl \}\)/,
      "the dashboard needs the origin so a chat file link copies as a bare path",
    );
    deps.assert.match(
      deps.PANELS_SOURCE,
      /onCreate: \(child\) => attachContextMenu\(child\.webContents\),/,
      "an arbitrary site's same-origin pathname is not a local file, so no origin here",
    );
  });

  (0, deps.it)("resolves every browser façade operation from the IPC sender's owner", () => {
    const resolver = deps.SOURCE.match(
      /function panelForSender\(sender, panelId, opts\) \{([\s\S]*?)\n  \}/,
    );
    deps.assert.ok(resolver, "panelForSender missing");
    deps.assert.match(
      resolver[1],
      /windowForWebContents\(sender\)/,
      "panel lookup must start from the sending dashboard",
    );

    for (const name of [
      "browserOpen",
      "browserNavigate",
      "browserSetBounds",
      "browserSetOverlay",
      "browserSetInactive",
      "browserClose",
      "browserGetState",
    ]) {
      const start = deps.SOURCE.indexOf(`function ${name}(`);
      const asyncStart = deps.SOURCE.indexOf(`async function ${name}(`);
      deps.assert.ok(
        start !== -1 || asyncStart !== -1,
        `${name} façade missing`,
      );
      const at = Math.max(start, asyncStart);
      const next = deps.SOURCE.indexOf("\n  function ", at + 1);
      const nextAsync = deps.SOURCE.indexOf("\n  async function ", at + 1);
      const ends = [next, nextAsync].filter((value) => value !== -1);
      const end = ends.length ? Math.min(...ends) : deps.SOURCE.length;
      const body = deps.SOURCE.slice(at, end);
      deps.assert.match(
        body,
        /panelForSender\(sender|windowForWebContents\(sender/,
        `${name} must not use a focused/global panel`,
      );
    }
  });

  (0, deps.it)("resolves the zoom target from the dashboard view, not the focused webContents", () => {
    const zoom = deps.SOURCE.match(
      /function zoomMenuItem\(apply\) \{([\s\S]*?)\n  \}/,
    );
    deps.assert.ok(zoom, "zoomMenuItem missing");
    deps.assert.match(
      zoom[1],
      /focusedDashboardWebContents\(\)/,
      "zoom must resolve the dashboard view like the sibling reload/devtools handlers",
    );
    deps.assert.doesNotMatch(
      zoom[1],
      /webContents\.getFocusedWebContents\(\)/,
      "getFocusedWebContents() returns null under BaseWindow+contentView, so zoom would silently no-op",
    );
  });
});
}

module.exports = { register };
