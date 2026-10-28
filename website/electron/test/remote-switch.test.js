"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  PROMPT_SAVE_LABEL,
  PROMPT_TITLE,
  STOP_FAILED_MESSAGE,
  SWITCHING_STATUS,
  createRemoteSwitch,
} = require("../runtime/gateway/remote-switch");

const flush = () => new Promise((resolve) => setImmediate(resolve));

function fakeStore(initial = {}, { beforeSet = () => {}, afterSet = () => {} } = {}) {
  const data = structuredClone(initial);
  return {
    data,
    // Like electron-store, reads are snapshots, not references to durable data.
    get: (key, fallback) => structuredClone(Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback),
    set: (key, value) => {
      beforeSet(key, value);
      data[key] = structuredClone(value);
      afterSet(key, value);
    },
  };
}

function fakeWindow() {
  const loads = [];
  return {
    loads,
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    webContents: { loadFile: (...args) => loads.push(args) },
  };
}

const CREW = { host: "devbox.example.com", binPath: "", remotePort: "", remotePath: "", manageTunnel: true };

/**
 * The owner with every dependency recorded. `forms` is the sequence of answers
 * the crew form gives, one per opening.
 */
function harness({
  store = fakeStore({ runLocalGateway: true, remoteHosts: {} }),
  port = 5476,
  window = fakeWindow(),
  main = undefined,
  ownership = "spawned",
  child = { exitCode: null },
  forms = [CREW],
  freed = true,
  isWindows = false,
  quitting = false,
  failures = {},
} = {}) {
  const events = [];
  const state = {
    ownership,
    child,
    monitor: { stop: () => events.push("monitor.stop") },
    switching: false,
    runLocalGateway: true,
    startFailure: { code: 0 },
  };
  const prompts = [];
  const dialogs = [];
  const starts = [];
  const logs = [];
  const owner = createRemoteSwitch({
    store,
    port,
    backendUrl: `http://localhost:${port}`,
    dialog: {
      showMessageBox: async (parent, options) => {
        dialogs.push(options);
        events.push(`dialog:${options.title}`);
        if (failures.dialog) throw failures.dialog;
      },
    },
    path,
    dirname: "/virtual/electron",
    isWindows,
    log: (message) => logs.push(message),
    mainWindow: () => (main === undefined ? window : main),
    quitting: () => quitting,
    promptRemoteCrew: async (parent, promptPort, draft, labels) => {
      prompts.push({ parent, promptPort, draft, labels });
      events.push("prompt");
      return forms.shift() ?? null;
    },
    splashQuery: () => ({ primary: "1" }),
    currentThemeAccent: () => "#8E48FF",
    sendStatus: (message) => events.push(`status:${message}`),
    getGatewayProcess: () => state.child,
    getGatewayOwnership: () => state.ownership,
    setGatewayOwnership: (value) => { state.ownership = value; },
    getLivenessMonitor: () => state.monitor,
    setLivenessMonitor: (value) => { state.monitor = value; },
    setSwitchingToRemote: (value) => { state.switching = value; events.push(`switching:${value}`); },
    setRunLocalGateway: (value) => { state.runLocalGateway = value; },
    setGatewayStartFailure: (value) => { state.startFailure = value; },
    stopGatewayGracefully: async () => {
      // A crew must not be on the port while our own gateway still holds it.
      events.push(`stop:crew=${store.get("remoteHosts", {})[String(port)]?.host || ""}`);
      state.child = null;
    },
    waitForPortFree: async () => { events.push("waitForPortFree"); return freed; },
    startGateway: async () => {
      starts.push({ local: state.runLocalGateway, config: store.get("remoteHosts", {})[String(port)] });
      events.push(`startGateway:local=${state.runLocalGateway}`);
      if (failures.start) throw failures.start;
    },
    showLoadingThenConnect: async (target, url) => {
      events.push(`connect:${url}`);
      if (failures.connect) throw failures.connect;
    },
  });
  return { owner, store, window, state, events, prompts, dialogs, starts, logs };
}

test("offered only to the main window on a gateway this app spawned and still runs", () => {
  const h = harness();
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), true);

  for (const [label, options] of [
    ["connection window", { main: fakeWindow() }],
    ["adopted gateway", { ownership: "reused-local" }],
    ["service gateway", { ownership: "reused-service" }],
    ["external holder", { ownership: "none" }],
    ["no child", { child: null }],
    ["exited child", { child: { exitCode: 1 } }],
    ["quitting", { quitting: true }],
    ["unselectable port", { port: 80 }],
    ["crew already on the port", {
      store: fakeStore({ remoteHosts: { 5476: { host: "elsewhere" } } }),
    }],
  ]) {
    const c = harness(options);
    assert.equal(c.owner.canSwitchToRemoteCrew(c.window), false, label);
  }
  const destroyed = harness();
  destroyed.window.destroyed = true;
  assert.equal(destroyed.owner.canSwitchToRemoteCrew(destroyed.window), false, "destroyed window");
});

