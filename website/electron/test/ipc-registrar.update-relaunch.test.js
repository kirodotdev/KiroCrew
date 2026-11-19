"use strict";

// The update modal's Restart now: `update:can-relaunch` must answer exactly
// what `update:relaunch` would do for the same sender, so the dashboard never
// offers a restart the shell then refuses.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { harness, updaterHandle } = require("./ipc-registrar-test-helpers");

const event = (url = "http://localhost:5476/") => ({
  sender: { id: "update-sender" },
  senderFrame: { url },
});

function relaunchHandlers(options = {}) {
  const relaunches = [];
  const h = harness({
    ...options,
    initAutoUpdate: () => updaterHandle({ relaunch: () => relaunches.push("relaunch") }),
  });
  h.registerUpdater(h.registrar);
  return {
    can: h.handlers.get("update:can-relaunch"),
    relaunch: h.handlers.get("update:relaunch"),
    relaunches,
  };
}

const REFUSED = [
  ["wrong origin", {}, "https://remote.example/"],
  ["remote window", { wslGatewayLocal: false }, undefined],
  ["port held by a forwarder", { primaryPortOwner: "foreign" }, undefined],
  ["unknown port owner", { primaryPortOwner: "unknown" }, undefined],
];

test("update:can-relaunch answers false wherever update:relaunch refuses", async () => {
  for (const [label, options, url] of REFUSED) {
    const h = relaunchHandlers(options);
    assert.equal(await h.can(event(url)), false, label);
    assert.deepEqual(await h.relaunch(event(url)), {
      ok: false,
      error: "relaunch is only available from the local dashboard",
    }, label);
    assert.deepEqual(h.relaunches, [], `${label}: a refused sender must not relaunch`);
  }
});

test("update:can-relaunch answers true where update:relaunch goes ahead", async () => {
  for (const primaryPortOwner of ["kirocrew", "service"]) {
    const h = relaunchHandlers({ primaryPortOwner });
    assert.equal(await h.can(event()), true, primaryPortOwner);
    assert.deepEqual(h.relaunches, [], "asking never relaunches");
    assert.deepEqual(await h.relaunch(event()), { ok: true }, primaryPortOwner);
    assert.deepEqual(h.relaunches, ["relaunch"], primaryPortOwner);
  }
});
