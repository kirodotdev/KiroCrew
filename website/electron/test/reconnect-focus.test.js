// Regression tests for #6373: the desktop app stole window focus on every
// gateway reconnect. The liveness-recovery paths (remote-tunnel reconnect,
// adopted-gateway recovery, wedged-gateway respawn) called win.show()
// unconditionally, and on macOS BrowserWindow.show() raises AND focuses — so
// every screen lock/unlock (which drops an SSH tunnel) yanked the app over
// whatever the user was working in.
//
// The rule under test:
//   - cold launch / user-initiated connects keep the historical raise (show()),
//   - liveness reconnects touch NOTHING while self-healing — no raise, no
//     focus, no un-minimize, no re-surfacing of a window the user hid to tray
//     (the splash loads fine into a background/hidden window),
//   - ESCALATION: any state that genuinely needs the user (token prompt,
//     failure dialog, terminal unrecoverable-gateway dialog) does a FULL
//     reveal at the point it is reached, on any path — an invisible prompt
//     would park recovery forever.
// revealWindowForConnect (gateway-recovery.js) carries the silent-vs-raise
// decision; source-level pins verify main.js wires every liveness path through
// it and keeps the escalation reveals reconnect-guarded.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { revealWindowForConnect } = require("../gateway-recovery");

function fakeWin() {
  const calls = [];
  return {
    calls,
    show: () => calls.push("show"),
    showInactive: () => calls.push("showInactive"),
    focus: () => calls.push("focus"),
    restore: () => calls.push("restore"),
  };
}

describe("revealWindowForConnect", () => {
  // Cold launch and user-initiated connects (app boot, dialog Retry, a new
  // connection window) must keep raising exactly as before the fix.
  it("raises via show() when not a reconnect (explicit false)", () => {
    const win = fakeWin();
    revealWindowForConnect(win, { reconnect: false });
    assert.deepEqual(win.calls, ["show"]);
  });

  it("raises via show() when opts are omitted (default is the cold path)", () => {
    const win = fakeWin();
    revealWindowForConnect(win);
    assert.deepEqual(win.calls, ["show"]);
  });

  // The reported bug, closed as a class: a liveness reconnect must not touch
  // window state AT ALL — no show/showInactive/focus/restore. This also pins
  // that a tray-hidden window stays hidden and a minimized one stays minimized
  // (revealing "inactively" would still re-surface a deliberately hidden
  // window on every screen lock/unlock).
  it("touches nothing on a reconnect", () => {
    const win = fakeWin();
    revealWindowForConnect(win, { reconnect: true });
    assert.deepEqual(win.calls, []);
  });
});

