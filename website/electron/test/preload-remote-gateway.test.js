// preload.js's local-only bridges, loaded for real against a fake `electron`,
// with and without the `--kc-remote-gateway` argument main.js passes a window
// it opened against a configured remote crew (#14815).
//
// The contract under test is ABSENCE: the SPA served by a remote gateway must
// see no crash-report, WSL, file-open or pane-cache bridge at all -- the same
// thing it sees in a plain browser tab -- so it never invokes a channel the
// main process would refuse. Every other bridge is unaffected.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

const PRELOAD = path.join(__dirname, "..", "preload.js");
const REMOTE_FLAG = "--kc-remote-gateway";

/** Bridges whose every channel is gated by `assertLocalDashboard` in main. */
const LOCAL_ONLY_BRIDGES = ["crashReportsAPI", "wslAPI", "fileOpenAPI"];
/** Bridges that stay up for every window, local or remote. */
const SHARED_BRIDGES = ["kirocrew", "electronAPI", "localGatewayAPI", "zoomAPI", "browserAPI", "updateAPI"];

function loadPreload({ argv = [] } = {}) {
  const exposed = {};
  const fakeElectron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: {
      send: () => {},
      invoke: () => Promise.resolve(),
      on: () => {},
      once: () => {},
      removeListener: () => {},
      removeAllListeners: () => {},
    },
    webUtils: { getPathForFile: () => "" },
  };
  const origLoad = Module._load;
  const origArgv = process.argv;
  Module._load = function (request, ...rest) {
    if (request === "electron") return fakeElectron;
    return origLoad.call(this, request, ...rest);
  };
  // The preload reads `process.argv` at module load, exactly where Electron
  // surfaces webPreferences.additionalArguments to a preload script.
  process.argv = [...origArgv, ...argv];
  try {
    delete require.cache[require.resolve(PRELOAD)];
    require(PRELOAD);
  } finally {
    process.argv = origArgv;
    Module._load = origLoad;
    delete require.cache[require.resolve(PRELOAD)];
  }
  return exposed;
}

test("a window opened against a remote crew gets no local-only bridge", () => {
  const exposed = loadPreload({ argv: [REMOTE_FLAG] });
  for (const bridge of LOCAL_ONLY_BRIDGES) {
    assert.equal(
      bridge in exposed,
      false,
      `${bridge} must not be exposed to a remote gateway's SPA: its channels answer facts about this machine`,
    );
  }
  for (const bridge of SHARED_BRIDGES) {
    assert.equal(typeof exposed[bridge], "object", `${bridge} must still be exposed`);
  }
  // The one local-only channel that lives on a shared bridge is withheld as a
  // METHOD, so the SPA's `typeof fn !== "function"` no-bridge path takes over.
  assert.equal("clearPaneHttpCache" in exposed.electronAPI, false, "pane:clear-http-cache is local-only too");
  assert.equal(typeof exposed.electronAPI.onStatus, "function", "the rest of electronAPI is untouched");
  // Nothing else about the shell's identity changes for a remote window.
  assert.equal(exposed.kirocrew.isElectron, true);
});

test("a window on this machine's own gateway exposes every bridge", () => {
  const exposed = loadPreload();
  for (const bridge of [...LOCAL_ONLY_BRIDGES, ...SHARED_BRIDGES]) {
    assert.equal(typeof exposed[bridge], "object", `${bridge} must be exposed to the local dashboard`);
  }
  assert.equal(typeof exposed.crashReportsAPI.get, "function");
  assert.equal(typeof exposed.crashReportsAPI.reveal, "function");
  assert.equal(typeof exposed.wslAPI.detect, "function");
  assert.equal(typeof exposed.fileOpenAPI.open, "function");
  assert.equal(typeof exposed.electronAPI.clearPaneHttpCache, "function");
});

test("the frameless-Linux argument does not stand in for the remote one", () => {
  // Two independent launch-time arguments ride the same list; neither implies
  // the other.
  const exposed = loadPreload({ argv: ["--kc-linux-frameless"] });
  assert.equal(exposed.kirocrew.linuxFrameless, true);
  for (const bridge of LOCAL_ONLY_BRIDGES) {
    assert.equal(typeof exposed[bridge], "object", `${bridge} is a question of the gateway, not the frame`);
  }
});
