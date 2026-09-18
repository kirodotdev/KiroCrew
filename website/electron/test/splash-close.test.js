"use strict";
//
// The un-closable splash (#11859). loading.html is painted into the MAIN
// window's own webContents on every liveness reconnect, and the page had no
// exit of its own. On macOS the window can have no reachable close control at
// that moment: native fullscreen hides the traffic lights, and the dashboard's
// focus mode hides them in windowed mode too -- its "restore on unmount" effect
// never runs, because loadFile destroys the document instead of unmounting
// React. A remote gateway that never comes back (lid closed, Mac woken offline)
// therefore parked the user on a page they could not leave.
//
// The fix gives the splash its own close control, routed through the same
// window-control IPC the Linux caption buttons use, and admits exactly `close`
// from exactly that page (file: loading.html) off Linux. These tests pin both
// halves.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { createWindowLifecycle } = require("../window-lifecycle");
const { fileShellPageBasename } = require("../splash-history");

const LOADING_HTML = fs.readFileSync(path.join(__dirname, "..", "loading.html"), "utf8");
const PRELOAD_JS = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
const WINDOW_LIFECYCLE_JS = fs.readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");

// The wording the page shows when it is not the main window (and when it does
// not know): closing a connection window destroys it and its connect loop, and
// the way back is the tray menu's "New Connection Window…" item. The button
// label carries the outcome too, so the reader does not depend on the small
// hint to learn that the click gives up on the reconnect.
const CONNECTION_WINDOW_LABEL = "Close window and stop reconnecting";
const CONNECTION_WINDOW_HINT =
  "This connection window closes and stops trying to connect. Kiro Crew itself keeps running; to connect again, pick New Connection Window… from the Kiro Crew icon's menu.";

// The main window as handleWindowControl sees it: a BaseWindow whose _mcView
// hosts the sender webContents. Records the caption actions applied to it.
function fakeWindow(sender) {
  const calls = [];
  const win = {
    calls,
    _mcView: { webContents: sender },
    isDestroyed: () => false,
    isMaximized: () => false,
    close: () => calls.push("close"),
    minimize: () => calls.push("minimize"),
    maximize: () => calls.push("maximize"),
    unmaximize: () => calls.push("unmaximize"),
  };
  return win;
}

function fakeSender(currentUrl, frameUrl = currentUrl) {
  const mainFrame = { url: frameUrl };
  return { getURL: () => currentUrl, mainFrame };
}

function lifecycleFor({ platform, sender, frameless = false }) {
  const win = fakeWindow(sender);
  const lifecycle = createWindowLifecycle({
    electron: { BaseWindow: { getAllWindows: () => [win] } },
    // linuxFrameless is the operator override decideLinuxFrame() honours first,
    // so it pins the frameless branch without depending on the host desktop.
    store: { get: (key) => (key === "linuxFrameless" ? frameless : null) },
    backendUrl: "http://localhost:5476",
    port: 5476,
    fetchLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    platform,
    env: {},
  });
  return { win, lifecycle };
}

const SPLASH = "file:///Applications/Kiro%20Crew.app/Contents/Resources/loading.html?accent=%23a259ff";
const TOKEN_PROMPT = "file:///Applications/Kiro%20Crew.app/Contents/Resources/token-prompt.html?port=5476";
const DASHBOARD = "http://localhost:5476/chat";

