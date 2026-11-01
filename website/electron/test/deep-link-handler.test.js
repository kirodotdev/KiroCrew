const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { createDeepLinkHandler, installDeepLinks, installNavigateReady } = require("../deep-link-handler");

function fakeDeliver(accepts = { value: true }) {
  const calls = [];
  const fn = (route) => {
    calls.push(route);
    return accepts.value;
  };
  return { fn, calls };
}

describe("createDeepLinkHandler", () => {
  it("applies a session link at once when the current document has acked", () => {
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    h.navigateReady();
    assert.equal(h.handleUrl("kirocrew://chat?sid=cos-quick-chat"), true);
    assert.deepEqual(calls, ["/chat?sid=cos-quick-chat"]);
    assert.equal(h.pending(), null);
  });

  it("a link flushed before the ack stays pending and is not delivered", () => {
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    assert.equal(h.handleUrl("kirocrew://chat?sid=a"), false);
    assert.equal(h.pending(), "/chat?sid=a");
    // A flush with no ack (what `did-finish-load` would have done) keeps it.
    assert.equal(h.flushPending(), false);
    assert.equal(h.pending(), "/chat?sid=a");
    assert.deepEqual(calls, [], "nothing is sent before the listener exists");
  });

  it("the ack flushes a held link exactly once", () => {
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    h.handleUrl("kirocrew://chat?sid=a");
    assert.equal(h.navigateReady(), true);
    assert.equal(h.pending(), null);
    assert.equal(h.flushPending(), false);
    assert.equal(h.navigateReady(), false, "a repeated ack has nothing left to send");
    assert.deepEqual(calls, ["/chat?sid=a"]);
  });

  it("a new main-frame navigation re-arms the wait", () => {
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    h.navigateReady();
    h.navigationStarted();
    assert.equal(h.handleUrl("kirocrew://chat?sid=b"), false);
    assert.equal(h.flushPending(), false);
    assert.deepEqual(calls, []);
    assert.equal(h.navigateReady(), true);
    assert.deepEqual(calls, ["/chat?sid=b"]);
  });

  it("an acked document that cannot take the route keeps the link held", () => {
    // e.g. the ack came from a page that is not the dashboard origin.
    const accepts = { value: false };
    const { fn, calls } = fakeDeliver(accepts);
    const h = createDeepLinkHandler({ deliver: fn });
    h.navigateReady();
    assert.equal(h.handleUrl("kirocrew://chat?sid=c"), false);
    assert.equal(h.pending(), "/chat?sid=c");
    accepts.value = true;
    h.navigationStarted();
    assert.equal(h.navigateReady(), true);
    assert.deepEqual(calls, ["/chat?sid=c", "/chat?sid=c"]);
    assert.equal(h.pending(), null);
  });

  it("a later link replaces a held one", () => {
    const { fn } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    h.handleUrl("kirocrew://chat?sid=first");
    h.handleUrl("kirocrew://chat?sid=second");
    assert.equal(h.pending(), "/chat?sid=second");
  });

  it("a bare kirocrew:// raises at once, before any ack, and supersedes a held link", () => {
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    h.handleUrl("kirocrew://chat?sid=held");
    assert.equal(h.handleUrl("kirocrew://"), true);
    assert.deepEqual(calls, [""]);
    assert.equal(h.pending(), null);
  });

  it("a refused URL opens nothing and holds nothing, and the log names the reason not the payload", () => {
    const { fn, calls } = fakeDeliver();
    const logs = [];
    const h = createDeepLinkHandler({ deliver: fn, log: (m) => logs.push(m) });
    h.navigateReady();
    const payload = "kirocrew://chat?sid=x&token=SECRETVALUE";
    assert.equal(h.handleUrl(payload), false);
    assert.deepEqual(calls, []);
    assert.equal(h.pending(), null);
    assert.equal(logs.length, 1);
    assert.match(logs[0], /unknown query parameter/);
    assert.ok(!logs[0].includes("SECRETVALUE"));
  });

  it("handleArgv ignores an argv without a link", () => {
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    h.navigateReady();
    assert.equal(h.handleArgv(["bin", "--flag"]), false);
    assert.equal(h.handleArgv(["bin", "kirocrew://chat?sid=warm"]), true);
    assert.deepEqual(calls, ["/chat?sid=warm"]);
  });

  it("a cold-start argv link is held until the dashboard acks", () => {
    // Windows/Linux: the URL that launched the first instance is in its own
    // process.argv, read before any window exists.
    const { fn, calls } = fakeDeliver();
    const h = createDeepLinkHandler({ deliver: fn });
    assert.equal(h.handleArgv(["C:\\Program Files\\KiroCrew\\KiroCrew.exe", "kirocrew://chat?sid=cold"]), false);
    assert.equal(h.pending(), "/chat?sid=cold");
    assert.deepEqual(calls, []);
    h.navigateReady();
    assert.deepEqual(calls, ["/chat?sid=cold"]);
    assert.equal(h.pending(), null);
  });
});

