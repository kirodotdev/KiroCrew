"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { createRemoteCrewPrompt } = require("../runtime/gateway/remote-crew-prompt");

/** Render the form and return its HTML; closing without a save resolves null. */
async function renderForm({ isWindows, initial }) {
  let html = "";
  class FakeWindow extends EventEmitter {
    setMenu() {}
    isDestroyed() { return false; }
    loadURL(url) {
      html = decodeURIComponent(url.replace(/^data:text\/html;charset=utf-8,/, ""));
      this.emit("closed");
    }
  }
  const { promptRemoteCrew } = createRemoteCrewPrompt({
    BrowserWindow: FakeWindow,
    nativeTheme: { shouldUseDarkColors: false },
    isWindows,
  });
  assert.equal(await promptRemoteCrew(null, 5477, initial), null);
  return html;
}

test("the tunnel option is offered on macOS and Linux, keeping the stored choice", async () => {
  const html = await renderForm({ isWindows: false, initial: { host: "devbox", manageTunnel: true } });
  assert.match(html, /id="mt" checked>/);
  assert.match(html, /Keep an SSH tunnel to this crew open/);
});

test("the tunnel option is not offered on Windows, where it would do nothing", async () => {
  const html = await renderForm({ isWindows: true, initial: { host: "devbox", manageTunnel: true } });
  assert.doesNotMatch(html, /id="mt"/);
  assert.doesNotMatch(html, /Keep an SSH tunnel/);
});