describe("window-control admission for the splash", () => {
  for (const platform of ["darwin", "win32"]) {
    it(`${platform}: close from loading.html reaches win.close()`, () => {
      const sender = fakeSender(SPLASH);
      const { win, lifecycle } = lifecycleFor({ platform, sender });
      lifecycle.chrome.windowControl(sender, "close", sender.mainFrame);
      assert.deepEqual(win.calls, ["close"]);
    });
  }

  // The token prompt is a transient shell page for history pruning
  // (splash-history.js), but it carries no close control and sends nothing on
  // this channel, so it gets no admission on it. The admission names its one
  // page by literal, not by reuse of the pruning set.
  it("the token prompt is not admitted: it sends nothing on this channel", () => {
    const sender = fakeSender(TOKEN_PROMPT);
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", sender.mainFrame);
    assert.deepEqual(win.calls, []);
  });

  // One parse, one page name. The admission reads the sending frame's page
  // through splash-history's fileShellPageBasename (the same helper the
  // history pruning uses) and compares it with exactly one literal.
  it("the admission is the shared file-page helper compared with the loading.html literal", () => {
    assert.match(
      WINDOW_LIFECYCLE_JS,
      /fileShellPageBasename\(sendingMainFrameUrl\(sender, senderFrame\)\) !== "loading\.html"/,
    );
    assert.equal((WINDOW_LIFECYCLE_JS.match(/"loading\.html"/g) || []).length, 1, "exactly one admitted page literal");
    assert.doesNotMatch(WINDOW_LIFECYCLE_JS, /CLOSE_ADMITTED|isCloseAdmittedShellPage/);
    // The helper, on the URL shapes the admission must tell apart.
    assert.equal(fileShellPageBasename(SPLASH), "loading.html", "the splash with its query string");
    assert.equal(fileShellPageBasename("file:///opt/kirocrew/loading.html"), "loading.html");
    assert.equal(fileShellPageBasename(TOKEN_PROMPT), "token-prompt.html");
    assert.equal(fileShellPageBasename(DASHBOARD), "");
    assert.equal(fileShellPageBasename("http://localhost:5476/loading.html"), "");
    assert.equal(fileShellPageBasename("file:///x/index.html?next=loading.html"), "index.html");
    assert.equal(fileShellPageBasename(""), "");
    assert.equal(fileShellPageBasename(undefined), "");
  });

  // The page that carries the close control is the page that is admitted, and
  // no other shell page sends on this channel.
  it("loading.html is the only shell page that sends windowControl", () => {
    const shellDir = path.join(__dirname, "..");
    const senders = fs
      .readdirSync(shellDir)
      .filter((f) => f.endsWith(".html"))
      .filter((f) => /windowControl\(/.test(fs.readFileSync(path.join(shellDir, f), "utf8")));
    assert.deepEqual(senders, ["loading.html"]);
  });

  // The admission is `close` only. The splash has no business minimizing or
  // maximizing, and a wider vocabulary is what the Linux branch is for.
  it("only close is admitted from the splash off Linux", () => {
    const sender = fakeSender(SPLASH);
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "minimize", sender.mainFrame);
    lifecycle.chrome.windowControl(sender, "maximize-toggle", sender.mainFrame);
    lifecycle.chrome.windowControl(sender, "", sender.mainFrame);
    assert.deepEqual(win.calls, []);
  });

  // The dashboard is always http(s) and has native controls; it must not gain
  // the ability to close its own window through IPC as a side effect.
  it("the dashboard document cannot close the window through this channel", () => {
    const sender = fakeSender(DASHBOARD);
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", sender.mainFrame);
    assert.deepEqual(win.calls, []);
  });

  // Pin the immutable source URL: WebContents.getURL() may already describe the
  // splash by the time an earlier dashboard IPC is handled.
  it("a captured dashboard frame stays refused after the current URL becomes the splash", () => {
    const sender = fakeSender(SPLASH, DASHBOARD);
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", sender.mainFrame);
    assert.deepEqual(win.calls, []);
  });

  // An http URL that merely names loading.html is the dashboard, not the splash.
  it("an http URL mentioning loading.html is still the dashboard", () => {
    const sender = fakeSender("http://localhost:5476/loading.html");
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", sender.mainFrame);
    assert.deepEqual(win.calls, []);
  });

  it("a missing captured frame is refused (fail closed)", () => {
    const sender = fakeSender(SPLASH);
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", undefined);
    assert.deepEqual(win.calls, []);
  });

  it("an unreadable captured frame URL is refused (fail closed)", () => {
    const sender = fakeSender(SPLASH);
    const unreadableFrame = {};
    Object.defineProperty(unreadableFrame, "url", {
      get: () => { throw new Error("destroyed"); },
    });
    sender.mainFrame = unreadableFrame;
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", unreadableFrame);
    assert.deepEqual(win.calls, []);
  });

  it("a child frame cannot use the splash admission", () => {
    const sender = fakeSender(DASHBOARD);
    const childFrame = { url: SPLASH };
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(sender, "close", childFrame);
    assert.deepEqual(win.calls, []);
  });

  it("a sender that is not this window's view is ignored", () => {
    const sender = fakeSender(SPLASH);
    const stranger = fakeSender(SPLASH);
    const { win, lifecycle } = lifecycleFor({ platform: "darwin", sender });
    lifecycle.chrome.windowControl(stranger, "close", stranger.mainFrame);
    assert.deepEqual(win.calls, []);
  });

  // Regression guard for the branch this admission was added next to: the
  // frameless Linux window keeps its full caption vocabulary from any page.
  it("Linux frameless keeps the whole caption vocabulary from the dashboard", () => {
    const sender = fakeSender(DASHBOARD);
    const { win, lifecycle } = lifecycleFor({ platform: "linux", sender, frameless: true });
    lifecycle.chrome.windowControl(sender, "minimize");
    lifecycle.chrome.windowControl(sender, "maximize-toggle");
    lifecycle.chrome.windowControl(sender, "close");
    assert.deepEqual(win.calls, ["minimize", "maximize", "close"]);
  });

  it("Linux with a native frame refuses the dashboard like every other platform", () => {
    const sender = fakeSender(DASHBOARD);
    const { win, lifecycle } = lifecycleFor({ platform: "linux", sender, frameless: false });
    lifecycle.chrome.windowControl(sender, "close", sender.mainFrame);
    assert.deepEqual(win.calls, []);
  });
});

