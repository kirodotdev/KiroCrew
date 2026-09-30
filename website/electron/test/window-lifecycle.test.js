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
const RUNTIME_DIR = path.join(__dirname, "..", "runtime", "window");
const PANELS_SOURCE = fs.readFileSync(path.join(RUNTIME_DIR, "browser-panels.js"), "utf8")
  .replace(/\r\n/g, "\n");
const {
  BROWSER_PARTITION,
  createWindowLifecycle,
  isAllowedTransientShellNavigation,
} = require("../window-lifecycle");
const { registerCaptureSurface } = require("../capture-trust");

function validOptions(overrides = {}) {
  return {
    electron: {},
    store: { get: () => null },
    backendUrl: "http://localhost:5476",
    port: 5476,
    mintLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    // Keep construction independent of the host running the suite.
    platform: "test",
    ...overrides,
  };
}

describe("every token-bearing navigation stays on the configured origin", () => {
  // The invariant, stated once: a navigation that carries a credential is
  // addressed to the configured backend URL, and no site derives a second origin
  // to put it on. What makes that one origin safe is decided before the secret
  // leaves -- the mint refuses unless this gateway holds every loopback family
  // the configured host resolves to -- and what makes a rewritten one unsafe is
  // the browser: storage is partitioned by origin, so moving the document strands
  // every existing user's unsent drafts, and the configured string is compared by
  // exact equality in several places. This scans the SOURCE rather than one call
  // path, so a site added later reddens too.
  const SUPERVISOR = fs
    .readFileSync(path.join(__dirname, "..", "gateway-supervisor.js"), "utf8")
    .replace(/\r\n/g, "\n");

  /** Lines composing a credential onto a base, with the 4 lines before each. */
  function credentialSites(text, isSite) {
    const lines = text.split("\n");
    return lines
      .map((line, index) => ({ line, index, before: lines.slice(Math.max(0, index - 4), index + 1) }))
      .filter(({ line }) => isSite(line));
  }

  it("never re-bases a token-bearing navigation onto a different origin", () => {
    // A structural guard rather than a behavioural one, because the failure mode
    // is a NEW navigation site added later on a path no test drives.
    const offenders = credentialSites(
      SOURCE,
      (line) => /\?token=\$\{/.test(line) || /searchParams\.set\(\s*"token"/.test(line),
    )
      .filter(({ line }) => /\$\{\s*\w*[Oo]rigin\s*\|\|/.test(line) || /loopbackOrigin/.test(line))
      .map(({ line, index }) => `window-lifecycle.js:${index + 1}: ${line.trim()}`);

    assert.deepEqual(
      offenders,
      [],
      "each of these re-bases a token-bearing navigation onto a rewritten origin",
    );
  });

  it("covers every navigation site rather than passing on an empty scan", () => {
    // Control for the guard above: an empty offender list must mean the sites
    // are right, not that the pattern matched nothing.
    const sites = credentialSites(
      SOURCE,
      (line) => /\?token=\$\{/.test(line) || /searchParams\.set\(\s*"token"/.test(line),
    );
    assert.ok(sites.length >= 4, `expected at least 4 token-bearing sites, found ${sites.length}`);
  });

  it("never drops the origin on a fallback that still carries a credential", () => {
    // The other side of the invariant, and the one a diff-reading sweep misses:
    // a half-plumbed origin -- a site that names one and then zeroes it -- leaves
    // the destination depending on which branch ran. No site may hold an origin
    // variable at all, whether it fills it or empties it.
    const offenders = [];
    for (const [name, text] of [["window-lifecycle.js", SOURCE], ["gateway-supervisor.js", SUPERVISOR]]) {
      text.split("\n").forEach((line, index) => {
        if (/tokenOrigin\s*=\s*""/.test(line)) {
          offenders.push(`${name}:${index + 1}: ${line.trim()}`);
        }
      });
    }
    assert.deepEqual(offenders, [], "a fallback here carries a token to a re-derived destination");
  });

  it("leaves every remote fallback addressing the URL it was given", () => {
    // An SSH-fetched token is a bearer on the same terms as a minted one, and it
    // reaches the same one origin. The absence of any origin plumbing in this
    // module IS the property, asserted positively so a reintroduction fails here
    // rather than in review.
    assert.equal(
      /loopbackOrigin|tokenOrigin/.test(SOURCE),
      false,
      "window-lifecycle.js must not derive an origin for a navigation",
    );
  });

  it("leaves the boot navigation on the configured backend URL", () => {
    // The boot navigation is the one token-bearing site outside this module, and
    // it addresses the URL it was configured with -- so the first document the
    // user sees is already on the origin every later navigation uses.
    const supervisor = fs.readFileSync(
      path.join(__dirname, "..", "gateway-supervisor.js"),
      "utf8",
    );
    assert.equal(
      /loopbackOrigin|tokenOrigin/.test(supervisor),
      false,
      "gateway-supervisor.js must not derive an origin for the boot navigation",
    );
  });
});

describe("window lifecycle module boundary", () => {
  it("loads in plain Node and never requires Electron at module scope", () => {
    assert.doesNotMatch(
      SOURCE,
      /require\(\s*["']electron["']\s*\)/,
      "Electron must come from the factory argument so node:test can load this module",
    );
    assert.equal(typeof createWindowLifecycle, "function");
  });

  it("composes its runtime owners, none of which loads Electron or anchors on its own directory", () => {
    // Electron arrives only through the factory argument, and every asset path
    // (preload, icons, tray template) stays anchored on the facade's directory.
    const owners = fs.readdirSync(RUNTIME_DIR).filter((name) => name.endsWith(".js")).sort();
    assert.deepEqual(owners, [
      "browser-panels.js",
      "chrome.js",
      "linux-captions.js",
      "prompts.js",
      "session-security.js",
    ]);
    for (const owner of owners) {
      const source = fs.readFileSync(path.join(RUNTIME_DIR, owner), "utf8");
      assert.doesNotMatch(source, /require\(\s*["']electron["']\s*\)/, `${owner} loads Electron`);
      assert.doesNotMatch(source, /__dirname/, `${owner} must not resolve assets from runtime/window`);
      assert.doesNotMatch(source, /require\(\s*"\.\.\/\.\.\/window-lifecycle"\s*\)/, `${owner} requires the facade`);
      const stem = owner.replace(/\.js$/, "");
      assert.match(
        SOURCE,
        new RegExp(`require\\("\\./runtime/window/${stem}"\\)`),
        `the facade composes ${owner} through an explicit, packaged require`,
      );
    }
  });

  it("fails loudly for every required composition dependency", () => {
    const cases = [
      ["electron", /electron is required/],
      ["store", /store is required/],
      ["backendUrl", /backendUrl is required/],
      ["port", /port is required/],
      ["mintLocalToken", /mintLocalToken is required/],
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

describe("transient shell navigation", () => {
  const backend = "http://localhost:5476";

  it("blocks every navigation initiated by stock and edition loading pages", () => {
    for (const page of ["loading.html", "edition-loading.html"]) {
      assert.equal(
        isAllowedTransientShellNavigation(`file:///app/${page}`, backend, backend),
        false,
      );
      assert.equal(
        isAllowedTransientShellNavigation(
          `file:///app/${page}`,
          "https://attacker.example/payload",
          backend,
        ),
        false,
      );
    }
  });

  it("allows the token prompt to hand off only to its configured gateway", () => {
    const prompt = "file:///app/token-prompt.html?port=5476";
    assert.equal(
      isAllowedTransientShellNavigation(prompt, `${backend}/?token=value`, backend),
      true,
    );
    assert.equal(
      isAllowedTransientShellNavigation(prompt, "http://localhost:6124/?token=value", backend),
      false,
    );
    assert.equal(
      isAllowedTransientShellNavigation(prompt, "https://attacker.example/", backend),
      false,
    );
  });

  it("does not change dashboard navigation policy", () => {
    assert.equal(
      isAllowedTransientShellNavigation(`${backend}/chat`, "https://example.com/", backend),
      true,
    );
  });

  it("applies the same origin guard to direct navigation and redirects", () => {
    assert.match(
      SOURCE,
      /webContents\.on\("will-navigate", guardTransientShellNavigation\)/,
    );
    assert.match(
      SOURCE,
      /webContents\.on\("will-redirect", guardTransientShellNavigation\)/,
    );
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
});

describe("window lifecycle source contracts", () => {
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

    // The panel registry is its own owner; the window hands it the dashboard view.
    assert.match(setup, /const browserPanels = attachBrowserPanels\(win, view, \{/);
    const manager = PANELS_SOURCE.match(/createBrowserViewManager\(\{([\s\S]*?)\n    \}\);/);
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
      PANELS_SOURCE,
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

// ── Characterization of the window runtime owners ─────────────────────────
//
// The facade composes session security, window chrome, Linux captions, modal
// prompts and the per-window browser panels. These tests pin what a dashboard
// window gets wired with, in what order, on each platform, so an ownership
// move that reorders a listener or drops a registration fails here by name.

function recordingEmitter(label, log) {
  const handlers = new Map();
  return {
    handlers,
    on(event, handler) {
      log.push(`${label}.on:${event}`);
      if (!handlers.has(event)) handlers.set(event, []);
      handlers.get(event).push(handler);
      return this;
    },
    emit(event, ...args) {
      for (const handler of handlers.get(event) || []) handler(...args);
    },
  };
}

function dashboardWindowHarness({ platform, frameless = false } = {}) {
  const log = [];
  const firstLine = (text) => String(text).split("\n").map((line) => line.trim()).find(Boolean);
  const viewEvents = recordingEmitter("view", log);
  const viewContents = {
    ...viewEvents,
    mainFrame: { url: "http://localhost:5476/" },
    session: {
      webRequest: {
        onBeforeSendHeaders() { log.push("view.session.onBeforeSendHeaders"); },
      },
    },
    setWindowOpenHandler() { log.push("view.setWindowOpenHandler"); },
    insertCSS(css) { log.push(`view.insertCSS:${firstLine(css)}`); return Promise.resolve(); },
    executeJavaScript(script) {
      log.push(`view.executeJavaScript:${firstLine(script)}`);
      return Promise.resolve("");
    },
    send(channel) { log.push(`view.send:${channel}`); },
    getZoomFactor: () => 1,
    isDestroyed: () => false,
    focus() {},
    close() { log.push("view.close"); },
  };
  const winEvents = recordingEmitter("win", log);
  const win = {
    ...winEvents,
    contentView: {
      addChildView() { log.push("win.addChildView"); },
      removeChildView() {},
    },
    isDestroyed: () => false,
    isFullScreen: () => false,
    isMaximized: () => false,
    getContentBounds: () => ({ x: 0, y: 0, width: 1280, height: 860 }),
    setTitle(title) { log.push(`win.setTitle:${title}`); },
    setBackgroundColor() {},
    setWindowButtonPosition(position) { log.push(`win.setWindowButtonPosition:${JSON.stringify(position)}`); },
    setTitleBarOverlay() { log.push("win.setTitleBarOverlay"); },
  };
  const panelViews = [];
  class WebContentsView {
    constructor(options) {
      if (options.webPreferences.partition) {
        // An embedded browser panel: its own page, never the dashboard's.
        panelViews.push(options);
        this.webContents = {
          ...recordingEmitter("panel", []),
          loads: [],
          loadURL(url) { this.loads.push(url); return Promise.resolve(); },
          setWindowOpenHandler() {},
          isDestroyed: () => false,
          getTitle: () => "",
          getURL: () => "",
          focus() {},
          close() {},
        };
        return;
      }
      log.push(`new WebContentsView:${JSON.stringify(options.webPreferences.additionalArguments || [])}`);
      this.webContents = viewContents;
    }
    setBackgroundColor() {}
    setBounds() {}
    setVisible() {}
  }
  const lifecycle = createWindowLifecycle(validOptions({
    electron: {
      WebContentsView,
      BaseWindow: { getAllWindows: () => [win], getFocusedWindow: () => null },
      Menu: { buildFromTemplate: () => ({ popup() {} }) },
      shell: { openExternal() {} },
      nativeTheme: { shouldUseDarkColors: false, themeSource: "system" },
      app: { getPath: () => "/virtual/logs", getVersion: () => "0.8.0", name: "Kiro Crew" },
    },
    store: { get: (key) => (key === "linuxFrameless" ? frameless : null) },
    platform,
    env: {},
  }));
  return { lifecycle, win, viewContents, log, panelViews };
}

async function wireDashboardWindow(t, options) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, status: 204, json: async () => null }));
  const harness = dashboardWindowHarness(options);
  harness.lifecycle.setupWindowContents(harness.win, "http://localhost:5476");
  const wiring = harness.log.splice(0);
  harness.viewContents.emit("did-finish-load");
  const onLoad = harness.log.splice(0);
  await harness.win._mcAgentChannel.stop();
  return { ...harness, wiring, onLoad };
}

const COMMON_WIRING_HEAD = [
  "new WebContentsView:[]",
  "win.addChildView",
];

describe("dashboard window wiring order", () => {
  it("a macOS window positions traffic lights and tracks zoom before its load handlers", async (t) => {
    const { wiring, onLoad } = await wireDashboardWindow(t, { platform: "darwin" });
    assert.deepEqual(wiring.slice(0, 2), COMMON_WIRING_HEAD);
    const events = wiring.filter((entry) => entry.startsWith("win.on:") || entry.startsWith("view.on:")
      || entry.startsWith("view.set") || entry.startsWith("view.session") || entry.startsWith("win.set"));
    assert.deepEqual(events, [
      "view.on:did-finish-load",
      "win.on:closed",
      "win.on:resize",
      "win.on:closed",
      "win.on:enter-full-screen",
      "win.on:leave-full-screen",
      "view.on:enter-html-full-screen",
      "view.on:leave-html-full-screen",
      "win.on:leave-full-screen",
      "win.on:show",
      "win.on:restore",
      "win.on:move",
      "view.on:did-finish-load",
      "view.on:context-menu",
      "win.setWindowButtonPosition:{\"x\":16,\"y\":11}",
      "view.on:zoom-changed",
      "win.on:system-context-menu",
      "view.on:did-finish-load",
      "view.on:page-title-updated",
      "view.on:did-finish-load",
      "win.on:focus",
      "win.on:focus",
      "view.setWindowOpenHandler",
      "view.on:will-navigate",
      "view.on:will-redirect",
      "view.session.onBeforeSendHeaders",
    ]);
    assert.deepEqual(onLoad, [
      "view.send:fullscreen-changed",
      "win.setTitle:Kiro Crew [:5476]",
      "view.insertCSS:#electron-drag-bar {",
      "view.executeJavaScript:if (!document.getElementById('electron-drag-bar')) {",
      "view.executeJavaScript:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()",
      "view.executeJavaScript:JSON.stringify({pref: document.documentElement.dataset.modePref || \"\",mode: document.documentElement.dataset.mode || \"\"})",
    ]);
  });

  it("a Windows window paints the title-bar overlay and tracks zoom", async (t) => {
    const { wiring, onLoad } = await wireDashboardWindow(t, { platform: "win32" });
    const chrome = wiring.filter((entry) => entry === "win.setTitleBarOverlay" || entry === "view.on:zoom-changed"
      || entry === "win.on:system-context-menu" || entry === "view.on:context-menu");
    assert.deepEqual(chrome, [
      "view.on:context-menu",
      "win.setTitleBarOverlay",
      "view.on:zoom-changed",
      "win.on:system-context-menu",
    ]);
    assert.ok(!wiring.some((entry) => entry.startsWith("win.setWindowButtonPosition")));
    assert.equal(onLoad[1], "win.setTitle:Kiro Crew", "the default local port is omitted on Windows");
    assert.equal(onLoad[2], "view.insertCSS:#electron-drag-bar {");
  });

  it("a frameless Linux window injects caption controls after the drag band", async (t) => {
    const { wiring, onLoad, win } = await wireDashboardWindow(t, { platform: "linux", frameless: true });
    assert.equal(wiring[0], "new WebContentsView:[\"--kc-linux-frameless\"]");
    assert.ok(!wiring.includes("view.on:zoom-changed"), "zoom tracking is macOS/Windows only");
    assert.deepEqual(onLoad, [
      "view.send:fullscreen-changed",
      "win.setTitle:Kiro Crew [:5476]",
      "view.insertCSS:#electron-drag-bar {",
      "view.executeJavaScript:if (!document.getElementById('electron-drag-bar')) {",
      "view.insertCSS:#electron-linux-controls {",
      "view.executeJavaScript:if (!document.getElementById('electron-linux-controls')) {",
      "win.on:maximize",
      "win.on:unmaximize",
      "view.executeJavaScript:{",
      "view.executeJavaScript:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()",
      "view.executeJavaScript:JSON.stringify({pref: document.documentElement.dataset.modePref || \"\",mode: document.documentElement.dataset.mode || \"\"})",
    ]);
    assert.equal(win._mcLinuxMaximizeSyncArmed, true);
  });

  it("a framed window injects neither the drag band nor caption controls", async (t) => {
    const { onLoad } = await wireDashboardWindow(t, { platform: "linux", frameless: false });
    assert.deepEqual(onLoad, [
      "view.send:fullscreen-changed",
      "win.setTitle:Kiro Crew [:5476]",
      "view.executeJavaScript:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()",
      "view.executeJavaScript:JSON.stringify({pref: document.documentElement.dataset.modePref || \"\",mode: document.documentElement.dataset.mode || \"\"})",
    ]);
  });

  it("the window carries every cross-module property before any caller loads it", async (t) => {
    const { win, viewContents } = await wireDashboardWindow(t, { platform: "darwin" });
    assert.equal(win.webContents, viewContents);
    assert.equal(win._mcView.webContents, viewContents);
    assert.equal(win._mcBackendUrl, "http://localhost:5476");
    assert.ok(win._mcBrowserPanels instanceof Map);
    assert.equal(typeof win._mcBrowserPanel, "function");
    assert.equal(typeof win._mcDestroyBrowserPanel, "function");
    assert.ok(win._mcReachableSessions instanceof Set);
    assert.equal(typeof win._mcSetCustomName, "function");
    assert.equal(win._mcGetCustomName(), null);
    win._mcSetCustomName("Work");
    assert.equal(win._mcGetCustomName(), "Work");
  });
});

describe("browser panel IPC routing", () => {
  it("resolves every request from its sender and never creates a panel from a layout report", async (t) => {
    const { lifecycle, win, viewContents } = await wireDashboardWindow(t, { platform: "darwin" });
    const stranger = { isDestroyed: () => false };
    assert.equal(lifecycle.browser.getState(stranger, "p1"), null);
    assert.equal(lifecycle.browser.setBounds(viewContents, "p1", { x: 0, y: 0, width: 1, height: 1 }), null);
    assert.equal(lifecycle.browser.setOverlay(viewContents, "p1", true), null);
    assert.equal(lifecycle.browser.setInactive(viewContents, "p1", true), null);
    assert.equal(lifecycle.browser.close(viewContents, "p1"), null);
    assert.equal(lifecycle.browser.getControl(viewContents, "p1"), null);
    assert.equal(await lifecycle.browser.control(viewContents, "p1", "snapshot", {}), null);
    assert.deepEqual(
      await lifecycle.browser.annotate(viewContents, "p1", "poll", {}),
      { ok: false, code: "no_view", error: "no native browser panel" },
    );
    assert.equal(win._mcBrowserPanels.size, 0, "no panel was created by any of the above");

    assert.deepEqual(lifecycle.browser.trackSession(stranger, "s1", true), { ok: false });
    assert.deepEqual(lifecycle.browser.trackSession(viewContents, "  s1  ", true), { ok: true });
    assert.deepEqual([...win._mcReachableSessions], ["s1"]);
    assert.deepEqual(lifecycle.browser.trackSession(viewContents, "s1", false), { ok: true });
    assert.equal(win._mcReachableSessions.size, 0);
  });
});

describe("embedded browser panel isolation", () => {
  it("opens a panel in the deny-all partition with every renderer privilege off", async (t) => {
    const { lifecycle, viewContents, panelViews, win } = await wireDashboardWindow(t, { platform: "darwin" });
    lifecycle.browser.open(viewContents, "p1", "https://example.com/");
    assert.equal(panelViews.length, 1, "open creates exactly one panel view");
    assert.deepEqual(panelViews[0].webPreferences, {
      partition: BROWSER_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
    });
    assert.equal(BROWSER_PARTITION, "persist:kirocrew-browser");
    assert.ok(win._mcBrowserPanels.has("p1"));
    lifecycle.browser.close(viewContents, "p1");
    assert.equal(win._mcBrowserPanels.size, 0);
  });
});

describe("window chrome controls", () => {
  it("places macOS traffic lights from the dashboard zoom", () => {
    const placed = [];
    const lifecycle = createWindowLifecycle(validOptions({ platform: "darwin" }));
    const at = (zoom) => {
      lifecycle.positionTrafficLights({
        isDestroyed: () => false,
        _mcView: { webContents: { getZoomFactor: () => zoom } },
        setWindowButtonPosition: (position) => placed.push(position),
      });
    };
    at(1);
    at(1.5);
    at(2);
    assert.deepEqual(placed, [{ x: 16, y: 11 }, { x: 24, y: 22 }, { x: 32, y: 32 }]);

    const offMac = [];
    const elsewhere = createWindowLifecycle(validOptions({ platform: "win32" }));
    elsewhere.positionTrafficLights({
      isDestroyed: () => false,
      _mcView: { webContents: { getZoomFactor: () => 1 } },
      setWindowButtonPosition: (position) => offMac.push(position),
    });
    assert.deepEqual(offMac, [], "traffic lights are placed on macOS only");
  });

  it("stores only a well-formed accent and resolves only known theme modes", () => {
    const stored = {};
    const nativeTheme = { themeSource: "system", shouldUseDarkColors: false };
    const lifecycle = createWindowLifecycle(validOptions({
      electron: { nativeTheme },
      store: { get: () => null, set: (key, value) => { stored[key] = value; } },
    }));
    lifecycle.chrome.setThemeAccent("#abc");
    lifecycle.chrome.setThemeAccent("#12345");
    lifecycle.chrome.setThemeAccent("red");
    assert.deepEqual(stored, { themeAccent: "#abc" });

    lifecycle.chrome.setThemeMode("dark");
    assert.equal(nativeTheme.themeSource, "dark");
    lifecycle.chrome.setThemeMode("sepia");
    assert.equal(nativeTheme.themeSource, "dark", "an unknown preference is ignored");
    lifecycle.chrome.setThemeMode("system");
    assert.equal(nativeTheme.themeSource, "system");

    lifecycle.chrome.setTitlebarMode("dark");
  });

  it("zoom requests clamp, step, and reconcile every dashboard window", () => {
    const placed = [];
    const dashboard = {
      _mcView: { webContents: { getZoomFactor: () => 1 } },
      isDestroyed: () => false,
      setWindowButtonPosition: (position) => placed.push(position),
    };
    const lifecycle = createWindowLifecycle(validOptions({
      platform: "darwin",
      electron: { BaseWindow: { getAllWindows: () => [dashboard, { isDestroyed: () => false }] } },
    }));
    let zoom = 1;
    const sender = {
      getZoomFactor: () => zoom,
      setZoomFactor: (factor) => { zoom = factor; },
    };
    assert.equal(lifecycle.chrome.getZoom(sender), 1);
    const stepped = lifecycle.chrome.stepZoom(sender, 1);
    assert.ok(stepped > 1);
    assert.equal(zoom, stepped);
    assert.equal(lifecycle.chrome.setZoom(sender, 100), zoom, "setZoom returns the clamped factor");
    assert.ok(zoom < 100);
    assert.equal(placed.length, 2, "only windows carrying a dashboard view are reconciled");
  });
});

describe("modal prompts", () => {
  function promptHarness({ title, platform = "test" } = {}) {
    const stored = {};
    const opened = [];
    class PromptWindow {
      constructor(options) {
        this.options = options;
        this.handlers = {};
        opened.push(this);
      }
      setMenu() {}
      on(event, handler) { this.handlers[event] = handler; }
      loadURL(url) {
        this.url = url;
        setImmediate(() => {
          if (title !== null) this.handlers["page-title-updated"]?.({}, title);
          this.handlers.closed?.();
        });
      }
    }
    const names = [];
    const focused = {
      isDestroyed: () => false,
      getTitle: () => "Kiro Crew [:5476]",
      _mcBackendUrl: "http://localhost:5476",
      _mcSetCustomName: (name) => names.push(name),
    };
    const lifecycle = createWindowLifecycle(validOptions({
      platform,
      electron: {
        BaseWindow: { getFocusedWindow: () => focused },
        BrowserWindow: PromptWindow,
        nativeTheme: { shouldUseDarkColors: true },
      },
      store: {
        get: (key) => (key in stored ? stored[key] : null),
        set: (key, value) => { stored[key] = value; },
      },
    }));
    return { lifecycle, stored, opened, names };
  }
  const settle = () => new Promise((resolve) => setImmediate(() => setImmediate(resolve)));

  it("rename stores the name and, when asked, the port's default name", async () => {
    const { lifecycle, stored, opened, names } = promptHarness({
      title: JSON.stringify({ name: "Work", setDefault: true }),
    });
    lifecycle.renameCurrentWindow();
    await settle();
    assert.equal(opened.length, 1);
    assert.deepEqual(
      { width: opened[0].options.width, height: opened[0].options.height, modal: opened[0].options.modal },
      { width: 400, height: 200, modal: true },
    );
    const html = decodeURIComponent(opened[0].url);
    assert.match(html, /value="\[:5476\]"/, "opens on the current name without the brand prefix");
    assert.match(html, /background:#1e293b/, "a window with no dashboard falls back to the native dark palette");
    assert.deepEqual(names, ["Work"]);
    assert.deepEqual(stored.remoteHosts, { 5476: { defaultName: "Work" } });
  });

  it("rename keeps the legacy plain-text answer and ignores a cancel", async () => {
    const legacy = promptHarness({ title: "Legacy" });
    legacy.lifecycle.renameCurrentWindow();
    await settle();
    assert.deepEqual(legacy.names, ["Legacy"]);

    const cancelled = promptHarness({ title: null });
    cancelled.lifecycle.renameCurrentWindow();
    await settle();
    assert.deepEqual(cancelled.names, []);
    assert.equal(cancelled.stored.remoteHosts, undefined);
  });
});

describe("microphone recovery dialog", () => {
  it("opens one Privacy dialog per denial burst and none when the probe fails", async () => {
    const boxes = [];
    let status = "denied";
    let release;
    const lifecycle = createWindowLifecycle(validOptions({
      platform: "darwin",
      electron: {
        systemPreferences: {
          getMediaAccessStatus: () => {
            if (status === "throw") throw new Error("probe unavailable");
            return status;
          },
        },
        dialog: {
          showMessageBox: (options) => {
            boxes.push(options);
            return new Promise((resolve) => { release = () => resolve({ response: 1 }); });
          },
        },
        shell: { openExternal() {} },
      },
    }));
    lifecycle.security.micDenied();
    lifecycle.security.micDenied();
    assert.equal(boxes.length, 1, "a racing second denial is latched");
    assert.equal(boxes[0].title, "Microphone permission needed");
    assert.deepEqual(boxes[0].buttons, ["Open System Settings", "Cancel"]);
    release();
    await new Promise((resolve) => setImmediate(resolve));

    status = "restricted";
    lifecycle.security.micDenied();
    assert.equal(boxes.length, 2);
    assert.deepEqual(boxes[1].buttons, ["OK"]);
    release();
    await new Promise((resolve) => setImmediate(resolve));

    status = "granted";
    lifecycle.security.micDenied();
    status = "throw";
    lifecycle.security.micDenied();
    assert.equal(boxes.length, 2);

    const offMac = createWindowLifecycle(validOptions({ platform: "win32" }));
    offMac.security.micDenied();
  });
});

describe("gateway port prompt", () => {
  const { createWindowPrompts } = require("../runtime/window/prompts");

  function portPrompt(answer) {
    const opened = [];
    class PromptWindow {
      constructor(options) {
        this.options = options;
        this.handlers = {};
        opened.push(this);
      }
      setMenu() {}
      on(event, handler) { this.handlers[event] = handler; }
      loadURL(url) {
        this.url = url;
        setImmediate(() => {
          if (answer !== null) this.handlers["page-title-updated"]?.({}, answer);
          this.handlers.closed?.();
        });
      }
    }
    const prompts = createWindowPrompts({
      BaseWindow: { getFocusedWindow: () => null },
      BrowserWindow: PromptWindow,
      nativeTheme: { shouldUseDarkColors: false },
      store: { get: () => null },
      getMainWindow: () => null,
    });
    return { prompts, opened };
  }
  const settle = () => new Promise((resolve) => setImmediate(() => setImmediate(resolve)));

  it("parents the form on the window current once its styling is ready", async () => {
    const { prompts, opened } = portPrompt(null);
    let parent = "before";
    const pending = prompts.promptConnectionPort(() => parent, async () => {});
    parent = "after";
    await pending;
    assert.equal(opened[0].options.parent, "after");
    assert.equal(opened[0].options.modal, true);
    await settle();
  });

  it("hands on only a port in 1..65535", async () => {
    for (const [answer, expected] of [
      ["7778", [7778]],
      [" 1 ", [1]],
      ["65535", [65535]],
      ["0", []],
      ["65536", []],
      ["abc", []],
      [null, []],
    ]) {
      const { prompts } = portPrompt(answer);
      const ports = [];
      await prompts.promptConnectionPort(() => null, async (port) => { ports.push(port); });
      await settle();
      assert.deepEqual(ports, expected, `answer ${JSON.stringify(answer)}`);
    }
  });
});
