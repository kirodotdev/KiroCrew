// The theme-accent-changed and theme-splash-changed listeners: both write under
// userData for the next launch's first paint, so only the main window's own
// dashboard (its top frame, on its assigned origin) may report them.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { harness, lastCall } = require("./ipc-registrar-test-helpers");

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
const DASHBOARD = "http://localhost:5476";

function themeHarness(owner = "main") {
  const h = harness();
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "theme-ipc-"));
  h.electron.app.getPath = (name) => {
    assert.equal(name, "userData");
    return userData;
  };
  const main = { fake: "main-window", _mcBackendUrl: DASHBOARD };
  const other = { fake: "connection-window", _mcBackendUrl: "http://localhost:6100" };
  h.windows.getMainWindow = () => main;
  h.windows.windowForWebContents = () => (owner === "main" ? main : owner === "other" ? other : null);
  h.registrar.registerShell();
  return { h, userData };
}

function frameEvent(url = `${DASHBOARD}/settings`) {
  const frame = { url };
  return { sender: { id: "view", mainFrame: frame }, senderFrame: frame };
}

const subframeEvent = () => {
  const event = frameEvent();
  event.senderFrame = { url: `${DASHBOARD}/embedded` };
  return event;
};

const splashFiles = (dir) => fs.readdirSync(dir).filter((name) => name.startsWith("theme-splash."));

test("theme-splash-changed writes the main window dashboard's art", () => {
  const { h, userData } = themeHarness();
  h.listeners.get("theme-splash-changed")(frameEvent(), { type: "image/png", bytes: PNG });
  assert.deepEqual(splashFiles(userData), ["theme-splash.png"]);
  h.listeners.get("theme-splash-changed")(frameEvent(), null);
  assert.deepEqual(splashFiles(userData), [], "null clears the art");
});

test("theme-splash-changed writes and clears nothing for any other sender", () => {
  const senders = [
    ["other window", "other", frameEvent()],
    ["no window", null, frameEvent()],
    ["subframe", "main", subframeEvent()],
    ["foreign origin", "main", frameEvent("https://evil.example/settings")],
    ["another local port", "main", frameEvent("http://localhost:6100/settings")],
    ["about:blank", "main", frameEvent("about:blank")],
  ];
  for (const [label, owner, event] of senders) {
    const { h, userData } = themeHarness(owner);
    fs.writeFileSync(path.join(userData, "theme-splash.svg"), "<svg/>");
    assert.doesNotThrow(() => h.listeners.get("theme-splash-changed")(event, { type: "image/png", bytes: PNG }), label);
    h.listeners.get("theme-splash-changed")(event, null);
    assert.deepEqual(splashFiles(userData), ["theme-splash.svg"], label);
    assert.ok(h.logs.some((line) => line.startsWith("theme-splash-changed rejected:")), label);
  }
});

test("theme-splash-changed drops a payload that is not null or { type, bytes }", () => {
  const { h, userData } = themeHarness();
  fs.writeFileSync(path.join(userData, "theme-splash.svg"), "<svg/>");
  for (const payload of [undefined, "x", 7, [], {}, { type: "image/png" }, { type: 1, bytes: PNG },
    { type: "image/png", bytes: [1, 2, 3] }]) {
    h.listeners.get("theme-splash-changed")(frameEvent(), payload);
  }
  assert.deepEqual(splashFiles(userData), ["theme-splash.svg"]);
});

test("theme-accent-changed stores only the main window dashboard's accent", () => {
  const ok = themeHarness();
  ok.h.listeners.get("theme-accent-changed")(frameEvent(), "#8E48FF");
  assert.deepEqual(lastCall(ok.h.windowCalls, "chrome.setThemeAccent").slice(1), ["#8E48FF"]);
  for (const [owner, event] of [["other", frameEvent()], ["main", subframeEvent()],
    ["main", frameEvent("https://evil.example/")]]) {
    const { h } = themeHarness(owner);
    h.listeners.get("theme-accent-changed")(event, "#000000");
    assert.equal(h.windowCalls.some(([name]) => name === "chrome.setThemeAccent"), false);
  }
});

test("a flood of refused theme reports logs only the throttled burst", () => {
  const { h } = themeHarness("other");
  const event = frameEvent();
  for (let i = 0; i < 50; i += 1) h.listeners.get("theme-splash-changed")(event, null);
  const rejected = h.logs.filter((line) => line.startsWith("theme-splash-changed rejected:"));
  assert.ok(rejected.length > 0 && rejected.length <= 5, `${rejected.length} lines`);
});