describe("installNavigateReady", () => {
  function harness() {
    const app = new EventEmitter();
    const ipcMain = new EventEmitter();
    const dashboard = new EventEmitter();
    const other = new EventEmitter();
    const { fn, calls } = fakeDeliver();
    const handler = createDeepLinkHandler({ deliver: fn });
    installNavigateReady({ app, ipcMain, handler, isDashboardContents: (c) => c === dashboard });
    app.emit("web-contents-created", {}, dashboard);
    app.emit("web-contents-created", {}, other);
    return { ipcMain, dashboard, other, handler, calls };
  }

  it("counts navigate-ready only from the dashboard view", () => {
    const { ipcMain, dashboard, other, handler, calls } = harness();
    handler.handleUrl("kirocrew://chat?sid=k");
    ipcMain.emit("navigate-ready", { sender: other });
    assert.deepEqual(calls, [], "another renderer's ack is ignored");
    ipcMain.emit("navigate-ready", { sender: dashboard });
    assert.deepEqual(calls, ["/chat?sid=k"]);
  });

  it("a main-frame cross-document navigation on the dashboard re-arms the wait", () => {
    const { ipcMain, dashboard, other, handler, calls } = harness();
    ipcMain.emit("navigate-ready", { sender: dashboard });
    // (event, url, isInPlace, isMainFrame)
    dashboard.emit("did-start-navigation", {}, "http://localhost:5476/chat", true, true);
    dashboard.emit("did-start-navigation", {}, "http://localhost:5476/pane", false, false);
    other.emit("did-start-navigation", {}, "http://localhost:5476/", false, true);
    assert.equal(handler.handleUrl("kirocrew://chat?sid=a"), true, "same-document, subframe and other views keep the ack");
    dashboard.emit("did-start-navigation", {}, "http://localhost:5476/?token=t", false, true);
    assert.equal(handler.handleUrl("kirocrew://chat?sid=b"), false, "a new document must ack again");
    ipcMain.emit("navigate-ready", { sender: dashboard });
    assert.deepEqual(calls, ["/chat?sid=a", "/chat?sid=b"]);
  });
});