test("a refused window gets no form", async () => {
  const h = harness({ ownership: "reused-local" });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "unavailable");
  assert.deepEqual(h.events, []);
});

test("a closed form changes nothing", async () => {
  const h = harness({ forms: [null] });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "cancelled");
  await flush();
  assert.deepEqual(h.events, ["prompt"]);
  assert.deepEqual(h.store.data.remoteHosts, {});
  assert.equal(h.store.data.runLocalGateway, true);
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), true, "the exit stays available");
});

test("the form opens with the tunnel option on, under first-run wording", async () => {
  const h = harness({ forms: [null] });
  await h.owner.switchToRemoteCrew(h.window);
  assert.equal(h.prompts[0].promptPort, 5476);
  assert.equal(h.prompts[0].draft.manageTunnel, true);
  assert.deepEqual(h.prompts[0].labels, { title: PROMPT_TITLE, saveLabel: PROMPT_SAVE_LABEL });

  const win = harness({ forms: [null], isWindows: true });
  await win.owner.switchToRemoteCrew(win.window);
  assert.equal(win.prompts[0].draft.manageTunnel, false, "Windows has no managed tunnel");
});

test("an invalid field reopens the form on what was typed and stops nothing", async () => {
  const bad = { ...CREW, host: "not a host!" };
  const h = harness({ forms: [bad, null] });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "cancelled");
  await flush();
  assert.deepEqual(h.events, ["prompt", "dialog:Invalid Input", "prompt"]);
  assert.equal(h.prompts[1].draft.host, "not a host!");
  assert.deepEqual(h.store.data.remoteHosts, {});
});

test("a saved form stops the local gateway, then records the crew and connects to it", async () => {
  const h = harness();
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "switching");
  await flush();
  await flush();

  assert.deepEqual(h.events, [
    "prompt",
    "monitor.stop",
    "switching:true",
    `status:${SWITCHING_STATUS}`,
    "stop:crew=",
    "waitForPortFree",
    "switching:false",
    "startGateway:local=false",
    "connect:http://localhost:5476",
  ]);
  assert.equal(h.window.loads.length, 1);
  assert.equal(h.window.loads[0][0], path.join("/virtual/electron", "loading.html"));
  assert.equal(h.store.data.remoteHosts["5476"].host, "devbox.example.com");
  assert.equal(h.store.data.remoteHosts["5476"].manageTunnel, true);
  assert.equal(h.store.data.runLocalGateway, false, "the setting is off for later launches too");
  assert.equal(h.state.runLocalGateway, false);
  assert.equal(h.state.ownership, "none", "nothing on the port is this app's any more");
  assert.equal(h.state.monitor, null);
  assert.equal(h.state.startFailure, null, "the stopped child's exit is not a start failure");
});

test("a port that does not free keeps the local gateway and says so", async () => {
  const h = harness({ freed: false });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "switching");
  await flush();
  await flush();

  assert.deepEqual(h.store.data.remoteHosts, {}, "no crew is recorded over our own gateway");
  assert.equal(h.store.data.runLocalGateway, true);
  assert.equal(h.dialogs.at(-1).message, STOP_FAILED_MESSAGE);
  assert.deepEqual(h.events.slice(-2), ["startGateway:local=true", "connect:http://localhost:5476"]);
});

for (const key of ["remoteHosts", "runLocalGateway"]) {
  for (const phase of ["beforeSet", "afterSet"]) {
    test(`${key} throws ${phase}: restores the local gateway before reconnecting`, async () => {
      const hosts = { 5476: { defaultName: "My crew" }, 5477: { host: "other.example.com" } };
      const writes = [];
      let injected = false;
      const store = fakeStore({ runLocalGateway: true, remoteHosts: hosts }, {
        [phase]: (writtenKey, value) => {
          writes.push({ key: writtenKey, value: structuredClone(value) });
          if (writtenKey === key && !injected) {
            injected = true;
            throw new Error("settings disk full");
          }
        },
      });
      const h = harness({ store });
      assert.equal(await h.owner.switchToRemoteCrew(h.window), "switching");
      await flush();
      await flush();

      assert.equal(injected, true);
      assert.deepEqual(store.data.remoteHosts, hosts, "only this port's SSH fields are cleared");
      assert.equal(store.data.runLocalGateway, true);
      assert.equal(h.state.runLocalGateway, true);
      assert.equal(h.state.switching, false);
      assert.equal(h.state.startFailure, null);
      assert.deepEqual(h.starts, [{ local: true, config: hosts[5476] }], "crew cleared BEFORE restart");
      assert.deepEqual(h.events.slice(-2), ["startGateway:local=true", "connect:http://localhost:5476"]);
      assert.match(h.dialogs.at(-1).message, /Could not save the remote crew settings/);
      assert.match(h.dialogs.at(-1).message, /writable and has free space/);
      assert.ok(h.logs.some((message) => message.includes("settings disk full")));
      assert.equal(writes[0].key, "remoteHosts");
      assert.equal(writes[0].value[5476].host, CREW.host);
      if (key === "runLocalGateway") assert.equal(writes[1].value, false);
    });
  }
}

