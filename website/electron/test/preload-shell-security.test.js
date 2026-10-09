"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { describe, it } = require("node:test");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");

function exposedFor(protocol, pathname = "/edition-loading.html") {
  const exposed = new Map();
  const sent = [];
  const electron = {
    contextBridge: {
      exposeInMainWorld(name, api) {
        exposed.set(name, api);
      },
    },
    ipcRenderer: {
      invoke() {},
      on() {},
      removeListener() {},
      send(...args) {
        sent.push(args);
      },
    },
    webUtils: { getPathForFile: () => "" },
  };
  vm.runInNewContext(SOURCE, {
    location: { protocol, pathname },
    process: { argv: [], platform: "linux", getHeapStatistics: () => ({}) },
    require(name) {
      assert.equal(name, "electron");
      return electron;
    },
  });
  return { exposed, sent };
}

describe("preload local-shell boundary", () => {
  it("ships a deny-by-default CSP on every stock local shell page", () => {
    for (const page of ["loading.html", "token-prompt.html"]) {
      const html = fs.readFileSync(path.join(__dirname, "..", page), "utf8");
      assert.match(html, /http-equiv="Content-Security-Policy"/);
      assert.match(html, /default-src 'none'/);
      assert.match(html, /connect-src 'none'/);
      assert.match(html, /frame-src 'none'/);
      assert.match(html, /form-action 'none'/);
    }
  });

  it("keeps every local shell page on the boot signals and caption bridge", () => {
    for (const page of ["loading.html", "edition-loading.html", "token-prompt.html"]) {
      const { exposed } = exposedFor("file:", `/app/${page}`);
      assert.deepEqual([...exposed.keys()], ["kirocrew", "electronAPI"]);
      assert.deepEqual(
        Object.keys(exposed.get("electronAPI")).sort(),
        ["bootComplete", "onBootReady", "onStatus"],
      );
      assert.deepEqual(Object.keys(exposed.get("kirocrew")).sort(), [
        "linuxFrameless",
        "platform",
        "windowControl",
      ]);
    }
  });

  it("forwards only the injected caption actions from local shell pages", () => {
    // The frameless-Linux caption buttons call these three actions on every
    // page, including the token prompt and an edition splash.
    for (const page of ["loading.html", "edition-loading.html", "token-prompt.html"]) {
      const { exposed, sent } = exposedFor("file:", `/app/${page}`);
      const bridge = exposed.get("kirocrew");
      bridge.windowControl("devtools");
      bridge.windowControl("");
      assert.deepEqual(sent, []);
      for (const action of ["minimize", "maximize-toggle", "close"]) {
        bridge.windowControl(action);
      }
      assert.deepEqual(sent, [
        ["window-control", "minimize"],
        ["window-control", "maximize-toggle"],
        ["window-control", "close"],
      ]);
    }
  });

  it("retains the full bridge on the dashboard origin", () => {
    const { exposed } = exposedFor("http:");
    for (const name of ["kirocrew", "electronAPI", "localGatewayAPI", "updateAPI"]) {
      assert.equal(exposed.has(name), true, `${name} must remain available to the dashboard`);
    }
  });
});