describe("loading.html carries its own close control", () => {
  it("has an accessible close button that is excluded from the drag region", () => {
    assert.match(
      LOADING_HTML,
      /<button id="closeWin" type="button" aria-describedby="closeHint" aria-label="Close window and stop reconnecting\. [^"]+">Close window and stop reconnecting<\/button>/,
      "the splash must carry a labelled close button",
    );
    // The whole body is a drag surface; a button inside it must opt out or a
    // press on it would start a window drag instead of a click.
    const rule = LOADING_HTML.match(/#closeWin\s*\{([^}]*)\}/);
    assert.ok(rule, "#closeWin must have a style rule");
    assert.match(rule[1], /-webkit-app-region:\s*no-drag/);
    // The connection-window label is the long one; in a narrow window it must
    // wrap inside the pill rather than clip.
    assert.match(rule[1], /white-space:\s*normal/);
  });

  // The button sits beside a live status line ("Connection lost — waiting…"),
  // where "Close window" reads as "give up on the reconnect". The copy has to
  // say what the click does to this window and that the app survives it. The
  // static markup carries the connection-window wording (the fail-safe when
  // no window marker arrives); the script rewrites it for the main window.
  // The per-window, per-platform sentences are pinned below.
  it("tells the user the app keeps running and what closing does to this window", () => {
    const label = LOADING_HTML.match(/<button id="closeWin"[^>]*aria-label="([^"]+)"/);
    assert.ok(label, "the close button must carry an aria-label");
    assert.match(label[1], /keeps running/, "the aria-label must say the app survives the click");
    const hint = LOADING_HTML.match(/<p id="closeHint">([^<]+)<\/p>/);
    assert.ok(hint, "the close button must have a visible hint beside it");
    assert.equal(hint[1], CONNECTION_WINDOW_HINT, "the static hint is the connection-window wording");
    assert.equal(label[1], `${CONNECTION_WINDOW_LABEL}. ${CONNECTION_WINDOW_HINT}`);
  });

  // The ink colour is computed against the accent, and the foreground ghosts
  // drift under the bottom-centre. White text on a white ghost body is
  // unreadable, so the block must paint its own accent-derived surface.
  it("paints an accent surface behind the exit block so the hint stays legible over ghosts", () => {
    const rule = LOADING_HTML.match(/#exit\s*\{([^}]*)\}/);
    assert.ok(rule, "#exit must have a style rule");
    assert.match(
      rule[1],
      /background:\s*color-mix\(in srgb, var\(--accent\)/,
      "#exit must declare a background derived from the accent token",
    );
  });

  // The status line under the close button stays as the base splash draws it:
  // bare text on the backdrop, no surface of its own. The accent surface
  // belongs to the exit block alone, and only #closeWin is the bordered pill,
  // so nothing else at the bottom-centre reads as a second control.
  it("the status line declares no surface; only the exit block and the button do", () => {
    const status = LOADING_HTML.match(/#status\s*\{([^}]*)\}/);
    assert.ok(status, "#status must have a style rule");
    assert.doesNotMatch(status[1], /background/, "#status paints no background of its own");
    assert.doesNotMatch(status[1], /border-radius/, "#status has no corners to round");
    assert.doesNotMatch(status[1], /(^|[^-])border:/, "#status must not draw a border");

    const button = LOADING_HTML.match(/#closeWin\s*\{([^}]*)\}/);
    assert.ok(button, "#closeWin must have a style rule");
    assert.match(button[1], /border-radius:\s*999px/, "#closeWin is the pill");
    assert.match(button[1], /border:\s*1px solid/, "#closeWin is the bordered element");
  });

  it("routes the click through the window-control bridge with the close action", () => {
    assert.match(LOADING_HTML, /bridge\.windowControl\("close"\)/);
    assert.match(
      PRELOAD_JS,
      /windowControl: \(action\) => ipcRenderer\.send\("window-control"/,
      "the preload must still expose the channel the splash sends on",
    );
  });

  // Linux frameless injects real caption buttons onto this page
  // (window-lifecycle.js did-finish-load); a second close control there would
  // be a duplicate. A browser preview has no window to close at all.
  it("stays hidden where captions are injected or no bridge exists", () => {
    assert.match(
      LOADING_HTML,
      /bridge && typeof bridge\.windowControl === "function" && !bridge\.linuxFrameless/,
    );
  });

  // Not Escape: PR #1876 removed an Escape-to-close handler from the token
  // prompt after review found BaseWindow.close() misbehaving on that path.
  it("does not bind Escape to close", () => {
    assert.doesNotMatch(LOADING_HTML, /Escape/);
  });
});

// When the control appears. A reconnect paints this page because the gateway
// went away, so the exit shows at once. A cold boot shows it only after a real
// stall: a healthy start replaces the page in about two seconds, and a merely
// slow one must not offer a button that reads as "cancel startup". The signal
// is a loadFile query rather than a status-string match (UI copy is brittle)
// or an IPC send (the splash loads asynchronously and can miss one).
describe("loading.html reveals the exit on reconnect, and on a cold boot only after a stall", () => {
  const SUPERVISOR_JS = fs.readFileSync(path.join(__dirname, "..", "gateway-supervisor.js"), "utf8");
  const fnBody = (name) => {
    const m = SUPERVISOR_JS.match(
      new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n  \\}`),
    );
    assert.ok(m, `gateway-supervisor.js must define function ${name}`);
    return m[0];
  };

  it("reads the reconnect marker the supervisor sends", () => {
    assert.match(SUPERVISOR_JS, /const SPLASH_RECONNECT_QUERY = Object\.freeze\(\{ reconnect: "1" \}\);/);
    assert.match(LOADING_HTML, /var splashQuery = new URLSearchParams\(location\.search\);\s*reconnect = splashQuery\.get\("reconnect"\) === "1";/);
  });

  it("reveals at once on a reconnect and only after the stall threshold otherwise", () => {
    assert.match(LOADING_HTML, /if \(reconnect\) revealExit\(\);\s*\n\s*else setTimeout\(revealExit, COLD_BOOT_STALL_MS\);/);
  });

  // The fail-safe: with no marker at all the exit still arrives, but not on a
  // boot that is merely slow. 15 s is well past a healthy start and inside
  // the adopted-gateway recovery wait, so a stuck boot is never left trapped.
  it("uses a 15 second cold-boot stall threshold", () => {
    assert.match(LOADING_HTML, /var COLD_BOOT_STALL_MS = 15000;/);
  });

  it("retires the exit once the gateway signals ready", () => {
    const markReady = LOADING_HTML.match(/function markReady\(\) \{[\s\S]*?\n    \}/);
    assert.ok(markReady, "loading.html must define markReady");
    assert.match(markReady[0], /exit\.hidden = true/);
  });

  for (const name of ["reconnectExternalGateway", "reconnectOrRespawnAdoptedGateway"]) {
    it(`${name} paints the splash with the reconnect marker`, () => {
      assert.match(
        fnBody(name),
        /loadFile\(path\.join\(dirname, "loading\.html"\), \{ query: splashQuery\(window, \{ reconnect: true \}\) \}\)/,
      );
    });
  }

  // showLoadingThenConnect serves both the cold boot and every reconnect
  // hand-off; the marker must follow its `reconnect` option, not be constant.
  it("showLoadingThenConnect carries the marker only when reconnecting", () => {
    const body = fnBody("showLoadingThenConnect");
    assert.match(
      body,
      /loadFile\(path\.join\(dirname, "loading\.html"\), \{\s*query: splashQuery\(window, \{ reconnect, accent: currentThemeAccent\(\) \}\),\s*\}\)/,
    );
  });

  it("relaunchViaConfirmedSuccessor marks its main-window splash without a reconnect marker", () => {
    const body = fnBody("relaunchViaConfirmedSuccessor");
    assert.match(
      body,
      /loadFile\(path\.join\(dirname, "loading\.html"\), \{\s*query: splashQuery\(window, \{ accent: currentThemeAccent\(\) \}\),\s*\}\)/,
    );
    assert.doesNotMatch(body, /query:\s*\{\s*accent:\s*currentThemeAccent\(\)/);
  });
});

// Which window the splash is in decides what "Close window" does. Only the
// main window's close handler hides to tray and leaves the connect loop
// running (window-lifecycle.js mainWindow.on("close")); a connection window
// (createConnectionWindow) has no such handler, so close destroys it and
// aborts its connection attempt. The supervisor stamps `primary=1` on the
// main window's splash and the page words its hint from that flag.
describe("the supervisor marks the main window's splash as primary", () => {
  const SUPERVISOR_JS = fs.readFileSync(path.join(__dirname, "..", "gateway-supervisor.js"), "utf8");

  // splashQuery evaluated as written, with mainWindow() and the two query
  // constants supplied, so the assertions are about its behaviour.
  function splashQueryFn(mainWin) {
    const m = SUPERVISOR_JS.match(/function splashQuery\(window[\s\S]*?\n  \}/);
    assert.ok(m, "gateway-supervisor.js must define splashQuery");
    const context = {
      mainWindow: () => mainWin,
      SPLASH_RECONNECT_QUERY: Object.freeze({ reconnect: "1" }),
      SPLASH_PRIMARY_QUERY: Object.freeze({ primary: "1" }),
    };
    vm.runInNewContext(`${m[0]}; this.splashQuery = splashQuery;`, context);
    // The vm realm has its own Object.prototype; copy into this realm so
    // deepEqual compares the entries, not the prototype identity.
    return (...args) => ({ ...context.splashQuery(...args) });
  }

  it("defines primary=1 as the marker the page reads", () => {
    assert.match(SUPERVISOR_JS, /const SPLASH_PRIMARY_QUERY = Object\.freeze\(\{ primary: "1" \}\);/);
    assert.match(LOADING_HTML, /primary = splashQuery\.get\("primary"\) === "1";/);
  });

  it("stamps primary on the main window and never on a connection window", () => {
    const main = { id: "main" };
    const conn = { id: "conn" };
    const splashQuery = splashQueryFn(main);
    assert.deepEqual(splashQuery(main, { reconnect: true }), { reconnect: "1", primary: "1" });
    assert.deepEqual(splashQuery(conn, { reconnect: true }), { reconnect: "1" });
    assert.deepEqual(
      splashQuery(main, { reconnect: false, accent: "#a259ff" }),
      { accent: "#a259ff", primary: "1" },
    );
    assert.deepEqual(splashQuery(conn, { accent: "#a259ff" }), { accent: "#a259ff" });
  });

  // No main window yet (or already gone): nothing may claim to be it.
  it("stamps nothing as primary when there is no main window", () => {
    const splashQuery = splashQueryFn(null);
    assert.deepEqual(splashQuery({ id: "w" }, { reconnect: true }), { reconnect: "1" });
  });
});

describe("loading.html words the close hint for the window and platform it is in", () => {
  // closeHintFor, its TRAY_LOCATION table and closeLabelFor, evaluated as written.
  const block = LOADING_HTML.match(/var TRAY_LOCATION = \{[\s\S]*?\};\s*function closeHintFor\([\s\S]*?\n    \}\s*function closeLabelFor\([\s\S]*?\n    \}/);
  assert.ok(block, "loading.html must define TRAY_LOCATION, closeHintFor and closeLabelFor");
  const context = {};
  vm.runInNewContext(`${block[0]}; this.closeHintFor = closeHintFor; this.closeLabelFor = closeLabelFor;`, context);
  const { closeHintFor, closeLabelFor } = context;

  // Finding: the old main-window sentence spent two sentences before the
  // payoff. It now leads with what the click does and puts the reassurance
  // in the first clause, then names the way back.
  const MAIN_PREFIX = "This window closes; Kiro Crew keeps running and keeps trying to connect in the background. Bring the window back from ";

  it("main window: leads with what happens and the reassurance in the first clause", () => {
    for (const platform of ["darwin", "win32", "linux"]) {
      const hint = closeHintFor(true, platform);
      assert.match(hint, /^This window closes; Kiro Crew keeps running/);
      // The way back comes after the reassurance, never before it.
      assert.ok(hint.indexOf("keeps running") < hint.indexOf("Bring the window back"));
    }
  });

  // Finding: the hint was the only reason the reader dared to click, and
  // "the tray icon" told them nothing about where to look. Each platform
  // names the place its icon actually lives (createTray in
  // window-lifecycle.js: a menu-bar item on macOS, a notification-area icon
  // on Windows, a system-tray icon elsewhere). It names the icon, not the
  // tray menu's "Show …" label, which follows app.name (nightly differs).
  it("main window on macOS: names the menu bar at the top-right", () => {
    const hint = closeHintFor(true, "darwin");
    assert.equal(hint, `${MAIN_PREFIX}the Kiro Crew icon in the menu bar, at the top-right of the screen.`);
  });

  it("main window on Windows: names the notification area by the clock and the overflow chevron", () => {
    const hint = closeHintFor(true, "win32");
    assert.equal(
      hint,
      `${MAIN_PREFIX}the Kiro Crew icon in the notification area next to the clock (it may sit behind the ^ chevron).`,
    );
  });

  it("main window on Linux (native frame) and on an unknown platform: names the system tray", () => {
    assert.equal(closeHintFor(true, "linux"), `${MAIN_PREFIX}the Kiro Crew icon in the system tray.`);
    assert.equal(closeHintFor(true, undefined), `${MAIN_PREFIX}the Kiro Crew icon in the system tray.`);
  });

  it("does not quote a tray menu label, which follows app.name", () => {
    for (const platform of ["darwin", "win32", "linux"]) {
      assert.doesNotMatch(closeHintFor(true, platform), /Show /);
    }
  });

  // Finding: in a connection window "keeps trying to connect" and "bring the
  // window back" are false, because close destroys that window and its
  // connect loop. The same wording serves the unknown case, since it promises
  // nothing false for either window.
  // Finding: ending at "stops trying to connect" left the reader unsure how
  // to get connected again. The sentence now names the way back: the tray
  // menu's "New Connection Window…" item (createTray in window-lifecycle.js,
  // built on every platform) — by item name, so it does not depend on which
  // platform shows an application menu bar.
  it("connection window (and unknown): says this window closes and its connect attempt stops, on every platform", () => {
    for (const platform of ["darwin", "win32", "linux", undefined]) {
      const hint = closeHintFor(false, platform);
      assert.equal(hint, CONNECTION_WINDOW_HINT);
      assert.doesNotMatch(hint, /trying to connect\. Bring|back from|tray/);
      assert.match(hint, /New Connection Window…/, "the hint must name the way to open another connection window");
      assert.ok(
        hint.indexOf("stops trying to connect") < hint.indexOf("New Connection Window…"),
        "the way back must come after the consequence it answers",
      );
    }
  });

  it("the two wordings differ on the claims that separate the windows", () => {
    const main = closeHintFor(true, "darwin");
    const conn = closeHintFor(false, "darwin");
    assert.match(main, /keeps trying to connect/);
    assert.match(main, /Bring the window back/);
    assert.doesNotMatch(conn, /keeps trying to connect/);
    assert.match(conn, /stops trying to connect/);
  });

  // The outcome that decides the click lives in the button label, not only in
  // the 12px hint under it. The main window's close hides to tray and keeps
  // reconnecting, so "Close window" is the whole truth there; a connection
  // window's close ends its reconnect, and its label says so. The unknown
  // case takes the connection-window label, as it takes that hint.
  it("the button label carries the outcome: the connection window says it stops reconnecting", () => {
    assert.equal(closeLabelFor(true), "Close window");
    assert.equal(closeLabelFor(false), CONNECTION_WINDOW_LABEL);
    assert.equal(closeLabelFor(undefined), CONNECTION_WINDOW_LABEL);
    // The label and the hint make the same claim for the same window.
    assert.match(closeHintFor(false, "darwin"), /stops trying to connect/);
    assert.doesNotMatch(closeHintFor(true, "darwin"), /stops trying to connect/);
  });

  // The page must apply the sentences to what the user sees and hears: the
  // button text, the visible hint, and the button's aria-label (which repeats
  // the visible label so the two agree), from the bridge's platform and the
  // primary marker. Fail-safe: primary starts false, so a splash with no
  // marker keeps the connection-window wording.
  it("renders the chosen label and sentence into the button, the hint and the aria-label", () => {
    assert.match(LOADING_HTML, /var primary = false;/);
    assert.match(LOADING_HTML, /var label = closeLabelFor\(primary\);/);
    assert.match(LOADING_HTML, /var hint = closeHintFor\(primary, bridge\.platform\);/);
    assert.match(LOADING_HTML, /closeWin\.textContent = label;/);
    assert.match(LOADING_HTML, /closeHint\.textContent = hint;/);
    assert.match(LOADING_HTML, /closeWin\.setAttribute\("aria-label", label \+ "\. " \+ hint\);/);
    assert.match(PRELOAD_JS, /platform: process\.platform,/, "the preload must expose the platform the page reads");
  });
});
