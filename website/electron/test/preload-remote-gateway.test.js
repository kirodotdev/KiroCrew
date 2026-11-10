"use strict";

// preload.js loaded for real against a fake `electron`, with and without the
// `--kc-remote-gateway` argument window-lifecycle.js passes a window whose
// gateway is not this machine's. That window's SPA must see no local-only
// bridge at all -- as in a plain browser tab -- so it never invokes a channel
// the main process refuses.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

const PRELOAD = path.join(__dirname, "..", "preload.js");
const LOCAL_ONLY = ["remoteCrewSetupAPI", "crashReportsAPI", "wslAPI", "fileOpenAPI", "folderPickAPI", "externalTerminalAPI", "relayCrewAPI"];

function loadPreload(argv) {
  const exposed = {};
  const fakeElectron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: { send() {}, invoke: () => Promise.resolve(), on() {}, removeListener() {} },
    webUtils: { getPathForFile: () => "" },
  };
  const origLoad = Module._load;
  const origArgv = process.argv;
  Module._load = function (request, ...rest) {
    if (request === "electron") return fakeElectron;
    return origLoad.call(this, request, ...rest);
  };
  // Electron hands webPreferences.additionalArguments to a preload as argv.
  process.argv = [...origArgv, ...argv];
  try {
    delete require.cache[require.resolve(PRELOAD)];
    require(PRELOAD);
  } finally {
    process.argv = origArgv;
    Module._load = origLoad;
  }
  return exposed;
}

test("a remote-crew window gets no local-only bridge; a local window keeps them", () => {
  const remote = loadPreload(["--kc-remote-gateway"]);
  const local = loadPreload([]);
  for (const bridge of LOCAL_ONLY) {
    assert.equal(bridge in remote, false, `${bridge} must be withheld from a remote gateway's SPA`);
    assert.equal(typeof local[bridge], "object", `${bridge} must stay exposed to the local dashboard`);
  }
  assert.equal("clearPaneHttpCache" in remote.electronAPI, false);
  assert.equal(typeof local.electronAPI.clearPaneHttpCache, "function");
  assert.equal("notifyPolicyGoverned" in remote.updateAPI, false);
  assert.equal(typeof local.updateAPI.notifyPolicyGoverned, "function");
  // Bridges about the window itself are unaffected.
  for (const bridge of ["kirocrew", "localGatewayAPI", "closeActionAPI", "zoomAPI", "updateAPI"]) {
    assert.equal(typeof remote[bridge], "object", `${bridge} must still be exposed`);
  }
});

test("both windows can ask for pane forwards; a remote gateway's window is the one that needs them", () => {
  // A window set up as client-only after it was created still carries the
  // local argument, so the bridge cannot be withheld from either.
  for (const exposed of [loadPreload(["--kc-remote-gateway"]), loadPreload([])]) {
    assert.equal(typeof exposed.remoteCrewPaneAPI.forwardPorts, "function");
  }
});
