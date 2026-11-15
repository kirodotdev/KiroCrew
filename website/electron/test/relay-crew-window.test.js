const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const {
  createTokenHolder,
  relayPartitionName,
  installRelayHeaderRule,
  createRelayCrewView,
  openRelayCrewWindow,
} = require("../relay-crew-window");

const ENDPOINT = "abcdefghij0123456789";
const CREW_URL = `https://${ENDPOINT}.relay.example.test`;
const ORIGIN = CREW_URL;
const TOKEN = "tok-SECRET-0123456789abcdef";

function fakeSession() {
  const ses = {
    partitions: [],
    headerRule: null,
    completedRule: null,
    errorRule: null,
    permissionRequest: null,
    permissionCheck: null,
    cleared: 0,
    webRequest: {
      onBeforeSendHeaders(...args) {
        const listener = args[args.length - 1];
        ses.headerRule = listener === null ? null : { args, listener };
      },
      onCompleted(filter, listener) {
        ses.completedRule = filter === null ? null : { filter, listener };
      },
      onErrorOccurred(filter, listener) {
        ses.errorRule = filter === null ? null : { filter, listener };
      },
    },
    userAgent: "Mozilla/5.0 Electron/44",
    getUserAgent() { return ses.userAgent; },
    setUserAgent(ua) { ses.userAgent = ua; },
    setPermissionRequestHandler(fn) { ses.permissionRequest = fn; },
    setPermissionCheckHandler(fn) { ses.permissionCheck = fn; },
    clearStorageData() { ses.cleared += 1; return Promise.resolve(); },
    clearCache() { return Promise.resolve(); },
  };
  return ses;
}

function fakeDeps() {
  const ses = fakeSession();
  const logs = [];
  const opened = [];
  const views = [];
  const windows = [];
  class WebContentsView {
    constructor(options) {
      this.options = options;
      this.webContents = new EventEmitter();
      this.webContents.loaded = [];
      this.webContents.reloads = 0;
      this.webContents.loadURL = (url) => { this.webContents.loaded.push(url); return Promise.resolve(); };
      this.webContents.files = [];
      this.webContents.loadFile = (file, opts) => { this.webContents.files.push({ file, opts }); return Promise.resolve(); };
      this.webContents.closed = false;
      this.webContents.ipc = new EventEmitter();
      this.webContents.reload = () => { this.webContents.reloads += 1; };
      this.webContents.close = () => { this.webContents.closed = true; };
      this.webContents.setWindowOpenHandler = (fn) => { this.webContents.openHandler = fn; };
      this.setBounds = (b) => { this.bounds = b; };
      views.push(this);
    }
  }
  class BaseWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.destroyed = false;
      this.focused = 0;
      this.minimized = false;
      this.calls = [];
      this.children = [];
      this.contentView = {
        addChildView: (v) => { this.child = v; this.children.push(v); },
        removeChildView: (v) => { this.children = this.children.filter((c) => c !== v); },
      };
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    getContentBounds() { return { width: 1000, height: 700 }; }
    isMinimized() { return this.minimized; }
    restore() { this.calls.push("restore"); this.minimized = false; this.emit("restore"); }
    show() { this.calls.push("show"); this.emit("show"); }
    focus() { this.calls.push("focus"); this.focused += 1; }
    close() { this.destroyed = true; this.emit("closed"); }
  }
  return {
    ses, logs, opened, views, windows,
    deps: {
      WebContentsView,
      BaseWindow,
      session: { fromPartition: (name) => { ses.partitions.push(name); return ses; } },
      shell: { openExternal: (url) => { opened.push(url); return Promise.resolve(); } },
      log: (m) => logs.push(m),
    },
  };
}

function send(ses, url, requestHeaders) {
  let out;
  ses.headerRule.listener({ url, requestHeaders }, (r) => { out = r.requestHeaders; });
  return out;
}

test("header: added for the exact origin on https and wss", () => {
  const ses = fakeSession();
  installRelayHeaderRule(ses, ORIGIN, () => TOKEN);
  assert.strictEqual(send(ses, `${ORIGIN}/api/x`, {})["Kiro-Relay-Authorization"], `Bearer ${TOKEN}`);
  assert.strictEqual(
    send(ses, `wss://${ENDPOINT}.relay.example.test/ws`, {})["Kiro-Relay-Authorization"],
    `Bearer ${TOKEN}`,
  );
});

