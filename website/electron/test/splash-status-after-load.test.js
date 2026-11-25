"use strict";

// A status line sent while loading.html is still loading reaches the page being
// replaced, and the loader then shows its default "Starting gateway…" instead.
// Every flow that paints the loader and then sends it a line waits for that
// load to end first: loaded, or replaced by a later navigation.

const { EventEmitter } = require("node:events");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { RESTARTING_STATUS, createUserRestart } = require("../runtime/gateway/user-restart");
const { SWITCHING_STATUS, createRemoteSwitch } = require("../runtime/gateway/remote-switch");
const { createReconnect } = require("../runtime/gateway/reconnect");
const { createSuccessorHandoff } = require("../runtime/gateway/successor-handoff");

const flush = () => new Promise((resolve) => setImmediate(resolve));
const PORT = 5476;
const BACKEND = `http://localhost:${PORT}`;
const DIRNAME = "/virtual/electron";

// Each loadFile stays pending until the test settles it, as a real load does
// until the page has loaded or a later navigation replaced it.
function splashWindow() {
  const window = {
    loads: [],
    isDestroyed: () => false,
    webContents: {
      isDestroyed: () => false,
      getURL: () => `${BACKEND}/`,
      loadFile(file) {
        let settle;
        const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
        window.loads.push({ file: String(file), ...settle });
        return promise;
      },
    },
  };
  return window;
}

// Each flow is started with the same window and status sink; a stop that
// reads as the app quitting ends the flow right after its first line.
const FLOWS = [
  ["Settings → Restart gateway", RESTARTING_STATUS, (window, sendStatus) => {
    let quit = false;
    let monitor = { stop() {} };
    const owner = createUserRestart({
      backendUrl: BACKEND,
      mainWindow: () => window,
      quitting: () => quit,
      isInstallingUpdate: () => false,
      // The supervisor's paintLoadingScreen: settles once the load has ended.
      paintLoadingScreen: (target) => target.webContents
        .loadFile(path.join(DIRNAME, "loading.html")).catch(() => {}),
      sendStatus,
      getGatewayProcess: () => ({ exitCode: null }),
      getGatewayOwnership: () => "spawned",
      getLivenessMonitor: () => monitor,
      setLivenessMonitor: (value) => { monitor = value; },
      setRestartingGateway: () => {},
      setGatewayStartFailure: () => {},
      stopGatewayGracefully: async () => { quit = true; },
      respawnSuperseded: "superseded",
      respawnGateway: async () => true,
      showLoadingThenConnect: async () => {},
    });
    assert.equal(owner.restartGateway(window), "restarting");
  }],
  ["switch to a remote crew", SWITCHING_STATUS, (window, sendStatus) => {
    let quit = false;
    let monitor = { stop() {} };
    const owner = createRemoteSwitch({
      store: { get: (_key, fallback) => fallback, set: () => {} },
      port: PORT,
      backendUrl: BACKEND,
      path,
      dirname: DIRNAME,
      isWindows: false,
      mainWindow: () => window,
      quitting: () => quit,
      promptRemoteCrew: async () => (
        { host: "devbox.example.com", binPath: "", remotePort: "", remotePath: "", manageTunnel: true }
      ),
      splashQuery: () => ({}),
      currentThemeAccent: () => "",
      sendStatus,
      getGatewayProcess: () => ({ exitCode: null }),
      getGatewayOwnership: () => "spawned",
      getLivenessMonitor: () => monitor,
      setLivenessMonitor: (value) => { monitor = value; },
      setSwitchingToRemote: () => {},
      setGatewayStartFailure: () => {},
      stopGatewayGracefully: async () => { quit = true; },
      waitForPortFree: async () => true,
    });
    void owner.switchToRemoteCrew(window);
  }],
  ...[
    ["external gateway connection lost", "Connection lost — waiting for the gateway to come back…", "reconnectExternalGateway"],
    ["adopted gateway stopped responding", "Gateway stopped responding — waiting for it to recover…", "reconnectOrRespawnAdoptedGateway"],
  ].map(([name, status, method]) => [name, status, (window, sendStatus) => {
    const reconnect = createReconnect({
      path,
      dirname: DIRNAME,
      port: PORT,
      backendUrl: BACKEND,
      healthUrl: `${BACKEND}/api/status`,
      log: () => {},
      quitting: () => false,
      sendStatus,
      splashQuery: () => ({}),
      setGatewayStartFailure: () => {},
      checkBackend: async () => {},
      showLoadingThenConnect: async () => {},
    });
    void reconnect[method](window);
  }]),
  ["restart handoff mid-session", "Restarting Kiro Crew to finish the update…", (window, sendStatus) => {
    const handoff = createSuccessorHandoff({
      app: { releaseSingleInstanceLock() {} },
      // Nothing answers on the port yet, so the handoff goes ahead.
      http: {
        get() {
          const request = new EventEmitter();
          setImmediate(() => request.emit("error", new Error("ECONNREFUSED")));
          return request;
        },
      },
      // A successor that has not started yet: the handoff waits on its events.
      spawn: () => Object.assign(new EventEmitter(), { pid: 4242 }),
      processObj: { execPath: "/virtual/kirocrew", argv: ["kirocrew"], env: {} },
      path,
      dirname: DIRNAME,
      port: PORT,
      backendUrl: BACKEND,
      log: () => {},
      quitting: () => false,
      mainWindow: () => window,
      sendStatus,
      splashQuery: () => ({}),
      currentThemeAccent: () => "",
      getLivenessMonitor: () => ({ stop() {} }),
      setLivenessMonitor: () => {},
    });
    void handoff.relaunchViaConfirmedSuccessor(() => {});
  }],
];

for (const [flow, status, start] of FLOWS) {
  for (const [outcome, settle] of [
    ["loaded", (load) => load.resolve()],
    ["replaced by a later navigation", (load) => load.reject(new Error("ERR_ABORTED (-3)"))],
  ]) {
    test(`${flow}: the status waits for the loader load to end (${outcome})`, async () => {
      const window = splashWindow();
      const statuses = [];
      start(window, (message) => statuses.push(message));
      for (let i = 0; i < 5; i += 1) await flush();
      assert.equal(window.loads.length, 1);
      assert.match(window.loads[0].file, /loading\.html$/);
      assert.deepEqual(statuses, [], "nothing goes to the page the loader replaces");

      settle(window.loads[0]);
      for (let i = 0; i < 5; i += 1) await flush();
      assert.equal(statuses[0], status);
    });
  }
}
