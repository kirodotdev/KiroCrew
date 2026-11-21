"use strict";
//
// The one-time notice the first close-to-background shows, and the tray quit
// label that names the gateway quitting stops.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  CLOSE_HIDE_NOTICE_KEY,
  CLOSE_ACTION_SETTING_ROUTE,
  trayQuitLabel,
  trayMenuTemplate,
  closeHideNotice,
  createCloseHideNotice,
  createCloseHideWiring,
} = require("../close-hide-notice");
const { createWindowLifecycle } = require("../window-lifecycle");

function fakeStore(initial = {}) {
  const data = { ...initial };
  return { data, get: (key) => data[key], set: (key, value) => { data[key] = value; } };
}

function fakeNotificationClass({ supported = true } = {}) {
  const made = [];
  class FakeNotification {
    constructor(options) {
      this.options = options;
      this.handlers = {};
      this.shown = false;
      made.push(this);
    }
    on(event, handler) { this.handlers[event] = handler; }
    emit(event) { if (this.handlers[event]) this.handlers[event](); }
    show() { this.shown = true; }
  }
  FakeNotification.isSupported = () => supported;
  return { FakeNotification, made };
}

const NOTICE = { title: "t", body: "b" };

test("the stored key is written only once the system reports the notice shown", () => {
  const store = fakeStore();
  const { FakeNotification, made } = fakeNotificationClass();
  let clicks = 0;
  const show = createCloseHideNotice({ store, Notification: FakeNotification, platform: "darwin" });
  assert.equal(show({ notice: NOTICE, onClick: () => { clicks += 1; } }), true);
  assert.equal(made.length, 1);
  assert.equal(made[0].shown, true);
  assert.deepEqual(made[0].options, { title: "t", body: "b" });
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], undefined, "not yet known to have been seen");

  made[0].emit("show");
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], true);
  made[0].emit("click");
  assert.equal(clicks, 1, "a click opens the setting");

  const later = createCloseHideNotice({ store, Notification: FakeNotification, platform: "darwin" });
  assert.equal(later({ notice: NOTICE, onClick() {} }), false, "a later launch shows nothing");
  assert.equal(made.length, 1);
});

test("a notice the system drops comes back on a later launch, once per run", () => {
  const store = fakeStore();
  const { FakeNotification, made } = fakeNotificationClass();
  const show = createCloseHideNotice({ store, Notification: FakeNotification, platform: "darwin" });
  // A burst of closes while the first notice never reports `show`.
  for (let i = 0; i < 3; i += 1) show({ notice: NOTICE, onClick() {} });
  assert.equal(made.length, 1, "a burst of closes makes one notification");
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], undefined);

  const nextLaunch = createCloseHideNotice({ store, Notification: FakeNotification, platform: "darwin" });
  assert.equal(nextLaunch({ notice: NOTICE, onClick() {} }), true, "tried again next launch");
  assert.equal(made.length, 2);
});

test("a Windows `failed` lets the next close try again", () => {
  const store = fakeStore();
  const { FakeNotification, made } = fakeNotificationClass();
  const show = createCloseHideNotice({ store, Notification: FakeNotification, platform: "win32" });
  show({ notice: NOTICE, onClick() {} });
  made[0].emit("failed");
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], undefined);
  assert.equal(show({ notice: NOTICE, onClick() {} }), true);
  assert.equal(made.length, 2);
});

test("Linux notifications emit no `show`, so the key is written after show()", () => {
  const store = fakeStore();
  const { FakeNotification } = fakeNotificationClass();
  const show = createCloseHideNotice({ store, Notification: FakeNotification, platform: "linux" });
  assert.equal(show({ notice: NOTICE, onClick() {} }), true);
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], true);
});

test("notifications unsupported: nothing shown, and it counts as done", () => {
  const store = fakeStore();
  const { FakeNotification, made } = fakeNotificationClass({ supported: false });
  assert.equal(createCloseHideNotice({ store, Notification: FakeNotification, platform: "linux" })({ notice: NOTICE, onClick() {} }), false);
  assert.equal(made.length, 0);
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], true);
});

test("no Notification class: nothing shown, nothing remembered", () => {
  const store = fakeStore();
  assert.equal(createCloseHideNotice({ store, Notification: undefined, platform: "linux" })({ notice: NOTICE, onClick() {} }), false);
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], undefined);
});

test("a notification whose show() throws never escapes the close path, and the next close retries", () => {
  const lines = [];
  const store = fakeStore();
  const { FakeNotification, made } = fakeNotificationClass();
  // Thrown after the in-memory flag is set, so only the catch block's reset
  // lets the next close try again.
  FakeNotification.prototype.show = () => { throw new Error("boom"); };
  const show = createCloseHideNotice({
    store, Notification: FakeNotification, platform: "linux", log: (line) => lines.push(line),
  });
  assert.equal(show({ notice: NOTICE, onClick() {} }), false);
  assert.match(lines[0], /boom/);
  assert.equal(store.data[CLOSE_HIDE_NOTICE_KEY], undefined, "a notice that never showed is not remembered");
  assert.equal(made.length, 1);
  assert.equal(show({ notice: NOTICE, onClick() {} }), false);
  assert.equal(made.length, 2, "the flag was reset, so the next close built a new notice");
});