describe("main.js wiring (source pins)", () => {
  const MAIN_JS = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");

  // Extract a top-level function body: from its declaration to the first
  // closing brace at column 0 (same technique as hide-to-tray.test.js).
  const fnBody = (name) => {
    const m = MAIN_JS.match(new RegExp(`async function ${name}\\([\\s\\S]*?\\n\\}`));
    assert.ok(m, `main.js must define async function ${name}`);
    return m[0];
  };

  // Assert a function body performs no window reveal/raise of any kind —
  // including through the escalation helper (the self-healing stretch of a
  // liveness path must stay fully silent; escalation belongs to the
  // needs-user states inside showLoadingThenConnect / the terminal dialog).
  const assertNoReveal = (body, name) => {
    for (const call of ["show()", "showInactive()", "focus()", "restore()"]) {
      assert.ok(
        !body.includes(`win.${call}`),
        `${name} must not call win.${call} on a liveness path`,
      );
    }
    assert.ok(
      !body.includes("revealForUserDecision("),
      `${name} must not invoke the escalation reveal while self-healing`,
    );
  };

  it("imports revealWindowForConnect from gateway-recovery", () => {
    assert.match(MAIN_JS, /revealWindowForConnect,[\s\S]*?\} = require\("\.\/gateway-recovery"\)/);
  });

  it("reconnectExternalGateway performs no reveal at all", () => {
    assertNoReveal(fnBody("reconnectExternalGateway"), "reconnectExternalGateway");
  });

  it("reconnectOrRespawnAdoptedGateway performs no reveal at all", () => {
    assertNoReveal(fnBody("reconnectOrRespawnAdoptedGateway"), "reconnectOrRespawnAdoptedGateway");
  });

  it("showLoadingThenConnect routes its initial reveal through the helper", () => {
    const body = fnBody("showLoadingThenConnect");
    assert.match(body, /async function showLoadingThenConnect\(win, backendUrl = BACKEND_URL, \{ reconnect = false \} = \{\}\)/);
    assert.match(body, /revealWindowForConnect\(win, \{ reconnect \}\)/);
  });

  // ESCALATION: inside showLoadingThenConnect the needs-user states (token
  // prompt, failure dialog) must do a FULL reveal — reconnect-guarded — via
  // revealForUserDecision. No bare win.show() may remain (it would skip the
  // deferred-tray-hide cancel and the un-minimize). The token-prompt reveal
  // must precede exitImmersiveModes: leaving fullscreen fires a deferred
  // tray-hide's listener, so the cancel inside the reveal has to come first.
  // ESCALATION: inside showLoadingThenConnect the needs-user states (token
  // prompt, failure dialog) must do a FULL reveal — unconditional, because a
  // window can be hidden on any path by the time input is needed, and the
  // reveal is idempotent when visible. No bare win.show() may remain (it
  // would skip the deferred-tray-hide cancel and the un-minimize). The
  // token-prompt reveal must precede exitImmersiveModes: leaving fullscreen
  // fires a deferred tray-hide's listener, so the cancel has to come first.
  it("showLoadingThenConnect escalates needs-user states via the full reveal", () => {
    const body = fnBody("showLoadingThenConnect");
    assert.ok(!body.includes("win.show()"), "no bare win.show() inside showLoadingThenConnect");
    const REVEAL = "revealForUserDecision(win);";
    // One reveal in the token-prompt branch, BEFORE exitImmersiveModes.
    const immersive = body.indexOf("exitImmersiveModes(win);");
    const tokenReveal = body.indexOf(REVEAL);
    assert.ok(tokenReveal !== -1 && immersive !== -1 && tokenReveal < immersive,
      "token-prompt reveal must exist and run before exitImmersiveModes");
    // And one in the failure-dialog branch: inside the catch, before the modal
    // actually opens (match the awaited call, not comment mentions of it).
    const catchStart = body.indexOf("} catch (err) {");
    const dialog = body.indexOf("await showGatewayErrorDialog(", catchStart);
    const dialogReveal = body.indexOf(REVEAL, catchStart);
    assert.ok(catchStart !== -1 && dialogReveal !== -1 && dialog !== -1 && dialogReveal < dialog,
      "failure-dialog reveal must sit in the catch branch, before the modal opens");
  });

  // The escalation reveal must follow the repo's full show idiom
  // (hide-to-tray.js contract): cancel the deferred hide, un-minimize, show,
  // focus, and steal macOS app activation (a background app's window rises
  // without keyboard focus otherwise — same as the global-hotkey summon).
  it("revealForUserDecision performs the full reveal idiom", () => {
    const m = MAIN_JS.match(/function revealForUserDecision\(win\) \{[\s\S]*?\n\}/);
    assert.ok(m, "main.js must define revealForUserDecision");
    const body = m[0];
    const order = [
      "cancelPendingTrayHide(win)",
      "if (win.isMinimized()) win.restore()",
      "win.show()",
      "win.focus()",
      "if (IS_MAC) app.focus({ steal: true })",
    ].map((s) => body.indexOf(s));
    assert.ok(order.every((i) => i !== -1), `full reveal idiom missing a step: ${body}`);
    assert.deepEqual([...order].sort((a, b) => a - b), order, "reveal steps out of order");
    assert.match(body, /isQuitting\) return;/, "reveal must bail during app quit");
  });

  // Terminal escalation: showUnrecoverableGatewayError is called DIRECTLY from
  // liveness paths (not through showLoadingThenConnect), presents a modal
  // child of `win`, and every branch needs a human decision. It must fully
  // reveal its parent first — a modal under a tray-hidden parent parks
  // invisibly, and every button on that dialog quits the app.
  it("showUnrecoverableGatewayError reveals its parent before the modal", () => {
    const body = fnBody("showUnrecoverableGatewayError");
    const reveal = body.indexOf("revealForUserDecision(win)");
    const dialog = body.indexOf("showGatewayErrorDialog");
    assert.ok(reveal !== -1, "terminal error dialog must reveal its parent window");
    assert.ok(dialog !== -1 && reveal < dialog, "reveal must happen before the modal opens");
  });

  // Every liveness-recovery re-entry into the boot flow must be flagged as a
  // reconnect: an unflagged own-port call site would raise+focus on a
  // background reconnect. All liveness paths call with the BACKEND_URL
  // literal, so any such call missing the flag fails here.
  it("every liveness recovery path re-enters the boot flow with reconnect: true", () => {
    const unflagged = MAIN_JS.match(/showLoadingThenConnect\(win, BACKEND_URL\)/g) || [];
    assert.deepEqual(unflagged, [], "own-port showLoadingThenConnect call sites must pass a reconnect flag");
    const flagged = MAIN_JS.match(/showLoadingThenConnect\(win, BACKEND_URL, \{ reconnect: true \}\)/g) || [];
    assert.ok(flagged.length >= 4, `expected the liveness call sites to stay flagged, saw ${flagged.length}`);
  });

  // recoverWedgedGateway has two callers: the liveness monitor (silent) and
  // the failed-update-install path, where the user clicked Install and is
  // watching — that one must keep the raise. The flag is threaded through to
  // the boot-flow re-entry, and the install path must actually pass it.
  it("recoverWedgedGateway raises for user-initiated recovery, stays silent for liveness", () => {
    const body = fnBody("recoverWedgedGateway");
    assert.match(body, /async function recoverWedgedGateway\(win, \{ userInitiated = false \} = \{\}\)/);
    assert.match(body, /showLoadingThenConnect\(win, BACKEND_URL, \{ reconnect: !userInitiated \}\)/);
    assert.match(MAIN_JS, /recoverWedgedGateway\(mainWindow, \{ userInitiated: true \}\)/);
  });

  // The cold-boot path must NOT be flagged — initial launch keeps its raise.
  it("cold launch still raises (boot call carries no reconnect flag)", () => {
    assert.match(MAIN_JS, /await showLoadingThenConnect\(win\);/);
  });
});
