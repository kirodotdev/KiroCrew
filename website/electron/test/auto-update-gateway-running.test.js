// Both update lanes refuse the swap when the pre-swap stop reports that a
// gateway running from this app's own bundle still holds the port.
//
// The stop the lanes await is the gateway-before-swap guarantee: replacing the
// bundle under a live gateway leaves its lazily imported modules reading new
// files against old in-memory ones, and every chat call then fails until the
// gateway is restarted by hand. When the stop cannot make that true it rejects
// with GATEWAY_STILL_RUNNING, and no lane may install or relaunch past it.
const { EventEmitter } = require("node:events");
const cpModule = require("node:child_process");
const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");
const { gatewayStillRunningError, GATEWAY_STILL_RUNNING } = require("../gateway-stop");

const settle = () => new Promise((resolve) => setImmediate(() => setImmediate(resolve)));

function makeDeps({ externallyManaged = null } = {}) {
  const calls = {
    quitAndInstall: 0,
    relaunch: 0,
    exit: [],
    quit: 0,
    installFailed: 0,
    notifications: [],
    states: [],
  };
  const handlers = {};
  const quitHooks = [];
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => "1.0.0",
      once: (event, fn) => { if (event === "before-quit") quitHooks.push(fn); },
      removeListener: () => {},
      relaunch: () => { calls.relaunch += 1; },
      exit: (code) => { calls.exit.push(code); },
      quit: () => { calls.quit += 1; },
    },
    autoUpdater: {
      setFeedURL: () => {},
      checkForUpdates: async () => {},
      downloadUpdate: async () => {},
      quitAndInstall: () => { calls.quitAndInstall += 1; },
      on: (event, fn) => { handlers[event] = fn; },
    },
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function Notification(options) {
      calls.notifications.push(options);
      return { show: () => {} };
    },
    getFlavor: () => "stable",
    getAutoDownloadPreference: () => true,
    stopGateway: async () => { throw gatewayStillRunningError(5476); },
    onInstallFailed: () => { calls.installFailed += 1; },
    osPlatform: "darwin",
    resourcesPath: "/Applications/Kiro Crew.app/Contents/Resources",
    probeBundleWritable: () => true,
    externallyManaged,
    feedBase: "https://cdn.example.dev/feed",
    nativeAutoUpdater: { once: () => {} },
    onUpdateState: (state) => calls.states.push(state),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  const emit = (event, payload) => handlers[event] && handlers[event](payload);
  return { deps, calls, emit, quitHooks };
}

// The managed lane shells the marker's commands through child_process.spawn,
// required inside the lane, so the real module's spawn is replaced for the test.
function stubSpawn(t) {
  const commands = [];
  const original = cpModule.spawn;
  cpModule.spawn = (command) => {
    commands.push(command);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    setImmediate(() => {
      if (command === "check") child.stdout.emit("data", Buffer.from("1.1.0"));
      child.emit("close", 0, null);
    });
    return child;
  };
  t.after(() => { cpModule.spawn = original; });
  return commands;
}

const MANAGED = { managedBy: "m", updateCommand: "apply", checkCommand: "check" };

test("the feed lane does not install while a bundled gateway is still running", async () => {
  const { deps, calls, emit } = makeDeps();
  const updater = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });

  await updater.install();

  assert.strictEqual(calls.quitAndInstall, 0, "the bundle must not be handed to the installer");
  assert.strictEqual(calls.installFailed, 1, "the host restores the gateway it disarmed");
  const last = calls.states[calls.states.length - 1];
  assert.strictEqual(last.state, "error");
  assert.strictEqual(last.phase, "install");
  assert.strictEqual(last.code, GATEWAY_STILL_RUNNING);
  assert.strictEqual(last.stopCommand, "kirocrew stop --port 5476", "the refusal names the command that stops the gateway");
});

test("the feed lane retries the install once the gateway is gone", async () => {
  const { deps, calls, emit } = makeDeps();
  let stopped = false;
  deps.stopGateway = async () => {
    if (!stopped) throw gatewayStillRunningError(5476);
  };
  const updater = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });

  await updater.install();
  stopped = true;
  await updater.install();

  assert.strictEqual(calls.quitAndInstall, 1, "the staged update survives the refusal");
});

test("the feed lane's deferred install quits without installing while a bundled gateway runs", async () => {
  const { deps, calls, emit, quitHooks } = makeDeps();
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  assert.strictEqual(quitHooks.length, 1, "the automatic stage arms the install on quit");

  quitHooks[0]({ preventDefault: () => {} });
  await settle();

  assert.strictEqual(calls.quitAndInstall, 0, "the bundle must not be handed to the installer");
  assert.strictEqual(calls.quit, 1, "the quit the user asked for still proceeds");
  assert.strictEqual(calls.notifications.length, 1, "the user is told the update did not install");
  assert.strictEqual(calls.notifications[0].title, "Update not installed");
  assert.match(
    calls.notifications[0].body,
    /run “kirocrew stop --port 5476” in Terminal, then reopen Kiro Crew/,
    "the notification names the step that lets the next install through",
  );
  assert.match(
    calls.notifications[0].body,
    /your chats and settings are kept/,
    "the notification says running the command loses nothing",
  );
});

test("a stop error that is not a running gateway keeps the feed lane's install going", async () => {
  const { deps, calls, emit } = makeDeps();
  deps.stopGateway = async () => { throw new Error("stop failed"); };
  const updater = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });

  await updater.install();

  assert.strictEqual(calls.quitAndInstall, 1);
});

test("the managed lane does not run its update command while a bundled gateway is running", async (t) => {
  const commands = stubSpawn(t);
  const { deps, calls } = makeDeps({ externallyManaged: MANAGED });
  const updater = initAutoUpdate(deps);
  await updater.check();

  await updater.install();

  assert.deepStrictEqual(commands, ["check"], "the update command must not run");
  assert.strictEqual(calls.relaunch, 0);
  assert.deepStrictEqual(calls.exit, []);
  assert.strictEqual(calls.installFailed, 1, "the host restores the gateway it disarmed");
  const last = calls.states[calls.states.length - 1];
  assert.strictEqual(last.state, "error");
  assert.strictEqual(last.phase, "install");
  assert.strictEqual(last.code, GATEWAY_STILL_RUNNING);
  assert.strictEqual(last.stopCommand, "kirocrew stop --port 5476");
});

test("the managed lane's quit-time apply exits without applying while a bundled gateway runs", async (t) => {
  const commands = stubSpawn(t);
  const { deps, calls, quitHooks } = makeDeps({ externallyManaged: MANAGED });
  const updater = initAutoUpdate(deps);
  await updater.check();
  assert.strictEqual(quitHooks.length, 1, "a found update arms the apply on quit");

  quitHooks[0]({ preventDefault: () => {} });
  await settle();

  assert.deepStrictEqual(commands, ["check"], "the update command must not run");
  assert.strictEqual(calls.relaunch, 0, "nothing relaunches into an update that did not apply");
  assert.deepStrictEqual(calls.exit, [0], "the quit the user asked for still proceeds");
});
