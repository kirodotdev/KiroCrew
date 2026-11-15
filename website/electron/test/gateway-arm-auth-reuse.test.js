// The update lane asks the gateway for its armed record every 30s
// (GATEWAY_ARM_POLL_MS in runtime/update/managed-lane.js). Each credential
// lookup is a fresh mint: a local-secret link, or for a remote crew an
// `ssh <host> kirocrew token`. Each mint takes one of the gateway's 50
// link-nonce slots, so a lookup per request turned over the ring and other
// pending links with it. The client reuses one credential until the gateway
// refuses it.
const { test } = require("node:test");
const assert = require("node:assert");

const { createGatewayArm } = require("../runtime/update/gateway-arm");

const STATUS = { update_armed_version: null, update_armed_at: null };

function okFetch(seen) {
  return async (url) => {
    seen.push(url);
    return { status: 200, json: async () => STATUS };
  };
}

test("gateway arm resolves its credential once across many polls", async () => {
  let mints = 0;
  const seen = [];
  const arm = createGatewayArm({
    backendUrl: "http://127.0.0.1:1",
    fetchGatewayAuth: async () => {
      mints += 1;
      return { value: `tok${mints}` };
    },
    fetchImpl: okFetch(seen),
  });
  for (let i = 0; i < 10; i += 1) await arm.armedRecord();
  assert.strictEqual(mints, 1, "one mint serves every poll the gateway accepts");
  assert.strictEqual(seen.length, 10);
  assert.ok(seen.every((u) => u.endsWith("token=tok1")));
});

test("gateway arm re-resolves once when the gateway refuses the cached credential", async () => {
  let mints = 0;
  const seen = [];
  const arm = createGatewayArm({
    backendUrl: "http://127.0.0.1:1",
    fetchGatewayAuth: async () => {
      mints += 1;
      return { value: `tok${mints}` };
    },
    fetchImpl: async (url) => {
      seen.push(url);
      // The first link token has lapsed by the second poll.
      if (url.endsWith("token=tok1") && seen.length > 1) return { status: 403, json: async () => null };
      return { status: 200, json: async () => STATUS };
    },
  });
  await arm.armedRecord();
  assert.deepStrictEqual(await arm.armedRecord(), { version: "", armedAt: null });
  assert.strictEqual(mints, 2);
  assert.ok(seen[2].endsWith("token=tok2"), "the retry carries the fresh credential");
  await arm.armedRecord();
  assert.strictEqual(mints, 2, "the fresh credential is reused in turn");
});

test("gateway arm does not loop when a fresh credential is refused too", async () => {
  let mints = 0;
  const arm = createGatewayArm({
    backendUrl: "http://127.0.0.1:1",
    fetchGatewayAuth: async () => {
      mints += 1;
      return { value: `tok${mints}` };
    },
    fetchImpl: async () => ({ status: 403, json: async () => null }),
  });
  assert.strictEqual(await arm.armedRecord(), null);
  assert.strictEqual(mints, 2, "one re-resolve per request, not a loop");
});

test("gateway arm asks again after an empty answer", async () => {
  let calls = 0;
  const arm = createGatewayArm({
    backendUrl: "http://127.0.0.1:1",
    fetchGatewayAuth: async () => {
      calls += 1;
      return { value: calls === 1 ? "" : "tok" };
    },
    fetchImpl: okFetch([]),
  });
  assert.strictEqual(await arm.armedRecord(), null);
  assert.deepStrictEqual(await arm.armedRecord(), { version: "", armedAt: null });
  assert.strictEqual(calls, 2);
});