test("the notice says the app keeps running, how to quit, and where the setting is", () => {
  const mac = closeHideNotice({ appName: "Kiro Crew", isMac: true, stopsGateway: false });
  assert.equal(mac.title, "Kiro Crew is still running");
  assert.match(mac.body, /the Dock/);
  // Named in words, so the way there does not depend on a click action.
  assert.match(mac.body, /Settings → Display → View → Desktop app/);
  const other = closeHideNotice({ appName: "Kiro Crew", isMac: false, stopsGateway: true });
  assert.equal(other.title, "Kiro Crew and its gateway are still running");
  assert.match(other.body, /the tray icon/);
});

test("the quit label names the gateway only when quitting stops one", () => {
  assert.equal(trayQuitLabel("Kiro Crew", false), "Quit Kiro Crew");
  assert.equal(trayQuitLabel("Kiro Crew", true), "Quit Kiro Crew and Stop Its Gateway");
  const onQuit = () => {};
  const onShow = () => {};
  const template = trayMenuTemplate({
    appName: "Kiro Crew", stopsGateway: true, onShow, onNewConnection() {}, onOpenConfig() {}, onQuit,
  });
  assert.equal(template[0].label, "Show Kiro Crew");
  assert.equal(template[0].click, onShow, "the Show item runs the tray's show handler");
  assert.equal(template.at(-1).label, "Quit Kiro Crew and Stop Its Gateway");
  assert.equal(template.at(-1).click, onQuit);
});

test("the notice opens the close-button row, the route the settings registry generates", () => {
  const registry = JSON.parse(fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "src", "kiro_crew", "docs", "settings-registry.generated.json"),
    "utf8",
  ));
  const row = registry.settings.find(
    (entry) => entry && entry.id === "display.keep-running-in-the-background-after-closing-the-window",
  );
  assert.ok(row, "the close-button row is registered");
  assert.equal(CLOSE_ACTION_SETTING_ROUTE, row.route);
});

test("the tray menu follows the gateway the app runs, both ways", () => {
  const menus = [];
  const trays = [];
  let ours = false;
  const lifecycle = createWindowLifecycle({
    electron: {
      app: { name: "Kiro Crew", getVersion: () => "0.8.0", getPath: () => "/virtual/logs" },
      Tray: class {
        constructor() { trays.push(this); }
        setToolTip() {}
        setContextMenu(menu) { this.menu = menu; }
        on() {}
      },
      Menu: { buildFromTemplate: (template) => { menus.push(template); return { template }; } },
      nativeImage: {
        createFromPath: () => ({ resize: () => ({ fake: "icon" }), setTemplateImage() {} }),
      },
      shell: {},
    },
    store: { path: "/virtual/config.json", get: () => undefined, set() {} },
    backendUrl: "http://localhost:5476",
    port: 5476,
    mintLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    stopsGatewayOnQuit: () => ours,
    platform: "linux",
    env: {},
  });
  lifecycle.syncTray();
  assert.equal(menus[0].at(-1).label, "Quit Kiro Crew");
  // The supervisor's spawn announcement.
  ours = true;
  lifecycle.refreshTrayMenu();
  assert.equal(menus[1].at(-1).label, "Quit Kiro Crew and Stop Its Gateway");
  // Its exit, stop or switch to a remote gateway.
  ours = false;
  lifecycle.refreshTrayMenu();
  assert.equal(menus[2].at(-1).label, "Quit Kiro Crew");
  assert.equal(trays[0].menu.template, menus[2], "the tray shows the rebuilt menu");
});

test("the close button's hide path, and only it, shows the notice", () => {
  // createWindow needs a full BaseWindow/WebContentsView fake, so the call is
  // pinned in source the way hide-to-tray.test.js pins hideToTray; the close
  // decision itself (quit never reaches hide) runs in close-action.test.js.
  const source = fs.readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");
  const start = source.indexOf('mainWindow.on("close", createCloseHandler({');
  assert.notEqual(start, -1, "could not locate the close handler");
  const handler = source.slice(start, source.indexOf("\n    }));\n", start));
  const hideStart = handler.indexOf("hide: () => {");
  const persistStart = handler.indexOf("persist: () => {");
  assert.ok(hideStart > 0 && persistStart > hideStart, "could not bound the hide branch");
  const hide = handler.slice(hideStart, persistStart);
  const quit = handler.slice(0, hideStart);
  assert.ok(
    hide.indexOf("closeHide.noticeOnHide()") > hide.indexOf("hideToTray(mainWindow"),
    "the notice follows the hide it describes",
  );
  assert.doesNotMatch(quit, /noticeOnHide/, "a close that quits shows no notice");
  assert.match(source, /onShow: showFromTray,/, "the tray Show item shares the icon click's handler");
  // Bounded to the wiring call: other owners pass the same text elsewhere.
  const wiringStart = source.indexOf("createCloseHideWiring({");
  assert.notEqual(wiringStart, -1, "could not locate the close-hide wiring");
  const wiring = source.slice(wiringStart, source.indexOf("\n  });\n", wiringStart));
  assert.match(wiring, /getMainWindow: \(\) => mainWindow,/, "the main window, which has the row");
});