describe("installDeepLinks", () => {
  function fakeApp({ registerResult = true, throws = false } = {}) {
    const app = new EventEmitter();
    app.registeredSchemes = [];
    app.setAsDefaultProtocolClient = (scheme) => {
      if (throws) throw new Error("boom");
      app.registeredSchemes.push(scheme);
      return registerResult;
    };
    return app;
  }

  it("registers the scheme and routes open-url on macOS", () => {
    const app = fakeApp();
    const { fn, calls } = fakeDeliver();
    const handler = createDeepLinkHandler({ deliver: fn });
    handler.navigateReady();
    const { registered } = installDeepLinks({ app, handler, platform: "darwin", defaultApp: false });
    assert.equal(registered, true);
    assert.deepEqual(app.registeredSchemes, ["kirocrew"]);
    let prevented = false;
    app.emit("open-url", { preventDefault: () => (prevented = true) }, "kirocrew://chat?sid=k");
    assert.equal(prevented, true);
    assert.deepEqual(calls, ["/chat?sid=k"]);
  });

  it("does not listen for open-url off macOS, and never attaches second-instance (main.js owns it)", () => {
    const app = fakeApp();
    const handler = createDeepLinkHandler({ deliver: () => true });
    installDeepLinks({ app, handler, platform: "win32" });
    assert.equal(app.listenerCount("open-url"), 0);
    assert.equal(app.listenerCount("second-instance"), 0);
    installDeepLinks({ app, handler, platform: "darwin" });
    assert.equal(app.listenerCount("second-instance"), 0);
  });

  it("a failed or throwing registration is logged and degraded, never thrown", () => {
    const logs = [];
    const handler = createDeepLinkHandler({ deliver: () => true });
    const r1 = installDeepLinks({ app: fakeApp({ registerResult: false }), handler, log: (m) => logs.push(m), platform: "linux", defaultApp: false });
    assert.equal(r1.registered, false);
    const r2 = installDeepLinks({ app: fakeApp({ throws: true }), handler, log: (m) => logs.push(m), platform: "linux", defaultApp: false });
    assert.equal(r2.registered, false);
    assert.equal(logs.length, 3);
  });

  it("registers the scheme when this is not a dev run", () => {
    const app = fakeApp();
    const logs = [];
    const handler = createDeepLinkHandler({ deliver: () => true });
    const { registered } = installDeepLinks({ app, handler, log: (m) => logs.push(m), platform: "win32", defaultApp: false });
    assert.equal(registered, true);
    assert.deepEqual(app.registeredSchemes, ["kirocrew"]);
    assert.deepEqual(logs, []);
  });

  it("a dev run (process.defaultApp) skips registration, logs one line, and still routes open-url", () => {
    // Without a path argument Electron registers process.execPath, which in a
    // dev run is the bare electron binary.
    const app = fakeApp({ throws: true });
    const logs = [];
    const { fn, calls } = fakeDeliver();
    const handler = createDeepLinkHandler({ deliver: fn });
    handler.navigateReady();
    const { registered } = installDeepLinks({ app, handler, log: (m) => logs.push(m), platform: "darwin", defaultApp: true });
    assert.equal(registered, false);
    assert.deepEqual(app.registeredSchemes, []);
    assert.equal(logs.length, 1);
    assert.match(logs[0], /process\.defaultApp/);
    app.emit("open-url", { preventDefault: () => {} }, "kirocrew://chat?sid=dev");
    assert.deepEqual(calls, ["/chat?sid=dev"]);
  });
});

describe("main.js wiring", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");

  it("routes a second-instance argv through the handler and keeps the show-window fallback", () => {
    // The registration lives in main.js so the GPU-policy ordering tests keep
    // their pin; this checks the shape those tests cannot: the argv reaches the
    // handler first and the old behaviour is the fallback, not replaced.
    assert.match(
      main,
      /app\.on\("second-instance", \(_event, argv\) => \{[\s\S]*?deepLinks\.handleArgv\(argv\)\) return;[\s\S]*?windows\?\.showMainWindow\(\{ focus: true \}\)/,
    );
  });

  it("reads the launching argv once, right after installing the handler and the ack wiring", () => {
    const install = main.indexOf("installDeepLinks({ app, handler: deepLinks");
    const ack = main.indexOf("installNavigateReady({", install);
    const argv = main.indexOf("deepLinks.handleArgv(process.argv);", ack);
    const secondInstance = main.indexOf('app.on("second-instance"', argv);
    assert.ok(install >= 0, "installDeepLinks is called");
    assert.ok(ack > install, "the navigate-ready wiring follows it");
    assert.ok(argv > ack, "the cold-start argv is read after both");
    assert.ok(secondInstance > argv, "and before second-instance is registered");
    assert.equal(main.split("deepLinks.handleArgv(process.argv)").length - 1, 1, "read exactly once");
  });

  it("delivers through the Crew Companion's origin check, with no predicate of its own", () => {
    const start = main.indexOf("function deliverDeepLink(");
    const end = main.indexOf("\n}\n", start);
    assert.ok(start >= 0 && end > start, "deliverDeepLink is defined");
    const body = main.slice(start, end);
    assert.match(body, /hostsDashboard\(wc, BACKEND_URL\)/);
    assert.doesNotMatch(body, /https\?/, "no second URL-shape readiness test");
    assert.doesNotMatch(main, /did-finish-load[\s\S]{0,200}flushPending/, "the ack, not did-finish-load, releases a held link");
    const lifecycle = fs.readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");
    assert.doesNotMatch(lifecycle, /openDashboardRoute/);
  });
});