test("header: a page-set copy is replaced and Authorization is untouched", () => {
  const ses = fakeSession();
  installRelayHeaderRule(ses, ORIGIN, () => TOKEN);
  const out = send(ses, `${ORIGIN}/`, {
    "kiro-relay-authorization": "Bearer page",
    "KIRO-RELAY-AUTHORIZATION": "Bearer page2",
    Authorization: "Bearer crew-own",
  });
  assert.deepStrictEqual(out, {
    "Kiro-Relay-Authorization": `Bearer ${TOKEN}`,
    Authorization: "Bearer crew-own",
  });
});

test("header: the listener has no URL filter, so a redirect off the origin is still stripped", () => {
  const ses = fakeSession();
  installRelayHeaderRule(ses, ORIGIN, () => TOKEN);
  assert.strictEqual(ses.headerRule.args.length, 1);
  // A request redirected to another origin arrives carrying the earlier copy.
  const out = send(ses, "https://elsewhere.example.test/landed", {
    "Kiro-Relay-Authorization": `Bearer ${TOKEN}`,
  });
  assert.deepStrictEqual(out, {});
});

test("header: other origins get no token, and a page-set copy is still dropped", () => {
  const ses = fakeSession();
  installRelayHeaderRule(ses, ORIGIN, () => TOKEN);
  for (const url of [
    `https://other${ENDPOINT}.relay.example.test/`,
    `${ORIGIN}:8443/`,
    `http://${ENDPOINT}.relay.example.test/`,
    `ws://${ENDPOINT}.relay.example.test/ws`,
    "not a url",
  ]) {
    const out = send(ses, url, { "Kiro-Relay-Authorization": "Bearer page", Accept: "*/*" });
    assert.deepStrictEqual(out, { Accept: "*/*" }, url);
  }
});

test("header: no token, a throwing provider or a non-string sends none", () => {
  for (const getToken of [() => null, () => "", () => 42, () => { throw new Error("x"); }]) {
    const ses = fakeSession();
    installRelayHeaderRule(ses, ORIGIN, getToken);
    assert.deepStrictEqual(send(ses, `${ORIGIN}/`, { "Kiro-Relay-Authorization": "Bearer page" }), {});
  }
});

test("header: each request reads the newest token from the holder", () => {
  const ses = fakeSession();
  const holder = createTokenHolder();
  installRelayHeaderRule(ses, ORIGIN, holder.current);
  holder.set("first");
  assert.strictEqual(send(ses, `${ORIGIN}/`, {})["Kiro-Relay-Authorization"], "Bearer first");
  holder.set("second");
  assert.strictEqual(send(ses, `${ORIGIN}/`, {})["Kiro-Relay-Authorization"], "Bearer second");
  holder.clear();
  assert.deepStrictEqual(send(ses, `${ORIGIN}/`, {}), {});
});

test("header rule: refuses a non-https origin", () => {
  assert.throws(() => installRelayHeaderRule(fakeSession(), "http://x.test", () => TOKEN));
});

test("view: in-memory partition, hardened prefs, no preload, permissions denied", () => {
  const f = fakeDeps();
  const { view, origin } = createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  assert.strictEqual(origin, ORIGIN);
  assert.strictEqual(relayPartitionName(ENDPOINT), `relay-crew-${ENDPOINT}`);
  assert.deepStrictEqual(f.ses.partitions, [`relay-crew-${ENDPOINT}`]);
  assert.ok(!f.ses.partitions[0].startsWith("persist:"));
  assert.deepStrictEqual(view.options.webPreferences, {
    partition: `relay-crew-${ENDPOINT}`,
    sandbox: true,
    contextIsolation: true,
    nodeIntegration: false,
    webviewTag: false,
  });
  assert.ok(!("preload" in view.options.webPreferences));
  let granted = null;
  f.ses.permissionRequest(null, "media", (ok) => { granted = ok; });
  assert.strictEqual(granted, false);
  assert.strictEqual(f.ses.permissionCheck(null, "notifications"), false);
});

