"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { harness, wslEvent } = require("./ipc-registrar-test-helpers");

const PICK_REJECTIONS = [
  ["wrong origin", {}, "https://remote.example/chat"],
  ["remote window", { wslOwner: { fake: "remote-window" }, wslGatewayLocal: false }, undefined],
  ["foreign port owner", { primaryPortOwner: "foreign" }, undefined],
  ["unknown port owner", { primaryPortOwner: "unknown" }, undefined],
];

// The data-home repair asks the OS for admin rights on this machine, so both
// channels take the full local-dashboard gate, and neither reaches the repair
// from a rejected sender.
function fakeDataHomeFix(available = true) {
  const calls = [];
  const asked = [];
  return {
    calls,
    asked,
    canFix: (opts) => {
      asked.push(opts);
      return available;
    },
    fixDataHome: async (opts) => {
      calls.push(opts);
      return { ok: true };
    },
  };
}

test("data-home:fix is refused outside the local dashboard and never runs the repair", async () => {
  for (const [label, options, url] of PICK_REJECTIONS) {
    const dataHomeFix = fakeDataHomeFix();
    const h = harness({ ...options, dataHomeFix });
    h.registrar.registerShell();
    await assert.rejects(
      () => h.handlers.get("data-home:fix")(wslEvent(url)),
      { message: "data-home:fix is restricted to the local dashboard" },
      label,
    );
    assert.equal(dataHomeFix.calls.length, 0, `${label}: a rejected sender must not run the repair`);
    assert.equal(await h.handlers.get("data-home:can-fix")(wslEvent(url)), false, label);
  }
});

test("data-home channels on the local dashboard pass only the gateway's home, as a check", async () => {
  const dataHomeFix = fakeDataHomeFix();
  const h = harness({ primaryPortOwner: "kirocrew", dataHomeFix });
  h.registrar.registerShell();
  assert.equal(await h.handlers.get("data-home:can-fix")(wslEvent(), "/srv/crew-data"), true);
  assert.deepEqual(dataHomeFix.asked, [{ gatewayHome: "/srv/crew-data" }]);
  assert.deepEqual(await h.handlers.get("data-home:fix")(wslEvent(), "/srv/crew-data"), { ok: true });
  assert.equal(dataHomeFix.calls.length, 1);
  // No target, platform or command from the renderer: data-home-fix resolves those.
  assert.deepEqual(Object.keys(dataHomeFix.calls[0]).sort(), ["gatewayHome", "log"]);
});

test("data-home:can-fix answers false where the repair cannot run here", async () => {
  const h = harness({ primaryPortOwner: "kirocrew", dataHomeFix: fakeDataHomeFix(false) });
  h.registrar.registerShell();
  assert.equal(await h.handlers.get("data-home:can-fix")(wslEvent(), "/srv/crew-data"), false);
});
