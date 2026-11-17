"use strict";

// preload.js loaded for real against a fake `electron`, with each answer the
// shell can give to `dashboard:is-remote-gateway` (window-lifecycle.js). A
// window whose gateway is not this machine's must show its SPA no local-only
// bridge at all -- as in a plain browser tab -- so it never invokes a channel
// the main process refuses.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

const PRELOAD = path.join(__dirname, "..", "preload.js");
const LOCAL_ONLY = ["remoteCrewSetupAPI", "crashReportsAPI", "wslAPI", "fileOpenAPI", "folderPickAPI", "externalTerminalAPI", "relayCrewAPI"];

function loadPreload(answer) {
  const exposed = {};
  const fakeElectron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: {
      send() {},
      sendSync: (channel) => (channel === "dashboard:is-remote-gateway" ? answer : undefined),
      invoke: () => Promise.resolve(),
      on() {},
      removeListener() {},
    },
    webUtils: { getPathForFile: () => "" },
  };
  const origLoad = Module._load;
  Module._load = function (request, ...rest) {
    if (request === "electron") return fakeElectron;
    return origLoad.call(this, request, ...rest);
  };
  try {
    delete require.cache[require.resolve(PRELOAD)];
    require(PRELOAD);
  } finally {
    Module._load = origLoad;
  }
  return exposed;
}

test("a remote-crew window gets no local-only bridge; a local window keeps them", () => {
  const remote = loadPreload(true);
  const local = loadPreload(false);
  const unanswered = loadPreload(undefined);
  for (const bridge of LOCAL_ONLY) {
    assert.equal(bridge in remote, false, `${bridge} must be withheld from a remote gateway's SPA`);
    assert.equal(bridge in unanswered, false, `${bridge} must be withheld unless the shell answers local`);
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
  // A window that loaded as local can be set up as client-only before its next
  // load, so the bridge cannot be withheld from either answer.
  for (const exposed of [loadPreload(true), loadPreload(false)]) {
    assert.equal(typeof exposed.remoteCrewPaneAPI.forwardPorts, "function");
  }
});