test("view: refuses a bad endpoint ID or a Crew URL that isn't its bare origin", () => {
  const f = fakeDeps();
  for (const [endpointId, crewUrl] of [
    ["short", CREW_URL],
    [ENDPOINT.toUpperCase(), CREW_URL],
    ["a".repeat(64), `https://${"a".repeat(64)}.relay.example.test`],
    ["abcdefghij.123456789", CREW_URL],
    [Array(20).fill("a"), CREW_URL],
    [ENDPOINT, `http://${ENDPOINT}.relay.example.test`],
    [ENDPOINT, `${CREW_URL}/path`],
    [ENDPOINT, `${CREW_URL}/?q=1`],
    [ENDPOINT, `https://user@${ENDPOINT}.relay.example.test`],
    [ENDPOINT, "https://evil.example.test"],
    [ENDPOINT, `https://evil.example.test/${ENDPOINT}.`],
    [ENDPOINT, "nope"],
  ]) {
    assert.throws(
      () => createRelayCrewView({ endpointId, crewUrl, getToken: () => TOKEN }, f.deps),
      /invalid endpoint or crew URL/,
      `${endpointId} ${crewUrl}`,
    );
  }
});

test("view: off-origin navigation and redirects are denied, same origin allowed", () => {
  const f = fakeDeps();
  const { view } = createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  const nav = (url) => {
    let prevented = false;
    view.webContents.emit("will-navigate", { preventDefault: () => { prevented = true; } }, url);
    return prevented;
  };
  assert.strictEqual(nav(`${ORIGIN}/chat`), false);
  assert.strictEqual(nav("https://evil.example.test/"), true);
  assert.strictEqual(nav("file:///etc/passwd"), true);
  let redirected = false;
  view.webContents.emit("will-redirect", {
    url: "https://evil.example.test/", isMainFrame: true, preventDefault: () => { redirected = true; },
  });
  assert.strictEqual(redirected, true);
});

