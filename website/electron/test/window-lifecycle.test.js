"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "window-lifecycle.js");
// Normalize to LF regardless of the checkout's line-ending translation: the
// source-scanning regexes below anchor on a literal "\n", and a Windows
// checkout with core.autocrlf on disk-translates the file to CRLF, which
// shifts every "}\n" anchor to "}\r\n" and fails the match on a file that is
// otherwise unchanged.
const SOURCE = fs.readFileSync(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");
const {
  BROWSER_PARTITION,
  createWindowLifecycle,
} = require("../window-lifecycle");
const { registerCaptureSurface } = require("../capture-trust");

function validOptions(overrides = {}) {
  return {
    electron: {},
    store: { get: () => null },
    backendUrl: "http://localhost:5476",
    port: 5476,
    fetchLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    // Keep construction independent of the host running the suite.
    platform: "test",
    ...overrides,
  };
}

describe("window lifecycle module boundary", () => {
  it("loads in plain Node and never requires Electron at module scope", () => {
    assert.doesNotMatch(
      SOURCE,
      /require\(\s*["']electron["']\s*\)/,
      "Electron must come from the factory argument so node:test can load this module",
    );
    assert.equal(typeof createWindowLifecycle, "function");
  });

  it("fails loudly for every required composition dependency", () => {
    const cases = [
      ["electron", /electron is required/],
      ["store", /store is required/],
      ["backendUrl", /backendUrl is required/],
      ["port", /port is required/],
      ["fetchLocalToken", /fetchLocalToken is required/],
      ["fetchRemoteToken", /fetchRemoteToken is required/],
      ["requestQuit", /requestQuit is required/],
      ["connectWindow", /connectWindow is required/],
    ];

    for (const [key, expected] of cases) {
      const options = validOptions();
      delete options[key];
      assert.throws(
        () => createWindowLifecycle(options),
        expected,
        `${key} must not silently degrade`,
      );
    }
    assert.doesNotThrow(() => createWindowLifecycle(validOptions()));
  });
});

const DASH_ORIGIN = "http://localhost:5476";
const PANE_ORIGIN = "http://localhost:7778";

function securityHarness() {
  const calls = {
    display: [],
    defaultRequest: [],
    defaultCheck: [],
    fromPartition: [],
    browserRequest: [],
    browserCheck: [],
    getSources: 0,
  };

  const browserSession = {
    setPermissionRequestHandler(handler) {
      calls.browserRequest.push(handler);
    },
    setPermissionCheckHandler(handler) {
      calls.browserCheck.push(handler);
    },
  };
  const defaultSession = {
    setDisplayMediaRequestHandler(handler, options) {
      calls.display.push({ handler, options });
    },
    setPermissionRequestHandler(handler) {
      calls.defaultRequest.push(handler);
    },
    setPermissionCheckHandler(handler) {
      calls.defaultCheck.push(handler);
    },
  };
  // The dashboard's capture surface, registered the way setupWindowContents
  // does, plus a pane subframe inside it. `fromFrame` is what Electron gives the
  // real handler to map a request's frame back to its contents.
  const dashboardMain = { parent: null, url: `${DASH_ORIGIN}/chat` };
  const dashboardWc = { mainFrame: dashboardMain };
  const paneFrame = { parent: dashboardMain, url: `${PANE_ORIGIN}/?token=x` };
  registerCaptureSurface(dashboardWc, DASH_ORIGIN);
  const frameOwners = new Map([
    [dashboardMain, dashboardWc],
    [paneFrame, dashboardWc],
  ]);

  const electron = {
    session: {
      defaultSession,
      fromPartition(name) {
        calls.fromPartition.push(name);
        return browserSession;
      },
    },
    webContents: { fromFrame: (frame) => frameOwners.get(frame) },
    desktopCapturer: {
      async getSources(options) {
        calls.getSources += 1;
        assert.deepEqual(options, { types: ["screen", "window"] });
        return [{ id: "screen:0", name: "Screen" }];
      },
    },
    // Not consulted on the pinned non-macOS branch.
    systemPreferences: {},
  };
  const lifecycle = createWindowLifecycle(validOptions({
    electron,
    platform: "win32",
  }));
  return { calls, lifecycle, dashboardMain, paneFrame };
}

describe("session security registration", () => {
  it("registers every default and browser-partition policy exactly once", async () => {
    const { calls, lifecycle, dashboardMain, paneFrame } = securityHarness();

    lifecycle.security.configureSession();
    lifecycle.security.configureSession();

    assert.equal(calls.display.length, 1);
    assert.deepEqual(calls.display[0].options, { useSystemPicker: true });
    assert.equal(calls.defaultRequest.length, 1);
    assert.equal(calls.defaultCheck.length, 1);
    assert.deepEqual(calls.fromPartition, [BROWSER_PARTITION]);
    assert.equal(calls.browserRequest.length, 1);
    assert.equal(calls.browserCheck.length, 1);

    // The dedicated browser partition is deny-all, independently of origin.
    let browserGranted = null;
    calls.browserRequest[0](
      { getURL: () => "http://localhost:5476" },
      "media",
      (value) => { browserGranted = value; },
    );
    assert.equal(browserGranted, false);
    assert.equal(calls.browserCheck[0](), false);

    // The default session retains the dashboard's narrow media/fullscreen grant.
    const dashboard = { getURL: () => "http://localhost:5476/chat" };
    let micGranted = null;
    calls.defaultRequest[0](
      dashboard,
      "media",
      (value) => { micGranted = value; },
      { mediaTypes: ["audio"] },
    );
    assert.equal(micGranted, true);
    assert.equal(
      calls.defaultCheck[0](null, "media", "http://localhost:5476", {
        mediaType: "audio",
      }),
      true,
    );
    assert.equal(
      calls.defaultCheck[0](dashboard, "media", "http://localhost:5476", {
        mediaType: "video",
      }),
      false,
    );

    // Screen capture is authorized by IDENTITY, asserted through the handler
    // configureSession actually registered — so the decision cannot be wired
    // into capture-trust.js and left out of the call site.
    let displayResult = null;
    await calls.display[0].handler({ frame: dashboardMain }, (result) => { displayResult = result; });
    assert.deepEqual(displayResult, {
      video: { id: "screen:0", name: "Screen" },
    });
    assert.equal(calls.getSources, 1);

    // An instances pane's subframe. Refused BEFORE desktopCapturer is asked —
    // the call count is what separates a denial from a granted stream nobody
    // read.
    let paneResult = "untouched";
    await calls.display[0].handler({ frame: paneFrame }, (result) => { paneResult = result; });
    assert.deepEqual(paneResult, {});
    assert.equal(calls.getSources, 1);

    // A pane that promoted itself: `target="_top"` replaces the dashboard's top
    // document, so the SAME main frame now hosts the pane's origin. Frame
    // position no longer separates them; the registered origin does.
    dashboardMain.url = `${PANE_ORIGIN}/hostile`;
    let promotedResult = "untouched";
    await calls.display[0].handler({ frame: dashboardMain }, (result) => { promotedResult = result; });
    assert.deepEqual(promotedResult, {});
    assert.equal(calls.getSources, 1);

    // An unregistered surface: any webContents this app did not open for its own
    // documents is refused without having to be named.
    let strangerResult = "untouched";
    await calls.display[0].handler(
      { frame: { parent: null, url: `${DASH_ORIGIN}/chat` } },
      (result) => { strangerResult = result; },
    );
    assert.deepEqual(strangerResult, {});
    assert.equal(calls.getSources, 1);
  });
});

describe("local gateway ownership policy", () => {
  it("uses the sender window's own port and rejects remote or destroyed windows", () => {
    const remoteHosts = {
      "6124": { host: "remote.example.test" },
    };
    const lifecycle = createWindowLifecycle(validOptions({
      // Deliberately differ from both tested windows: this factory port belongs
      // only to the primary window and must not influence a sender-scoped gate.
      port: 5476,
      store: {
        get(key) {
          return key === "remoteHosts" ? remoteHosts : null;
        },
      },
    }));
    const isLocal = lifecycle.security.isGatewayLocalForWindow;

    assert.equal(isLocal(null), false);
    assert.equal(isLocal({
      isDestroyed: () => true,
      _mcBackendUrl: "http://localhost:6123",
    }), false);
    assert.equal(isLocal({ isDestroyed: () => false }), false);
    assert.equal(isLocal({
      isDestroyed: () => false,
      _mcBackendUrl: "https://gateway.example.test:6123",
    }), false);
    assert.equal(isLocal({
      isDestroyed: () => false,
      _mcBackendUrl: "http://127.0.0.1:6124",
    }), false, "a configured tunnel is remote even though its URL is loopback");
    assert.equal(isLocal({
      isDestroyed: () => false,
      _mcBackendUrl: "http://localhost:6123",
    }), true, "an unconfigured loopback port is local");
  });

  it("resolves a scheme's default port before the remote-host lookup", () => {
    // `new URL("http://localhost:80").port` is "", so a lookup keyed off the raw
    // property asks for remoteHosts[""], misses, and reports a tunnelled crew as
    // a gateway on this machine -- after which the host-presence heartbeat sends
    // this machine's internal secret over that tunnel. Both scheme defaults are
    // covered, in every spelling of a loopback host the shell accepts, and in
    // both directions so the normalizer cannot be a blanket "remote".
    const remoteHosts = {
      "80": { host: "crew-http.example.test" },
      "443": { host: "crew-https.example.test" },
      "6124": { host: "crew-explicit.example.test" },
    };
    const lifecycle = createWindowLifecycle(validOptions({
      port: 5476,
      store: { get: (key) => (key === "remoteHosts" ? remoteHosts : null) },
    }));
    const isLocal = lifecycle.security.isGatewayLocalForWindow;
    const win = (url) => isLocal({ isDestroyed: () => false, _mcBackendUrl: url });

    // A crew is configured on the port each URL really names, so every one of
    // these is a tunnel and none of them is this machine.
    for (const url of [
      "http://localhost:80/",
      "http://localhost/",
      "http://127.0.0.1:80/",
      "http://127.0.0.1/",
      "http://[::1]:80/",
      "http://[::1]/",
      "http://0.0.0.0/",
      "http://pod.localhost/",
      "https://localhost:443/",
      "https://localhost/",
      "https://127.0.0.1/",
      "https://[::1]/",
      // Cross-scheme: 80 is not https's default and 443 is not http's, so the
      // raw property already carried these. They must not change.
      "https://localhost:80/",
      "http://localhost:443/",
      "http://localhost:6124/",
    ]) {
      assert.equal(win(url), false, `${url} names a configured crew, so it is remote`);
    }

    // The same normalization must not invent a crew where none is configured:
    // with the default-port entries removed, a default-port URL is local again.
    const bare = createWindowLifecycle(validOptions({
      port: 5476,
      store: { get: (key) => (key === "remoteHosts" ? { "6124": { host: "c.example.test" } } : null) },
    }));
    const bareIsLocal = bare.security.isGatewayLocalForWindow;
    for (const url of [
      "http://localhost:80/",
      "http://localhost/",
      "https://localhost:443/",
      "https://localhost/",
      "http://localhost:5476/",
    ]) {
      assert.equal(
        bareIsLocal({ isDestroyed: () => false, _mcBackendUrl: url }),
        true,
        `${url} has no configured crew, so it is this machine`,
      );
    }

    // A non-loopback host stays remote whatever its port resolves to.
    for (const url of ["http://crew.example.test/", "https://crew.example.test:443/"]) {
      assert.equal(win(url), false, `${url} is not loopback`);
    }
  });

  it("still reads a crew an older version recorded under the empty key as remote", () => {
    // An older version keyed this map off the raw `URL.port`, so an install that
    // configured its crew while on a scheme-default port persisted it under
    // `remoteHosts[""]` -- and the gate of the day read that same empty key, so
    // it answered "remote" by accident. Resolving the key without honouring that
    // record would classify the crew as local on the first launch after upgrade
    // and put this machine's internal secret through the tunnel.
    const lifecycle = createWindowLifecycle(validOptions({
      port: 80,
      store: { get: (key) => (key === "remoteHosts" ? { "": { host: "legacy.example.test" } } : null) },
    }));
    const isLocal = lifecycle.security.isGatewayLocalForWindow;
    const win = (url) => isLocal({ isDestroyed: () => false, _mcBackendUrl: url });

    // Every URL shape that could have produced that record: the port was resolved
    // rather than stated, under either scheme.
    for (const url of [
      "http://localhost/",
      "http://localhost:80/",
      "http://127.0.0.1/",
      "https://localhost/",
      "https://localhost:443/",
    ]) {
      assert.equal(win(url), false, `${url} must honour the legacy record`);
    }

    // And no other shape: a stated non-default port could not have written that
    // record, so it must not be dragged into it.
    assert.equal(win("http://localhost:5476/"), true, "a stated port is unaffected");
    assert.equal(win("http://localhost:6124/"), true, "a stated port is unaffected");

    // An entry under the empty key holding only a window name is a title
    // setting, which the same older versions also wrote there. It is not a crew.
    const named = createWindowLifecycle(validOptions({
      port: 80,
      store: { get: (key) => (key === "remoteHosts" ? { "": { defaultName: "Pinned" } } : null) },
    }));
    assert.equal(
      named.security.isGatewayLocalForWindow({
        isDestroyed: () => false,
        _mcBackendUrl: "http://localhost/",
      }),
      true,
      "a defaultName-only legacy entry names no crew",
    );

    // A resolved-key entry wins, so the legacy record cannot override a crew the
    // user has since restated.
    const healed = createWindowLifecycle(validOptions({
      port: 80,
      store: {
        get: (key) => (key === "remoteHosts"
          ? { "": { host: "legacy.example.test" }, "80": { host: "current.example.test" } }
          : null),
      },
    }));
    assert.equal(
      healed.security.isGatewayLocalForWindow({
        isDestroyed: () => false,
        _mcBackendUrl: "http://localhost/",
      }),
      false,
    );
  });
});

describe("window lifecycle source contracts", () => {
  it("keys the remote-host writes with the same port the local-gateway gate reads", () => {
    // The gate and the forms that write `remoteHosts` must agree on the key, or
    // a crew the user records here classifies as a gateway on this machine. They
    // agree by both taking their port from the window's own backendUrl through
    // the normalizer, so pin that each of the three sites does.
    for (const fn of ["promptRemoteHost", "renameCurrentWindow"]) {
      const start = SOURCE.indexOf(`function ${fn}(`);
      assert.notEqual(start, -1, `${fn} must exist`);
      const body = SOURCE.slice(start, SOURCE.indexOf("\n  }\n", start));
      assert.match(
        body,
        /defaultedPort\(focused\._mcBackendUrl\)/,
        `${fn} must key remoteHosts by the window's own normalized port`,
      );
    }
    const gateStart = SOURCE.indexOf("function isGatewayLocalForWindow(");
    assert.notEqual(gateStart, -1);
    const gate = SOURCE.slice(gateStart, SOURCE.indexOf("\n  }\n", gateStart));
    assert.match(
      gate,
      /getRemoteHostConfigForUrl\(store, url\)/,
      "the gate must read remoteHosts through the URL-aware resolver",
    );
  });

  it("only claims SSH failed when an SSH attempt reported one", () => {
    // `fetchRemoteToken` keys its own lookup by port, so on a record the resolver
    // reached under the empty key it returns without running ssh at all. Naming
    // that "SSH to <host> failed" describes an attempt that never happened and
    // sends the reader to check a connection nothing used.
    const start = SOURCE.indexOf("async function refreshToken(");
    assert.notEqual(start, -1);
    const body = SOURCE.slice(start, SOURCE.indexOf("\n  }\n", start));
    assert.match(body, /detail: sshError\s*\n\s*\? `SSH to \$\{config\?\.host/, "the SSH wording must be gated on sshError");
    assert.match(body, /no SSH attempt was made/, "the no-attempt state must say so");
    assert.doesNotMatch(
      body,
      /sshError \|\| "Check your connection\."/,
      "a falsy sshError must not be papered over with generic advice under an SSH heading",
    );
  });

  it("retires a superseded empty-key record only after the write is durable", () => {
    // Order is the whole point. Retiring FIRST erases the record on a write that
    // is refused -- and `saveRemoteCrewConfig` refuses an unselectable port, so
    // on an http window resolving to :80 no write ever succeeds. The crew would
    // then read as this machine's own gateway with no way back. Pinned on the
    // source because the write runs inside a BrowserWindow's `closed` handler.
    const start = SOURCE.indexOf("function promptRemoteHost(");
    assert.notEqual(start, -1);
    const body = SOURCE.slice(start, SOURCE.indexOf("\n  }\n", start));
    assert.match(
      body,
      /const retireLegacy = \(\) => \{\s*\n\s*if \(portIsSchemeDefault\(focused\._mcBackendUrl\)\) \{\s*\n\s*retireLegacyEmptyPortHost\(store\);/,
      "retire only for a URL whose port is its scheme default",
    );
    const clear = body.indexOf("setRemoteHostConfig(store, focusedPort, {})");
    const save = body.indexOf("saveRemoteCrewConfig(store, focusedPort, fields)");
    const refusedReturn = body.indexOf("return;", body.indexOf("title: \"Invalid Input\""));
    const calls = [...body.matchAll(/retireLegacy\(\);/g)].map((m) => m.index);
    assert.ok(clear !== -1 && save !== -1 && refusedReturn !== -1, "both writes and the refusal must exist");
    assert.equal(calls.length, 2, "one retirement per write path, and no more");
    assert.ok(calls[0] > clear && calls[0] < save, "the clear path retires after its own write");
    assert.ok(
      calls[1] > refusedReturn,
      "the save path retires only past the refusal guard, so a refused save keeps the record",
    );
  });

  it("tears command/control owners down before closing dashboard contents", () => {
    const setupStart = SOURCE.indexOf("function setupWindowContents");
    const setupEnd = SOURCE.indexOf("function applyDashboardChrome", setupStart);
    assert.notEqual(setupStart, -1);
    assert.notEqual(setupEnd, -1);
    const setup = SOURCE.slice(setupStart, setupEnd);

    const stop = setup.indexOf("void win._mcAgentChannel.stop()");
    const destroyPanels = setup.indexOf("win._mcDestroyBrowserPanel(id)");
    const closeDashboard = setup.indexOf("view.webContents.close()");
    assert.ok(stop !== -1, "agent command channel cleanup missing");
    assert.ok(destroyPanels !== -1, "browser panel cleanup missing");
    assert.ok(closeDashboard !== -1, "dashboard WebContents cleanup missing");
    assert.ok(
      stop < destroyPanels && destroyPanels < closeDashboard,
      "cleanup order must be channel -> panels/control -> dashboard contents",
    );
  });

  it("registers the dashboard view as a capture surface on its own gateway origin", () => {
    // Screen capture is authorized against this registry, so a dashboard that is
    // never registered silently loses the chat composer's snip and the
    // web-preview crop. Pinned on the source because the security harness
    // registers a surface of its own, which would mask the call going missing.
    const setupStart = SOURCE.indexOf("function setupWindowContents");
    const setupEnd = SOURCE.indexOf("function applyDashboardChrome", setupStart);
    assert.notEqual(setupStart, -1);
    assert.notEqual(setupEnd, -1);
    const setup = SOURCE.slice(setupStart, setupEnd);
    assert.match(
      setup,
      /registerCaptureSurface\(view\.webContents, windowBackendUrl\)/,
      "the dashboard view must be registered against THIS window's gateway origin",
    );
  });

  it("hands keyboard focus from a hidden or released browser view back to the dashboard view", () => {
    // A BaseWindow routes keystrokes to exactly one child view. The manager
    // decides WHEN to hand focus back (browser-view.test.js); this pins that
    // the window wires the hand-back to the dashboard view, and that a window
    // re-activation asks every panel to heal a hidden-yet-focused view. Without
    // the first, every dashboard text input goes deaf after a modal opens over
    // the panel; without the second, the platform can put focus back onto the
    // hidden view when the window is re-activated.
    const setupStart = SOURCE.indexOf("function setupWindowContents");
    const setupEnd = SOURCE.indexOf("function applyDashboardChrome", setupStart);
    assert.notEqual(setupStart, -1);
    assert.notEqual(setupEnd, -1);
    const setup = SOURCE.slice(setupStart, setupEnd);

    const manager = setup.match(/createBrowserViewManager\(\{([\s\S]*?)\n      \}\);/);
    assert.ok(manager, "browser view manager wiring missing");
    assert.match(
      manager[1],
      /focusHost:\s*\(\)\s*=>\s*\{[\s\S]*?view\.webContents\.focus\(\)/,
      "focusHost must give the DASHBOARD view (the window's `view`) keyboard focus",
    );
    assert.match(
      manager[1],
      /focusHost:[\s\S]*?!view\.webContents\.isDestroyed\(\)[\s\S]*?view\.webContents\.focus\(\)/,
      "focusHost must not touch a dashboard WebContents that is already gone",
    );
    assert.match(
      setup,
      /win\.on\("focus",\s*\(\)\s*=>\s*\{\s*for \(const entry of browserPanels\.values\(\)\) entry\.manager\.reclaimFocus\(\);/,
      "window re-activation must ask every panel to reclaim focus from a hidden view",
    );
  });

  it("keeps immediate fullscreen bounds updates plus bounded settle passes", () => {
    assert.match(
      SOURCE,
      /const FULLSCREEN_SETTLE_MS = \[250, 1500\]/,
      "both the quick pass and slow-window-manager backstop are required",
    );
    for (const event of ["enter-full-screen", "leave-full-screen"]) {
      const match = SOURCE.match(
        new RegExp(`win\\.on\\("${event}", \\(\\) => \\{([\\s\\S]*?)\\}\\);`),
      );
      assert.ok(match, `${event} handler missing`);
      const body = match[1];
      const immediate = body.indexOf("updateViewBounds()");
      const notify = body.indexOf("sendFullScreen()");
      const settle = body.indexOf("scheduleFullscreenSettle()");
      assert.ok(
        immediate !== -1 && notify !== -1 && settle !== -1,
        `${event} must update, notify and settle`,
      );
      assert.ok(
        immediate < notify && notify < settle,
        `${event} ordering changed`,
      );
    }
    assert.match(
      SOURCE,
      /win\.on\("closed", \(\) => \{[\s\S]*?fullscreenSettleTimers[\s\S]*?clearTimeout/,
      "pending settle timers must be cleared at teardown",
    );
  });

  it("gives the dashboard's context menu the app origin and the browser panel none", () => {
    assert.match(
      SOURCE,
      /attachContextMenu\(view\.webContents, \{ getAppOrigin: \(\) => windowBackendUrl \}\)/,
      "the dashboard needs the origin so a chat file link copies as a bare path",
    );
    assert.match(
      SOURCE,
      /onCreate: \(child\) => attachContextMenu\(child\.webContents\),/,
      "an arbitrary site's same-origin pathname is not a local file, so no origin here",
    );
  });

  it("resolves every browser façade operation from the IPC sender's owner", () => {
    const resolver = SOURCE.match(
      /function panelForSender\(sender, panelId, opts\) \{([\s\S]*?)\n  \}/,
    );
    assert.ok(resolver, "panelForSender missing");
    assert.match(
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
      "browserTrackSession",
      "browserSetAgentAct",
      "browserSetControlOwner",
      "browserGetControl",
      "browserControl",
    ]) {
      const start = SOURCE.indexOf(`function ${name}(`);
      const asyncStart = SOURCE.indexOf(`async function ${name}(`);
      assert.ok(
        start !== -1 || asyncStart !== -1,
        `${name} façade missing`,
      );
      const at = Math.max(start, asyncStart);
      const next = SOURCE.indexOf("\n  function ", at + 1);
      const nextAsync = SOURCE.indexOf("\n  async function ", at + 1);
      const ends = [next, nextAsync].filter((value) => value !== -1);
      const end = ends.length ? Math.min(...ends) : SOURCE.length;
      const body = SOURCE.slice(at, end);
      assert.match(
        body,
        /panelForSender\(sender|windowForWebContents\(sender/,
        `${name} must not use a focused/global panel`,
      );
    }
  });

  it("resolves the zoom target from the dashboard view, not the focused webContents", () => {
    const zoom = SOURCE.match(
      /function zoomMenuItem\(apply\) \{([\s\S]*?)\n  \}/,
    );
    assert.ok(zoom, "zoomMenuItem missing");
    assert.match(
      zoom[1],
      /focusedDashboardWebContents\(\)/,
      "zoom must resolve the dashboard view like the sibling reload/devtools handlers",
    );
    assert.doesNotMatch(
      zoom[1],
      /webContents\.getFocusedWebContents\(\)/,
      "getFocusedWebContents() returns null under BaseWindow+contentView, so zoom would silently no-op",
    );
  });
});

describe("main window frame-load diagnostics", () => {
  it("journals frame loads on the dashboard webContents", () => {
    const createWindow = SOURCE.match(/function createWindow\(\) \{([\s\S]*?)\n  \}\n/);
    assert.ok(createWindow, "createWindow missing");
    assert.match(
      createWindow[1],
      /attachFrameLoadLogging\(\s*mainWindow\.webContents,\s*glog,\s*backendUrl,?\s*\)/,
      "a crew pane that never navigates must leave evidence in gateway-launch.log",
    );
  });

  it("passes the dashboard's own URL as the trusted origin", () => {
    // Without the third argument the `[pane]` journal is disabled rather than
    // granted to whoever happens to be the top frame — so the wiring, not just
    // the gate inside the module, is what has to be pinned here.
    assert.match(
      SOURCE,
      /attachFrameLoadLogging\([^)]*backendUrl/,
      "the [pane] allowlist must be anchored to the origin the window was loaded with",
    );
  });

  it("writes those lines through the launch log, not console only", () => {
    assert.match(
      SOURCE,
      /require\("\.\/frame-load-log"\)/,
      "frame diagnostics must come from the shared, unit-tested module",
    );
  });
});
