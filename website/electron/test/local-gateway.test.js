const { test } = require("node:test");
const assert = require("node:assert");
const {
  LOCAL_GATEWAY_KEY,
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
  classifyStartFailure,
} = require("../local-gateway");

/** Minimal electron-store stand-in: the two methods these helpers use. */
function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get: (key) => data[key],
    set: (key, value) => { data[key] = value; },
  };
}

test("isLocalGatewayEnabled: a store that has never held the key reads as enabled", () => {
  assert.equal(isLocalGatewayEnabled(fakeStore()), true);
});

test("isLocalGatewayEnabled: only an explicit false disables it", () => {
  assert.equal(isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: false })), false);
  assert.equal(isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: true })), true);
});

test("isLocalGatewayEnabled: a non-boolean stored value is not a request to stop", () => {
  // A hand-edited config carrying "false" or 0 is malformed, not an opt-out —
  // reading it as one would silently stop starting the gateway.
  for (const value of ["false", 0, null, "", "no"]) {
    assert.equal(
      isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: value })),
      true,
      `stored ${JSON.stringify(value)} should leave the gateway enabled`,
    );
  }
});

test("setLocalGatewayEnabled: writes a real boolean and returns what it wrote", () => {
  const store = fakeStore();
  assert.equal(setLocalGatewayEnabled(store, false), false);
  assert.equal(store.data[LOCAL_GATEWAY_KEY], false);
  assert.equal(isLocalGatewayEnabled(store), false);

  assert.equal(setLocalGatewayEnabled(store, true), true);
  assert.equal(store.data[LOCAL_GATEWAY_KEY], true);
  assert.equal(isLocalGatewayEnabled(store), true);
});

test("setLocalGatewayEnabled: coerces a truthy non-boolean rather than storing it raw", () => {
  const store = fakeStore();
  assert.equal(setLocalGatewayEnabled(store, "yes"), true);
  assert.strictEqual(store.data[LOCAL_GATEWAY_KEY], true);
});

// ── classifyStartFailure ──

test("classifyStartFailure: a disabled record is client-only", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, failure: { disabled: true, port: 5476 } }),
    "client-only",
  );
});

test("classifyStartFailure: client-only OUTRANKS a stale port-in-use log line", () => {
  // The launch log survives across launches, so a bound-port line from an
  // earlier run must not offer to force-stop a holder of a silent port.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { disabled: true, port: 5476 },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "client-only",
  );
});

test("classifyStartFailure: a refused incomplete bundle is 'installing', not a crash", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, failure: { incompleteBundle: true } }),
    "installing",
  );
});

test("classifyStartFailure: installing OUTRANKS a stale port-in-use log line", () => {
  // Nothing was spawned, so a bound-port line left by an earlier run must not
  // offer to force-stop a holder that this refusal says nothing about.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { incompleteBundle: true },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "installing",
  );
});

test("classifyStartFailure: client-only outranks an incomplete bundle", () => {
  // Both can hold at once on a client-only install that also has a partial
  // bundle; the user turned the local gateway off, so that is the real story.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { disabled: true, incompleteBundle: true },
    }),
    "client-only",
  );
});

test("classifyStartFailure: a real port conflict still wins when nothing is disabled", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, isOwnPort: true, portInUseInLog: true }),
    "port-conflict",
  );
});

test("classifyStartFailure: a bound port on ANOTHER window's port is not our conflict", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, isOwnPort: false, portInUseInLog: true }),
    "failed",
  );
});

test("classifyStartFailure: a plain spawn failure and a timeout stay distinct", () => {
  assert.equal(classifyStartFailure({ failedToStart: true }), "failed");
  assert.equal(classifyStartFailure({ failedToStart: false }), "unreachable");
  assert.equal(classifyStartFailure(), "unreachable");
});