test("view: window opens are denied and other web origins go to the system browser", () => {
  const f = fakeDeps();
  const { view } = createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  const open = view.webContents.openHandler;
  assert.deepStrictEqual(open({ url: "https://docs.example.test/a" }), { action: "deny" });
  assert.deepStrictEqual(open({ url: `${ORIGIN}/popup` }), { action: "deny" });
  assert.deepStrictEqual(open({ url: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" }), { action: "deny" });
  assert.deepStrictEqual(open({ url: "javascript:alert(1)" }), { action: "deny" });
  assert.deepStrictEqual(f.opened, ["https://docs.example.test/a"]);
});

test("window: one per Crew, loads the origin, reports main-frame status, cleans up on close", async () => {
  const f = fakeDeps();
  const opts = { endpointId: ENDPOINT, crewUrl: CREW_URL, title: "Team Crew", getToken: () => TOKEN };
  const handle = openRelayCrewWindow(opts, f.deps);
  assert.strictEqual(openRelayCrewWindow(opts, f.deps), handle);
  assert.strictEqual(f.windows.length, 1);
  assert.strictEqual(f.windows[0].focused, 1);
  assert.deepStrictEqual(f.views[0].webContents.loaded, [`${ORIGIN}/`]);
  assert.deepStrictEqual(f.views[0].bounds, { x: 0, y: 0, width: 1000, height: 700 });

  const statuses = [];
  handle.onMainFrameStatus((code) => statuses.push(code));
  f.ses.completedRule.listener({ resourceType: "xhr", statusCode: 401 });
  f.ses.completedRule.listener({ resourceType: "mainFrame", statusCode: 401 });
  assert.deepStrictEqual(statuses, [401]);
  handle.reload();
  assert.strictEqual(f.views[0].webContents.reloads, 1);

  let closedCalls = 0;
  handle.onClosed(() => { closedCalls += 1; });
  handle.close();
  handle.close();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(closedCalls, 1);
  assert.strictEqual(handle.isClosed(), true);
  assert.strictEqual(f.ses.headerRule, null);
  assert.strictEqual(f.ses.completedRule, null);
  assert.strictEqual(f.ses.cleared, 1);

  const again = openRelayCrewWindow(opts, f.deps);
  assert.notStrictEqual(again, handle);
  again.close();
});

test("window: visibility changes report hidden on minimize and hide, shown on restore and show", () => {
  const f = fakeDeps();
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  const seen = [];
  handle.onVisibilityChange((hidden) => seen.push(hidden));
  for (const event of ["minimize", "restore", "hide", "show"]) f.windows[0].emit(event);
  assert.deepStrictEqual(seen, [true, false, true, false]);
  handle.close();
  f.windows[0].emit("show");
  assert.deepStrictEqual(seen, [true, false, true, false]);
});

test("window: focus restores a minimized window, then shows and focuses it", () => {
  const f = fakeDeps();
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  const w = f.windows[0];
  const seen = [];
  handle.onVisibilityChange((hidden) => seen.push(hidden));
  w.minimized = true;
  w.emit("minimize");
  handle.focus();
  assert.deepStrictEqual(w.calls, ["restore", "show", "focus"]);
  assert.strictEqual(seen[seen.length - 1], false, "the window reports shown");
  handle.close();
});

test("window: focus shows a hidden, not minimized window without restoring it", () => {
  const f = fakeDeps();
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  const w = f.windows[0];
  w.emit("hide");
  handle.focus();
  assert.deepStrictEqual(w.calls, ["show", "focus"]);
  handle.close();
});

test("logs: never carry the token", () => {
  const f = fakeDeps();
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  send(f.ses, `${ORIGIN}/`, {});
  f.ses.completedRule.listener({ resourceType: "mainFrame", statusCode: 401 });
  f.views[0].webContents.emit("will-navigate", { preventDefault() {} }, "https://evil.example.test/");
  f.views[0].webContents.openHandler({ url: "javascript:x" });
  handle.close();
  assert.ok(f.logs.length >= 4);
  for (const line of f.logs) assert.ok(!line.includes(TOKEN), line);
});

// --- Reachability and the window's own notice ---

const { UNREACHABLE_AFTER_MS } = require("../relay-crew-reachability");

function openWithClock() {
  const f = fakeDeps();
  let t = 5_000_000;
  f.deps.now = () => t;
  const advance = (ms) => { t += ms; };
  const handle = openRelayCrewWindow(
    { endpointId: ENDPOINT, crewUrl: CREW_URL, title: "Team Crew", getToken: () => TOKEN },
    f.deps,
  );
  const unreachable = [];
  handle.onUnreachable(() => unreachable.push(t));
  const urlFor = (path, resourceType) =>
    `${resourceType === "webSocket" ? ORIGIN.replace(/^https:/, "wss:") : ORIGIN}${path}`;
  const completed = (path, statusCode, resourceType = "xhr") =>
    f.ses.completedRule.listener({ url: urlFor(path, resourceType), resourceType, statusCode });
  const failed = (path, error = "net::ERR_CONNECTION_CLOSED", resourceType = "xhr") =>
    f.ses.errorRule.listener({ url: urlFor(path, resourceType), resourceType, error });
  return { f, handle, unreachable, advance, completed, failed, crewView: f.views[0] };
}

// The page's pace while disconnected: a status probe every 3 s and a socket
// attempt every 10 s once its backoff has reached the ceiling.
function outage(w, ms, { statusAnswer = 502 } = {}) {
  for (let elapsed = 0; elapsed < ms; elapsed += 3000) {
    w.advance(3000);
    w.completed("/api/status", statusAnswer);
    if (elapsed % 9000 === 0) w.failed("/api/ws", "net::ERR_CONNECTION_FAILED", "webSocket");
  }
}

test("window: the Crew's own session carries a relay-window marker in its user agent", () => {
  const f = fakeDeps();
  createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  assert.strictEqual(f.ses.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow");
  createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, getToken: () => TOKEN }, f.deps);
  assert.strictEqual(f.ses.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow", "added once");
});

test("window: a host that stays gone is reported unreachable once the cap runs out, and only once", () => {
  const w = openWithClock();
  const wss = `wss://${ENDPOINT}.relay.example.test/*`;
  assert.deepStrictEqual(w.f.ses.completedRule.filter.urls, [`${ORIGIN}/*`, wss], "the socket's wss: upgrade is watched too");
  assert.deepStrictEqual(w.f.ses.errorRule.filter.urls, [`${ORIGIN}/*`, wss]);
  outage(w, UNREACHABLE_AFTER_MS - 6000);
  assert.deepStrictEqual(w.unreachable, [], "still inside the cap");
  outage(w, 30_000);
  assert.strictEqual(w.unreachable.length, 1);
  outage(w, 60_000);
  assert.strictEqual(w.unreachable.length, 1, "once per cycle");
  w.handle.close();
});

test("window: a blip that recovers inside the cap stays quiet", () => {
  const w = openWithClock();
  outage(w, 60_000);
  w.completed("/api/status", 200);
  outage(w, 60_000);
  w.completed("/api/ws", 101, "webSocket");
  outage(w, 60_000);
  assert.deepStrictEqual(w.unreachable, []);
  assert.deepStrictEqual(w.crewView.webContents.loaded, [`${ORIGIN}/`], "the page is never touched");
  w.handle.close();
});

test("window: a refused upgrade or no-token 401 counts, other paths and aborted requests do not", () => {
  const w = openWithClock();
  for (let i = 0; i < 100; i += 1) {
    w.advance(3000);
    w.completed("/api/slots", 502);
    w.failed("/api/status", "net::ERR_ABORTED");
  }
  assert.deepStrictEqual(w.unreachable, [], "only the two retry loops count, and a cancel is not a failure");
  outage(w, UNREACHABLE_AFTER_MS + 6000, { statusAnswer: 401 });
  assert.strictEqual(w.unreachable.length, 1, "a page left with no token is also bounded");
  w.handle.close();
});

test("window: a main-frame load the relay answers 5xx or that fails is reported at once", () => {
  for (const hit of [
    (w) => w.completed("/", 502, "mainFrame"),
    (w) => w.failed("/", "net::ERR_CONNECTION_RESET", "mainFrame"),
  ]) {
    const w = openWithClock();
    hit(w);
    assert.strictEqual(w.unreachable.length, 1);
    w.handle.close();
  }
  const w = openWithClock();
  w.completed("/", 401, "mainFrame");
  w.failed("/", "net::ERR_ABORTED", "mainFrame");
  assert.deepStrictEqual(w.unreachable, [], "a 401 is renewal's, an abort is a cancel");
  w.handle.close();
});

test("window: a notice unloads the Crew's page and shows a local, locked-down view", () => {
  const w = openWithClock();
  const text = { title: "Example Crew is currently unavailable", body: "", check: "Check again", close: "Close window" };
  w.handle.showNotice(text);
  assert.deepStrictEqual(w.crewView.webContents.loaded, [`${ORIGIN}/`, "about:blank"], "the page's socket and probe stop with it");
  const notice = w.f.views[1];
  assert.ok(notice, "a second view");
  const prefs = notice.options.webPreferences;
  assert.strictEqual(prefs.sandbox, true);
  assert.strictEqual(prefs.contextIsolation, true);
  assert.strictEqual(prefs.nodeIntegration, false);
  assert.match(prefs.preload, /relay-crew-notice-preload\.js$/);
  assert.notStrictEqual(prefs.partition, `relay-crew-${ENDPOINT}`, "never the Crew's own session");
  assert.deepStrictEqual(notice.webContents.files.map((x) => x.opts.query), [text]);
  assert.match(notice.webContents.files[0].file, /relay-crew-notice\.html$/);
  assert.deepStrictEqual(notice.bounds, { x: 0, y: 0, width: 1000, height: 700 });
  assert.ok(w.f.windows[0].children.includes(notice));

  let prevented = false;
  notice.webContents.emit("will-navigate", { preventDefault: () => { prevented = true; } }, "https://example.test/");
  assert.strictEqual(prevented, true);
  assert.deepStrictEqual(notice.webContents.openHandler({ url: "https://example.test/" }), { action: "deny" });

  const actions = [];
  w.handle.onNoticeAction((a) => actions.push(a));
  notice.webContents.ipc.emit("relay-crew-notice:act", {}, "check");
  notice.webContents.ipc.emit("relay-crew-notice:act", {}, "close");
  notice.webContents.ipc.emit("relay-crew-notice:act", {}, "anything-else");
  assert.deepStrictEqual(actions, ["check", "close"]);

  // Showing again replaces the text in the same view.
  w.handle.showNotice({ ...text, title: "again" });
  assert.strictEqual(w.f.views.length, 2);
  assert.strictEqual(notice.webContents.files.length, 2);
  w.handle.close();
  assert.strictEqual(notice.webContents.closed, true, "closing the window closes the notice too");
});

test("window: reload after a notice removes it and loads the Crew again, with a fresh cap", () => {
  const w = openWithClock();
  outage(w, UNREACHABLE_AFTER_MS + 6000);
  assert.strictEqual(w.unreachable.length, 1);
  w.handle.showNotice({ title: "t", body: "", check: "Check again", close: "Close window" });
  const notice = w.f.views[1];
  w.handle.reload();
  assert.strictEqual(w.f.windows[0].children.includes(notice), false);
  assert.strictEqual(notice.webContents.closed, true);
  assert.deepStrictEqual(w.crewView.webContents.loaded, [`${ORIGIN}/`, "about:blank", `${ORIGIN}/`]);
  assert.strictEqual(w.crewView.webContents.reloads, 0, "a load of the origin, not a reload of the blank page");
  outage(w, UNREACHABLE_AFTER_MS + 6000);
  assert.strictEqual(w.unreachable.length, 2, "the next outage trips again");
  w.handle.close();
});
