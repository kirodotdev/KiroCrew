const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const {
  createTokenHolder,
  relayPartitionName,
  installRelayHeaderRule,
  createRelayCrewView,
  openRelayCrewWindow,
  openRelayCrewPane,
  paneChordIndex,
} = require("../relay-crew-window");

const ENDPOINT = "abcdefghij0123456789";
const DOMAIN = "relay.example.test";
const CREW_URL = `https://${ENDPOINT}.${DOMAIN}`;
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
      this.webContents.setUserAgent = (ua) => { this.webContents.userAgent = ua; };
      this.webContents.hasFocus = false;
      this.webContents.isFocused = () => this.webContents.hasFocus;
      this.webContents.focus = () => { this.webContents.hasFocus = true; };
      this.webContents.isDestroyed = () => this.webContents.closed;
      this.visible = true;
      this.setVisible = (v) => { this.visible = v; };
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
    isFocused() { return this.windowFocused !== false; }
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
  const { view, origin } = createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
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
      () => createRelayCrewView({ endpointId, crewUrl, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps),
      /invalid endpoint or crew URL/,
      `${endpointId} ${crewUrl}`,
    );
  }
});

test("view: trusts only exactly https://<endpointId>.<relayDomain> on the default port", () => {
  const f = fakeDeps();
  for (const [crewUrl, relayDomain] of [
    // The endpoint ID as a prefix of a host the relay does not own.
    [`https://${ENDPOINT}.evil.example`, DOMAIN],
    [`https://${ENDPOINT}.${DOMAIN}.evil.example`, DOMAIN],
    [`https://${ENDPOINT}x.${DOMAIN}`, DOMAIN],
    [`https://x.${ENDPOINT}.${DOMAIN}`, DOMAIN],
    [`https://${ENDPOINT}.evil${DOMAIN}`, DOMAIN],
    [`https://${ENDPOINT}.${DOMAIN}:8443`, DOMAIN],
    [`wss://${ENDPOINT}.${DOMAIN}`, DOMAIN],
    // No relay domain configured: nothing is trusted.
    [CREW_URL, ""],
    [CREW_URL, undefined],
    [CREW_URL, null],
  ]) {
    assert.throws(
      () => createRelayCrewView({ endpointId: ENDPOINT, crewUrl, relayDomain, getToken: () => TOKEN }, f.deps),
      /invalid endpoint or crew URL/,
      `${crewUrl} ${relayDomain}`,
    );
  }
  // The explicit default port is the same origin.
  const { origin } = createRelayCrewView(
    { endpointId: ENDPOINT, crewUrl: `https://${ENDPOINT}.${DOMAIN}:443/`, relayDomain: DOMAIN, getToken: () => TOKEN },
    f.deps,
  );
  assert.strictEqual(origin, ORIGIN);
});