test("reconciliation write failures are logged without skipping restart or reconnect", async () => {
  let remoteWrites = 0;
  const store = fakeStore({ runLocalGateway: true, remoteHosts: {} }, {
    beforeSet: (key) => {
      if (key === "remoteHosts" && ++remoteWrites === 1) return;
      throw new Error(`cannot write ${key}`);
    },
  });
  const h = harness({ store });
  await h.owner.switchToRemoteCrew(h.window);
  await flush();
  await flush();

  assert.equal(store.data.remoteHosts[5476].host, CREW.host, "failed clear must not pretend it succeeded");
  assert.equal(store.data.runLocalGateway, true);
  assert.equal(h.state.runLocalGateway, true);
  // The real startGateway refuses to spawn over this still-configured crew.
  assert.equal(h.starts[0].local, true, "still attempts the guarded launch path");
  assert.equal(h.starts[0].config.host, CREW.host);
  assert.deepEqual(h.events.slice(-2), ["startGateway:local=true", "connect:http://localhost:5476"]);
  assert.ok(h.logs.some((message) => message.includes("clearing the crew failed")));
  assert.ok(h.logs.some((message) => message.includes("restoring the local gateway setting failed")));
  assert.match(h.dialogs.at(-1).message, /Could not save the remote crew settings/);
});

test("a rejected error dialog and restart still reach connection recovery", async () => {
  const store = fakeStore({ runLocalGateway: true, remoteHosts: {} }, {
    beforeSet: () => { throw new Error("settings disk full"); },
  });
  const h = harness({ store, failures: { dialog: new Error("dialog closed"), start: new Error("start failed") } });
  await h.owner.switchToRemoteCrew(h.window);
  await flush();
  await flush();

  assert.deepEqual(store.data.remoteHosts, {});
  assert.equal(h.state.runLocalGateway, true);
  assert.deepEqual(h.starts, [{ local: true, config: undefined }]);
  assert.deepEqual(h.events.slice(-2), ["startGateway:local=true", "connect:http://localhost:5476"]);
  assert.ok(h.logs.some((message) => message.includes("showing the failure dialog failed")));
  assert.ok(h.logs.some((message) => message.includes("starting the gateway failed")));
});

test("a rejected reconnect shows an actionable dialog without undoing a completed switch", async () => {
  const h = harness({ failures: { connect: new Error("connection failed") } });
  await h.owner.switchToRemoteCrew(h.window);
  await flush();
  await flush();

  assert.equal(h.store.data.remoteHosts[5476].host, CREW.host);
  assert.equal(h.state.runLocalGateway, false, "do not restart locally over a possible remote tunnel");
  assert.match(h.dialogs.at(-1).message, /could not reconnect.*Quit Kiro Crew and reopen it/);
  assert.ok(h.logs.some((message) => message.includes("reconnecting failed")));
});

test("one switch at a time", async () => {
  let closeForm;
  const h = harness({ forms: [new Promise((resolve) => { closeForm = resolve; })] });
  const first = h.owner.switchToRemoteCrew(h.window);
  await flush();
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), false, "not while the form is open");
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "unavailable");
  closeForm(null);
  assert.equal(await first, "cancelled");
  assert.equal(h.prompts.length, 1, "the second request opened no form");
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), true);
});

test("a gateway that exits while the form is open is not replaced", async () => {
  let closeForm;
  const h = harness({ forms: [new Promise((resolve) => { closeForm = resolve; })] });
  const pending = h.owner.switchToRemoteCrew(h.window);
  await flush();
  h.state.child = { exitCode: 1 };
  closeForm(CREW);
  assert.equal(await pending, "unavailable");
  await flush();
  assert.deepEqual(h.store.data.remoteHosts, {});
  assert.ok(!h.events.some((e) => e.startsWith("stop:")), "nothing was stopped");
});
