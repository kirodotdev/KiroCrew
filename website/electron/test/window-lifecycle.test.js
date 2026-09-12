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
  const electron = {
    session: {
      defaultSession,
      fromPartition(name) {
        calls.fromPartition.push(name);
        return browserSession;
      },
    },
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
  return { calls, lifecycle };
}

describe("session security registration", () => {
  it("registers every default and browser-partition policy exactly once", async () => {
    const { calls, lifecycle } = securityHarness();

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

    let displayResult = null;
    await calls.display[0].handler({}, (result) => { displayResult = result; });
    assert.deepEqual(displayResult, {
      video: { id: "screen:0", name: "Screen" },
    });
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

describe("reconcileFontSizeChecks — Font Size menu radio state", () => {
  // Helper: build a minimal menu whose radio items match `font-size-<tier.name>`
  // ids. Records every .checked assignment (with the tier's name) so tests can
  // assert exactly which items were touched — the whole point of the bug #3
  // fix is that ONLY the target should be touched, not the siblings.
  function makeMenuMock(tiers) {
    const items = new Map();
    const assignments = [];
    for (const tier of tiers) {
      const id = `font-size-${tier.name}`;
      const item = { id };
      Object.defineProperty(item, "checked", {
        set(value) { assignments.push([id, value]); },
        get() { return false; },
      });
      items.set(id, item);
    }
    return {
      menu: { getMenuItemById: (id) => items.get(id) || null },
      assignments,
    };
  }

  const TIERS = [
    { name: "verySmall", px: 9 },
    { name: "small", px: 12 },
    { name: "medium", px: 16 },
    { name: "large", px: 20 },
    { name: "veryLarge", px: 24 },
  ];

  it("sets ONLY the target item's checked=true and leaves siblings untouched", () => {
    // Idiomatic Electron: the radio group auto-toggles when one item is set
    // to true. Explicit .checked=false on siblings conflicts with the
    // "exactly one checked" invariant and on some platforms leaves the OS
    // display showing the LAST-touched item rather than the actual target
    // — that was the observed bug ("always says Very Large").
    const { reconcileFontSizeChecks } = require("../window-lifecycle");
    const { menu, assignments } = makeMenuMock(TIERS);

    reconcileFontSizeChecks(menu, TIERS, 12);

    assert.deepEqual(assignments, [["font-size-small", true]]);
  });

  it("no-ops when the menu is null (Menu.getApplicationMenu returned nothing yet)", () => {
    const { reconcileFontSizeChecks } = require("../window-lifecycle");
    assert.doesNotThrow(() => reconcileFontSizeChecks(null, TIERS, 16));
  });

  it("no-ops when px does not match any tier (defensive against a stale IPC call)", () => {
    const { reconcileFontSizeChecks } = require("../window-lifecycle");
    const { menu, assignments } = makeMenuMock(TIERS);

    reconcileFontSizeChecks(menu, TIERS, 999);

    assert.deepEqual(assignments, [], "no radio touched for an unknown px");
  });

  it("no-ops when the matching item id is not in the menu yet (menu built without Font Size)", () => {
    const { reconcileFontSizeChecks } = require("../window-lifecycle");
    const emptyMenu = { getMenuItemById: () => null };
    assert.doesNotThrow(() => reconcileFontSizeChecks(emptyMenu, TIERS, 16));
  });
});

// ── Shell-owned window construction contract (PR #10247 Design Review response) ──
// Every `new BrowserWindow` and `new WebContentsView` in window-lifecycle.js
// must either (a) live inside createShellBrowserWindow / createShellWebContentsView,
// or (b) carry an explicit `SHELL-BARE:` marker documenting the exclusion
// (currently: only the embedded browser panel's WebContentsView).
//
// This contract keeps the "seeded at construction ↔ rippled at runtime"
// invariant (see display-preferences/index.js SHELL_OWNED_WC) machine-
// checkable — a future contributor adding a new shell window who forgets
// to tag it would fail this test rather than silently ship a window that
// gets its font size seeded but never rippled on runtime tier changes.
describe("shell-owned window construction contract", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const SOURCE = fs.readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");

  it("createShellBrowserWindow helper exists and is the only `new BrowserWindow(` in the file (SHELL-BARE marker exempts otherwise)", () => {
    assert.ok(
      /function createShellBrowserWindow\s*\(/.test(SOURCE),
      "createShellBrowserWindow helper must be defined",
    );

    const lines = SOURCE.split("\n");
    const offenders = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!/\bnew BrowserWindow\(/.test(line)) continue;
      // Exempt: (a) the wrapper's own definition line; (b) any line
      // carrying the explicit SHELL-BARE marker on the same or previous
      // non-blank line.
      if (/function createShellBrowserWindow/.test(lines[Math.max(0, i - 1)])) continue;
      if (/const win = new BrowserWindow\(opts\);/.test(line)) continue; // wrapper body
      if (/SHELL-BARE/.test(line)) continue;
      // Look one line back for a same-block SHELL-BARE marker.
      if (i > 0 && /SHELL-BARE/.test(lines[i - 1])) continue;
      offenders.push(`  L${i + 1}: ${line.trim()}`);
    }
    assert.strictEqual(
      offenders.length,
      0,
      `Found bare \`new BrowserWindow(\` calls in window-lifecycle.js — route them through createShellBrowserWindow or add a SHELL-BARE marker:\n${offenders.join("\n")}`,
    );
  });

  it("createShellWebContentsView helper exists and is the only `new WebContentsView(` in the file (SHELL-BARE marker exempts otherwise)", () => {
    assert.ok(
      /function createShellWebContentsView\s*\(/.test(SOURCE),
      "createShellWebContentsView helper must be defined",
    );

    const lines = SOURCE.split("\n");
    const offenders = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!/\bnew WebContentsView\(/.test(line)) continue;
      if (/const view = new WebContentsView\(opts\);/.test(line)) continue; // wrapper body
      if (/SHELL-BARE/.test(line)) continue;
      if (i > 0 && /SHELL-BARE/.test(lines[i - 1])) continue;
      offenders.push(`  L${i + 1}: ${line.trim()}`);
    }
    assert.strictEqual(
      offenders.length,
      0,
      `Found bare \`new WebContentsView(\` calls in window-lifecycle.js — route them through createShellWebContentsView or add a SHELL-BARE marker:\n${offenders.join("\n")}`,
    );
  });

  it("SHELL-BARE marker documents at least the browser panel exemption (regression guard)", () => {
    // If someone rewrites the browser panel path and forgets the marker,
    // the two tests above would refuse the change — this test just
    // confirms the marker is still discoverable in the file. If the
    // browser panel gets its own module later and vanishes from
    // window-lifecycle.js entirely, this test should be relaxed / removed.
    assert.ok(
      /SHELL-BARE:/.test(SOURCE),
      "at least one SHELL-BARE marker (the browser panel) should live in window-lifecycle.js",
    );
  });

  it("every `defaultFontSize:` seed sits inside a createShell* helper — the seeded ↔ rippled invariant", () => {
    // Complementary check: every construction-time `defaultFontSize`
    // seed's containing block must be either a createShell* factory or
    // the wrapper itself. Right now the invariant is enforced by the
    // simpler check "bare `new BrowserWindow/WebContentsView` is
    // forbidden without SHELL-BARE" above; this test walks the source
    // to confirm no seed accidentally lives inside a SHELL-BARE block.
    const lines = SOURCE.split("\n");
    const offenders = [];
    for (let i = 0; i < lines.length; i++) {
      if (!/defaultFontSize:/.test(lines[i])) continue;
      // Walk back up to 20 lines to find the containing `new … (` call.
      let container = null;
      for (let j = i; j >= Math.max(0, i - 20); j--) {
        const m = lines[j].match(/(new (?:BrowserWindow|WebContentsView)\(|createShell(?:BrowserWindow|WebContentsView)\()/);
        if (m) { container = { line: j, ctor: m[1] }; break; }
      }
      if (!container) continue; // seed with no obvious container — skip
      if (/^createShell/.test(container.ctor)) continue; // OK
      // A bare `new BrowserWindow/WebContentsView(` container is only OK if
      // it carries a SHELL-BARE marker — but that would mean seeding a
      // wc we're deliberately excluding, which is contradictory. Flag it.
      const ctorLine = lines[container.line];
      const prevLine = container.line > 0 ? lines[container.line - 1] : "";
      if (/SHELL-BARE/.test(ctorLine) || /SHELL-BARE/.test(prevLine)) {
        offenders.push(`  L${i + 1}: defaultFontSize seed inside SHELL-BARE container at L${container.line + 1} — contradiction (either drop the SHELL-BARE or drop the seed)`);
      }
    }
    assert.strictEqual(
      offenders.length,
      0,
      `Seed-vs-marker contradictions:\n${offenders.join("\n")}`,
    );
  });
});