test("view: off-origin navigation and redirects are denied, same origin allowed", () => {
  const f = fakeDeps();
  const { view } = createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
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
  const { view } = createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
  const open = view.webContents.openHandler;
  assert.deepStrictEqual(open({ url: "https://docs.example.test/a" }), { action: "deny" });
  assert.deepStrictEqual(open({ url: `${ORIGIN}/popup` }), { action: "deny" });
  assert.deepStrictEqual(open({ url: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" }), { action: "deny" });
  assert.deepStrictEqual(open({ url: "javascript:alert(1)" }), { action: "deny" });
  assert.deepStrictEqual(f.opened, ["https://docs.example.test/a"]);
});

test("window: one per Crew, loads the origin, reports main-frame status, cleans up on close", async () => {
  const f = fakeDeps();
  const opts = { endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, title: "Team Crew", getToken: () => TOKEN };
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
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
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
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
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
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
  const w = f.windows[0];
  w.emit("hide");
  handle.focus();
  assert.deepStrictEqual(w.calls, ["show", "focus"]);
  handle.close();
});

test("logs: never carry the token", () => {
  const f = fakeDeps();
  const handle = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
  send(f.ses, `${ORIGIN}/`, {});
  f.ses.completedRule.listener({ resourceType: "mainFrame", statusCode: 401 });
  f.views[0].webContents.emit("will-navigate", { preventDefault() {} }, "https://evil.example.test/");
  f.views[0].webContents.openHandler({ url: "javascript:x" });
  handle.close();
  assert.ok(f.logs.length >= 4);
  for (const line of f.logs) assert.ok(!line.includes(TOKEN), line);
});

// --- Reachability and the window's own notice ---

const { UNREACHABLE_AFTER_MS, HOST_GONE_AFTER_MS } = require("../relay-crew-reachability");

function openWithClock() {
  const f = fakeDeps();
  let t = 5_000_000;
  f.deps.now = () => t;
  const advance = (ms) => { t += ms; };
  const handle = openRelayCrewWindow(
    { endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, title: "Team Crew", getToken: () => TOKEN },
    f.deps,
  );
  const unreachable = [];
  handle.onUnreachable(() => unreachable.push(t));
  const urlFor = (path, resourceType) =>
    `${resourceType === "webSocket" ? ORIGIN.replace(/^https:/, "wss:") : ORIGIN}${path}`;
  const completed = (path, statusCode, resourceType = "xhr", responseHeaders = undefined) =>
    f.ses.completedRule.listener({ url: urlFor(path, resourceType), resourceType, statusCode, responseHeaders });
  const failed = (path, error = "net::ERR_CONNECTION_CLOSED", resourceType = "xhr") =>
    f.ses.errorRule.listener({ url: urlFor(path, resourceType), resourceType, error });
  return { f, handle, unreachable, advance, completed, failed, crewView: f.views[0] };
}

// The page's pace while disconnected: a status probe every 3 s and a socket
// attempt every 10 s once its backoff has reached the ceiling.
function outage(w, ms, { statusAnswer = 502, headers = undefined, socketAccepted = false } = {}) {
  for (let elapsed = 0; elapsed < ms; elapsed += 3000) {
    w.advance(3000);
    w.completed("/api/status", statusAnswer, "xhr", headers);
    if (elapsed % 9000 !== 0) continue;
    // With no host, the relay accepts the upgrade (101) and then closes it 4404.
    if (socketAccepted) w.completed("/api/ws", 101, "webSocket");
    else w.failed("/api/ws", "net::ERR_CONNECTION_FAILED", "webSocket");
  }
}

// The relay's answer for a Crew it holds no host connection for (relay protocol
// v1, section 7: 503 with the typed reason header).
const CREW_NOT_CONNECTED = { "X-Kiro-Relay-Error": ["crew_not_connected"], "Retry-After": ["5"] };

test("window: the Crew's own session carries a relay-window marker in its user agent", () => {
  const f = fakeDeps();
  createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
  assert.strictEqual(f.ses.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow");
  createRelayCrewView({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
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
  w.completed("/api/slots", 200);
  outage(w, 60_000);
  assert.deepStrictEqual(w.unreachable, []);
  assert.deepStrictEqual(w.crewView.webContents.loaded, [`${ORIGIN}/`], "the page is never touched");
  w.handle.close();
});

test("window: a socket the relay accepts and then closes does not count as the host answering", () => {
  const w = openWithClock();
  outage(w, UNREACHABLE_AFTER_MS + 6000, { socketAccepted: true });
  assert.strictEqual(w.unreachable.length, 1);
  w.handle.close();
});

test("window: the relay saying it has no host connection ends the wait after the short bound", () => {
  const w = openWithClock();
  outage(w, HOST_GONE_AFTER_MS - 6000, { statusAnswer: 503, headers: CREW_NOT_CONNECTED, socketAccepted: true });
  assert.deepStrictEqual(w.unreachable, [], "a relay move can answer it for a moment");
  outage(w, 9000, { statusAnswer: 503, headers: CREW_NOT_CONNECTED, socketAccepted: true });
  assert.strictEqual(w.unreachable.length, 1);
  w.handle.close();
  // A plain 503 with no relay reason waits out the full cap.
  const plain = openWithClock();
  outage(plain, HOST_GONE_AFTER_MS + 30_000, { statusAnswer: 503 });
  assert.deepStrictEqual(plain.unreachable, []);
  plain.handle.close();
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

test("window: a reload the page's service worker answers with the relay's 503 is reported once", () => {
  // The worker's own fetch never reaches the network hook as a main frame, so
  // only did-navigate sees the status the document was served with.
  const w = openWithClock();
  const wc = w.crewView.webContents;
  wc.emit("did-start-navigation", {}, `${ORIGIN}/chat`, false, true);
  wc.emit("did-navigate", {}, `${ORIGIN}/chat`, 503);
  assert.strictEqual(w.unreachable.length, 1);
  assert.strictEqual(w.handle.showsError(), true, "opening it again reloads");
  w.handle.close();

  // Seen by both: one notice.
  const both = openWithClock();
  both.crewView.webContents.emit("did-start-navigation", {}, `${ORIGIN}/`, false, true);
  both.completed("/", 503, "mainFrame", CREW_NOT_CONNECTED);
  both.crewView.webContents.emit("did-navigate", {}, `${ORIGIN}/`, 503);
  assert.strictEqual(both.unreachable.length, 1, "a load the hook already reported is not reported again");
  both.handle.close();

  // The other order: did-navigate commits before the hook sees the body end.
  const navFirst = openWithClock();
  navFirst.crewView.webContents.emit("did-start-navigation", {}, `${ORIGIN}/`, false, true);
  navFirst.crewView.webContents.emit("did-navigate", {}, `${ORIGIN}/`, 503);
  navFirst.completed("/", 503, "mainFrame", CREW_NOT_CONNECTED);
  assert.strictEqual(navFirst.unreachable.length, 1, "did-navigate first, then the hook: one notice");
  navFirst.handle.close();

  // The notice's own about:blank load does not start a new report, so the
  // 503 document's did-navigate after it is the same load.
  const viaNotice = openWithClock();
  const wcn = viaNotice.crewView.webContents;
  viaNotice.handle.onUnreachable(() => {
    viaNotice.handle.showNotice({ title: "t", body: "", check: "Check again", close: "Close window" });
    wcn.emit("did-start-navigation", {}, "about:blank", false, true);
  });
  wcn.emit("did-start-navigation", {}, `${ORIGIN}/`, false, true);
  viaNotice.completed("/", 503, "mainFrame", CREW_NOT_CONNECTED);
  wcn.emit("did-navigate", {}, `${ORIGIN}/`, 503);
  assert.strictEqual(viaNotice.unreachable.length, 1, "a notice raised by the first report is not raised again");
  // A same-document navigation does not start a new load either.
  wcn.emit("did-start-navigation", {}, `${ORIGIN}/#x`, true, true);
  wcn.emit("did-navigate", {}, `${ORIGIN}/`, 503);
  assert.strictEqual(viaNotice.unreachable.length, 1);
  viaNotice.handle.close();

  // Another origin, or a healthy page, reports nothing.
  const ok = openWithClock();
  ok.crewView.webContents.emit("did-navigate", {}, "https://elsewhere.example.test/", 503);
  ok.crewView.webContents.emit("did-navigate", {}, `${ORIGIN}/`, 200);
  assert.deepStrictEqual(ok.unreachable, []);
  assert.strictEqual(ok.handle.showsError(), false);
  ok.handle.close();
});

test("window: a page the relay answered 4xx reads as an error until the Crew loads again", () => {
  const w = openWithClock();
  w.completed("/", 403, "mainFrame");
  assert.deepStrictEqual(w.unreachable, [], "a 4xx is not an outage");
  assert.strictEqual(w.handle.showsError(), true);
  w.handle.reload();
  assert.strictEqual(w.handle.showsError(), false, "a reload starts the page over");
  w.completed("/", 200, "mainFrame");
  assert.strictEqual(w.handle.showsError(), false);
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

// --- The pane: the same Crew view, inside the main window ---

const PANE_RECT = { x: 0, y: 42, width: 1000, height: 658 };
const PANE_NOTICE = { title: "t", body: "", check: "Check again", close: "Close" };

function openPane(f, extra = {}) {
  const parent = extra.parent || new f.deps.BaseWindow({});
  const dashboard = extra.dashboard || {
    webContents: Object.assign(new EventEmitter(), { focused: 0, focus() { this.focused += 1; }, isDestroyed: () => false }),
  };
  const handle = openRelayCrewPane({
    endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN,
    parent, dashboard, bounds: PANE_RECT, platform: "linux",
  }, f.deps);
  return { parent, dashboard, handle, view: f.views[f.views.length - 1] };
}

test("pane: built by the window's own view factory, so its isolation is the window's", () => {
  const probe = (view, ses) => {
    let offOrigin = false;
    view.webContents.emit("will-navigate", { preventDefault: () => { offOrigin = true; } }, "https://evil.example.test/");
    let redirected = false;
    view.webContents.emit("will-redirect", {
      url: "https://evil.example.test/", isMainFrame: true, preventDefault: () => { redirected = true; },
    });
    return {
      prefs: view.options.webPreferences,
      partitions: [...ses.partitions],
      guards: [view.webContents.listenerCount("will-navigate"), view.webContents.listenerCount("will-redirect")],
      offOrigin,
      redirected,
      popups: [`${ORIGIN}/popup`, "https://docs.example.test/a", "javascript:x"].map((url) => view.webContents.openHandler({ url })),
      permission: [ses.permissionCheck(null, "media"), ses.permissionCheck(null, "notifications")],
      header: send(ses, `${ORIGIN}/`, { "Kiro-Relay-Authorization": "Bearer page" }),
      headerElsewhere: send(ses, "https://elsewhere.example.test/", { "Kiro-Relay-Authorization": "Bearer page" }),
      sessionUserAgent: ses.userAgent,
    };
  };
  const fw = fakeDeps();
  const win = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, fw.deps);
  const fromWindow = probe(fw.views[0], fw.ses);
  win.close();
  const fp = fakeDeps();
  const p = openPane(fp);
  const fromPane = probe(p.view, fp.ses);
  p.handle.close();
  assert.deepStrictEqual(fromPane, fromWindow);
  assert.deepStrictEqual(fromPane.prefs, {
    partition: `relay-crew-${ENDPOINT}`, sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false,
  });
  assert.deepStrictEqual(fromPane.header, { "Kiro-Relay-Authorization": `Bearer ${TOKEN}` });
  assert.deepStrictEqual(fromPane.headerElsewhere, {});
  assert.deepStrictEqual(fp.opened, ["https://docs.example.test/a"]);
});

test("pane: its page's user agent carries both whole markers; the session keeps the window's only", () => {
  const f = fakeDeps();
  const p = openPane(f);
  assert.strictEqual(p.view.webContents.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane");
  assert.strictEqual(f.ses.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow");
  p.handle.close();
});

test("pane: a child of the main window at the given rect, loads the origin, one per Crew", () => {
  const f = fakeDeps();
  const p = openPane(f);
  assert.ok(p.parent.children.includes(p.view));
  assert.deepStrictEqual(p.view.bounds, PANE_RECT);
  assert.deepStrictEqual(p.view.webContents.loaded, [`${ORIGIN}/`]);
  assert.strictEqual(openPane(f, { parent: p.parent, dashboard: p.dashboard }).handle, p.handle);
  p.handle.setBounds({ x: 10, y: 50, width: 400, height: 300 });
  assert.deepStrictEqual(p.view.bounds, { x: 10, y: 50, width: 400, height: 300 });
  p.handle.setBounds({ x: 0, y: 0, width: 5000, height: 5000 });
  assert.deepStrictEqual(p.view.bounds, { x: 0, y: 0, width: 1000, height: 700 }, "clamped to the window");
  p.handle.setBounds({ x: "a" });
  assert.deepStrictEqual(p.view.bounds, { x: 0, y: 0, width: 1000, height: 700 }, "a bad rect is ignored");
  const unreachable = [];
  p.handle.onUnreachable(() => unreachable.push(1));
  f.ses.completedRule.listener({ resourceType: "mainFrame", statusCode: 502 });
  assert.strictEqual(unreachable.length, 1, "the window's reachability watch");
  p.handle.showNotice(PANE_NOTICE);
  const notice = f.views[1];
  assert.deepStrictEqual(notice.bounds, p.view.bounds);
  assert.ok(p.parent.children.includes(notice));
  assert.deepStrictEqual(p.view.webContents.loaded, [`${ORIGIN}/`, "about:blank"]);
  p.handle.close();
});

test("pane: refused while the same Crew is open in a window, whose partition it would share", () => {
  const f = fakeDeps();
  const win = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
  assert.throws(() => openPane(f), /already open in a window/);
  win.close();
  openPane(f).handle.close();
});

test("pane: hidden parks renewal as a minimized window does, and its notice hides with it", () => {
  const f = fakeDeps();
  const p = openPane(f);
  const seen = [];
  p.handle.onVisibilityChange((hidden) => seen.push(hidden));
  p.handle.setInactive(true);
  p.handle.setInactive(true);
  assert.strictEqual(p.view.visible, false);
  p.handle.showNotice(PANE_NOTICE);
  const notice = f.views[1];
  assert.strictEqual(notice.visible, false, "a notice shown while hidden stays hidden");
  p.handle.setInactive(false);
  assert.strictEqual(notice.visible, true);
  p.parent.emit("minimize");
  p.parent.emit("restore");
  p.handle.setInactive(true);
  p.parent.emit("restore");
  assert.deepStrictEqual(seen, [true, false, true, false, true]);
  p.handle.close();
  p.parent.emit("minimize");
  assert.strictEqual(seen.length, 5, "quiet once closed");
});

test("pane: leaving the screen while it holds the keyboard hands focus back to the dashboard", async () => {
  const f = fakeDeps();
  const p = openPane(f);
  const wc = p.view.webContents;
  wc.hasFocus = true;
  p.handle.setInactive(true);
  assert.strictEqual(p.dashboard.webContents.focused, 1);
  p.handle.setInactive(false);
  wc.hasFocus = false;
  p.handle.setInactive(true);
  assert.strictEqual(p.dashboard.webContents.focused, 1, "no hand-back when the pane did not hold focus");
  wc.emit("focus");
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(p.dashboard.webContents.focused, 2, "focus taken while hidden goes back");
  p.handle.focus();
  assert.strictEqual(p.view.visible, true);
  assert.strictEqual(wc.hasFocus, true);
  assert.deepStrictEqual(p.parent.calls, ["show", "focus"]);
  p.handle.close();
  assert.strictEqual(p.dashboard.webContents.focused, 3, "closing a focused pane hands focus back");
});

test("pane: a dashboard reload or a gone renderer closes it, and a closed pane leaves no listener behind", () => {
  for (const event of ["did-navigate", "render-process-gone"]) {
    const f = fakeDeps();
    const p = openPane(f);
    p.dashboard.webContents.emit(event);
    const closed = p.handle.isClosed();
    const onWindow = p.parent.children.includes(p.view);
    const left = p.dashboard.webContents.listenerCount(event);
    p.handle.close();
    assert.strictEqual(closed, true, event);
    assert.strictEqual(onWindow, false, event);
    assert.strictEqual(left, 0, event);
  }
});

test("pane: an overlay and an inactive tab each hide it; it shows once both are off, and focus keeps the overlay", () => {
  const f = fakeDeps();
  const p = openPane(f);
  p.handle.setOverlay(true);
  assert.strictEqual(p.view.visible, false);
  p.handle.setInactive(true);
  p.handle.setOverlay(false);
  assert.strictEqual(p.view.visible, false, "still behind another tab");
  p.handle.setOverlay(true);
  p.handle.setInactive(false);
  assert.strictEqual(p.view.visible, false, "still under the overlay");
  p.handle.setInactive(true);
  p.handle.focus();
  assert.strictEqual(p.view.visible, false, "focus brings its tab forward, and the overlay still hides it");
  p.handle.setOverlay(false);
  assert.strictEqual(p.view.visible, true);
  p.handle.close();
});

test("pane: main reads the Crew-switch chord before the page and reports its tab index", () => {
  const f = fakeDeps();
  const p = openPane(f);
  const chords = [];
  p.handle.onChord((index) => chords.push(index));
  const press = (input) => {
    let prevented = false;
    p.view.webContents.emit("before-input-event", { preventDefault: () => { prevented = true; } }, { type: "keyDown", ...input });
    return prevented;
  };
  assert.strictEqual(press({ control: true, code: "Digit1" }), true);
  assert.strictEqual(press({ control: true, code: "Digit6" }), true);
  assert.strictEqual(press({ control: true, code: "Digit7" }), false);
  assert.strictEqual(press({ control: true, shift: true, code: "Digit2" }), false);
  assert.strictEqual(press({ control: true, alt: true, code: "Digit2" }), false);
  assert.strictEqual(press({ meta: true, code: "Digit2" }), false, "Cmd is the macOS chord");
  assert.strictEqual(press({ control: true, code: "KeyR" }), false);
  assert.deepStrictEqual(chords, [0, 5]);
  assert.strictEqual(paneChordIndex({ type: "keyDown", meta: true, code: "Digit3" }, "darwin"), 2);
  assert.strictEqual(paneChordIndex({ type: "keyDown", control: true, code: "Digit3" }, "darwin"), -1);
  assert.strictEqual(paneChordIndex({ type: "keyUp", control: true, code: "Digit3" }, "linux"), -1);
  assert.strictEqual(paneChordIndex(null, "linux"), -1);
  p.handle.close();
});

test("pane: close takes its views off the window, clears the partition, runs once; the window closing closes it", async () => {
  const f = fakeDeps();
  const p = openPane(f);
  p.handle.showNotice(PANE_NOTICE);
  const notice = f.views[1];
  let closedCalls = 0;
  p.handle.onClosed(() => { closedCalls += 1; });
  p.handle.close();
  p.handle.close();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(closedCalls, 1);
  assert.strictEqual(p.handle.isClosed(), true);
  assert.deepStrictEqual(p.parent.children, []);
  assert.strictEqual(p.view.webContents.closed, true);
  assert.strictEqual(notice.webContents.closed, true);
  assert.strictEqual(f.ses.headerRule, null);
  assert.strictEqual(f.ses.completedRule, null);
  assert.strictEqual(f.ses.errorRule, null);
  assert.strictEqual(f.ses.cleared, 1);
  for (const event of ["minimize", "restore", "hide", "show", "closed"]) {
    assert.strictEqual(p.parent.listenerCount(event), 0, `no ${event} listener left on the main window`);
  }
  const again = openPane(f);
  assert.notStrictEqual(again.handle, p.handle);
  again.parent.close();
  assert.strictEqual(again.handle.isClosed(), true);
});

test("pane: only main's IPC opens it, and from the dashboard only the pane helper calls it", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const callers = [];
  const walk = (dir, exts, needle, root) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!["node_modules", "test", "dist", "build"].includes(entry.name)) walk(full, exts, needle, root);
      } else if (exts.some((ext) => entry.name.endsWith(ext)) && full !== require.resolve("../relay-crew-window")
        && fs.readFileSync(full, "utf8").includes(needle)) {
        callers.push(path.relative(root, full));
      }
    }
  };
  const shell = path.join(__dirname, "..");
  walk(shell, [".js"], "openRelayCrewPane", shell);
  assert.deepStrictEqual(callers, ["ipc-registrar.js"]);
  callers.length = 0;
  const dashboard = path.join(shell, "..", "src");
  walk(dashboard, [".ts", ".tsx"], "relayCrewAPI", dashboard);
  const paneCalls = /\b(openPane|setPaneBounds|setPaneOverlay|setPaneInactive|closePane)\b/;
  const users = callers.filter((f) => !/\.test\.tsx?$/.test(f)
    && paneCalls.test(fs.readFileSync(path.join(dashboard, f), "utf8")));
  assert.ok(callers.length > 0, "the scan finds the dashboard's relayCrewAPI users");
  assert.deepStrictEqual(users, [path.join("lib", "relayCrewPanes.ts")]);
  // That helper hands out the bridge whenever the shell has the pane calls: no hidden key gates it.
  const helper = fs.readFileSync(path.join(dashboard, "lib", "relayCrewPanes.ts"), "utf8");
  assert.ok(!helper.includes("mc-relay-crew-pane"));
  assert.ok(!helper.includes("safeGetItem"));
});

test("window and pane: the handle and the attached Crew's log lines name the Crew", () => {
  const hosts = [
    (f) => openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps),
    (f) => openPane(f).handle,
  ];
  for (const open of hosts) {
    const f = fakeDeps();
    const handle = open(f);
    assert.strictEqual(handle.endpointId, ENDPOINT);
    f.ses.completedRule.listener({ resourceType: "mainFrame", statusCode: 403 });
    assert.ok(f.logs.includes(`relay crew ${ENDPOINT}: main frame 403`), f.logs.join("\n"));
    handle.close();
    for (const line of f.logs) assert.ok(!line.includes("undefined"), line);
  }
});