test("the wiring's notice click opens the close-button row on the main window", () => {
  const { FakeNotification, made } = fakeNotificationClass();
  const shows = [];
  const sent = [];
  let mainWindow = null;
  let shown = false;
  const wiring = createCloseHideWiring({
    app: { name: "Kiro Crew" },
    Menu: { buildFromTemplate: (template) => ({ template }) },
    Notification: FakeNotification,
    store: fakeStore(),
    platform: "win32",
    stopsGatewayOnQuit: () => true,
    getMainWindow: () => mainWindow,
    getTray: () => null,
    showMainWindow: (o) => { shows.push(o); return shown; },
    onShow() {}, onNewConnection() {}, onOpenConfig() {}, onQuit() {},
  });
  assert.equal(wiring.noticeOnHide(), true);
  assert.equal(made.length, 1);
  assert.equal(made[0].options.title, "Kiro Crew and its gateway are still running");

  made[0].emit("click");
  assert.deepEqual(shows, [{ focus: true }]);
  assert.deepEqual(sent, [], "nothing shown: nothing to navigate");

  // Shown, but the main window is gone or has no dashboard view yet.
  shown = true;
  assert.doesNotThrow(() => made[0].emit("click"));
  mainWindow = {};
  assert.doesNotThrow(() => made[0].emit("click"));
  assert.deepEqual(sent, [], "no dashboard view: nothing to navigate");

  mainWindow = { _mcView: { webContents: { isDestroyed: () => false, send: (...args) => sent.push(args) } } };
  made[0].emit("click");
  assert.equal(shows.length, 4);
  assert.ok(shows.every((o) => o.focus === true), "a click is a focused show");
  assert.deepEqual(sent, [], "the dashboard has not acked yet: the route is held");

  // The dashboard mounts its navigate listener: the held route goes once.
  wiring.navigation.navigateReady();
  assert.deepEqual(sent, [["navigate", CLOSE_ACTION_SETTING_ROUTE]]);
  wiring.navigation.navigateReady();
  assert.equal(sent.length, 1, "a later ack does not resend");

  // A ready document takes a click at once.
  made[0].emit("click");
  assert.equal(sent.length, 2);

  // A new document (reload, gateway-error page) waits for its own ack.
  wiring.navigation.navigationStarted();
  made[0].emit("click");
  assert.equal(sent.length, 2, "held across the new document's load");
  wiring.navigation.navigateReady();
  assert.equal(sent.length, 3);
});

test("the wiring builds the tray menu from its handlers and the gateway state", () => {
  let tray = null;
  let ours = false;
  const onShow = () => {};
  const onQuit = () => {};
  const wiring = createCloseHideWiring({
    app: { name: "Kiro Crew" },
    Menu: { buildFromTemplate: (template) => ({ template }) },
    Notification: undefined,
    store: fakeStore(),
    platform: "linux",
    stopsGatewayOnQuit: () => ours,
    getMainWindow: () => null,
    getTray: () => tray,
    showMainWindow: () => false,
    onShow, onNewConnection() {}, onOpenConfig() {}, onQuit,
  });
  wiring.refreshTrayMenu();
  tray = { setContextMenu(menu) { this.menu = menu; } };
  ours = true;
  wiring.refreshTrayMenu();
  assert.equal(tray.menu.template[0].click, onShow);
  assert.equal(tray.menu.template.at(-1).label, "Quit Kiro Crew and Stop Its Gateway");
  assert.equal(tray.menu.template.at(-1).click, onQuit);
});

test("main.js rebuilds the tray menu on every change the supervisor announces", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(main, /onOwnedGatewayChange: \(\) => windows\?\.refreshTrayMenu\(\),/);
  // The notice click waits for the main dashboard's navigate-ready ack.
  // One navigate-ready handler feeds both the deep links and the notice click.
  assert.equal(main.split("installNavigateReady({").length - 1, 1, "a single navigate-ready registration");
  assert.match(
    main,
    /navigateReady: \(\) => \{ deepLinks\.navigateReady\(\); windows\?\.closeHideNavigation\.navigateReady\(\); \},/,
  );
  assert.match(
    main,
    /navigationStarted: \(\) => \{ deepLinks\.navigationStarted\(\); windows\?\.closeHideNavigation\.navigationStarted\(\); \},/,
  );
  const lifecycle = fs.readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");
  assert.match(lifecycle, /closeHideNavigation: closeHide\.navigation,/);
  assert.match(main, /stopsGatewayOnQuit: \(\) => gateway\.stopsGatewayOnQuit\(\),/);
});
