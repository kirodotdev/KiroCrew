"use strict";

// Which systems reach connected-computer sharing from the desktop app: the
// preview read and the consent-only host follow the same platform list as the
// app's own host, and both read the data folder the app hands its gateway.
// Shared fakes live in gateway-supervisor-test-helpers.js.

const { test } = require("node:test");
const assert = require("node:assert");

const { harness } = require("./gateway-supervisor-test-helpers");
const { CONNECTED_COMPUTER_PLATFORMS } = require("../gateway-supervisor");

// The folder the harness hands the supervisor as its gateway's data home.
const GATEWAY_HOME = "/virtual/kirocrew-home";

function platformHarness(platform, { inheritedHome = GATEWAY_HOME } = {}) {
  const previewCalls = [];
  const h = harness({
    processRef: {
      platform,
      arch: "x64",
      env: { KIROCREW_HOME: inheritedHome },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    execFileSyncFn: (_file, args, options) => {
      if (Array.isArray(args) && args[0] === "connected-computer" && args[1] === "preview") {
        previewCalls.push({ args, env: options && options.env });
        return JSON.stringify({ enabled: true });
      }
      throw new Error("no other command answers in this harness");
    },
  });
  return { ...h, previewCalls };
}

test("macOS, Windows and Linux are the connected-computer systems", () => {
  assert.deepStrictEqual([...CONNECTED_COMPUTER_PLATFORMS].sort(), ["darwin", "linux", "win32"]);
});

for (const platform of ["linux", "darwin", "win32"]) {
  test(`the preview is read from the backend on ${platform}`, () => {
    const { supervisor, previewCalls } = platformHarness(platform);
    assert.strictEqual(supervisor.connectedComputersPreview(), true);
    assert.strictEqual(previewCalls.length, 1);
  });
}

test("another system never asks the backend and reads the preview as off", () => {
  const { supervisor, previewCalls } = platformHarness("freebsd");
  assert.strictEqual(supervisor.connectedComputersPreview(), false);
  assert.strictEqual(previewCalls.length, 0);
});

test("the consent-only host is unavailable on another system", () => {
  const { supervisor, spawnCalls } = platformHarness("freebsd");
  const host = supervisor.connectedComputersConsentHost();
  assert.strictEqual(host.start(), false);
  assert.strictEqual(host.state(), "unavailable");
  assert.strictEqual(spawnCalls.length, 0);
});

test("the consent-only host starts on Windows", () => {
  const { supervisor, spawnCalls } = platformHarness("win32");
  const host = supervisor.connectedComputersConsentHost();
  assert.strictEqual(host.start(), true);
  assert.strictEqual(host.state(), "starting");
  assert.strictEqual(spawnCalls.length, 1);
});

test("the preview read and the consent host use the gateway's data folder", () => {
  const { supervisor, spawnCalls, previewCalls } = platformHarness("linux", {
    inheritedHome: "/virtual/some-other-home",
  });
  supervisor.connectedComputersPreview();
  assert.strictEqual(previewCalls[0].env.KIROCREW_HOME, GATEWAY_HOME);
  supervisor.connectedComputersConsentHost().start();
  assert.strictEqual(spawnCalls[0][2].env.KIROCREW_HOME, GATEWAY_HOME);
});
