"use strict";

// wakatime:open-in-browser: Connect with WakaTime hands the dashboard's own
// WakaTime panel to the system browser. The URL is built in the main process
// from backendUrl; a refused sender opens nothing. Kept out of
// ipc-registrar.test.js so its channel tests don't grow that file.

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { harness, wslEvent, PICK_REJECTIONS } = require("./ipc-registrar-test-helpers");

const WAKATIME_PANEL_URL = "http://localhost:5476/settings/overview?view=wakatime";

test("wakatime:open-in-browser opens only this dashboard's WakaTime panel and answers true", async () => {
  const h = harness();
  h.registrar.registerShell();
  assert.equal(await h.handlers.get("wakatime:open-in-browser")(wslEvent(), "https://evil.example"), true);
  assert.deepEqual(h.shellCalls, [["openExternal", WAKATIME_PANEL_URL]]);
});

test("wakatime:open-in-browser answers false and logs when the OS hand-off fails", async () => {
  const h = harness({ openExternalRejects: true });
  h.registrar.registerShell();
  assert.equal(await h.handlers.get("wakatime:open-in-browser")(wslEvent()), false);
  assert.deepEqual(h.shellCalls, [["openExternal", WAKATIME_PANEL_URL]]);
  assert.ok(h.logs.includes("wakatime:open-in-browser failed: no browser registered"));
});

test("wakatime:open-in-browser is refused outside the local dashboard and opens nothing", async () => {
  for (const [label, options, url] of PICK_REJECTIONS) {
    const h = harness(options);
    h.registrar.registerShell();
    await assert.rejects(
      () => h.handlers.get("wakatime:open-in-browser")(wslEvent(url)),
      (error) => {
        assert.equal(
          error && error.message,
          "wakatime:open-in-browser is restricted to the local dashboard",
          label,
        );
        return true;
      },
    );
    assert.equal(h.shellCalls.length, 0, `${label}: a rejected sender must open nothing`);
  }
});
