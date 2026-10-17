/**
 * instanceIsLive — the gate on ever calling `connect`.
 *
 * `POST /api/instances/{id}/connect` is "open tunnel + mint token", i.e. a WRITE
 * with side effects. Mochi FOLLOWS core's tunnels and must never open one: the pet
 * reconciles every 5s, so a connect on a down instance would mean the shell
 * silently establishing SSH connections the user never asked for, each able to
 * block for as long as SSH takes to fail.
 *
 * This is therefore a deny-by-default gate: anything short of a positive
 * "connected with a real port" must NOT be attempted.
 */
const test = require("node:test");
const assert = require("node:assert");
const Module = require("node:module");

/** mochi/index.js is not requireable in a unit test (it boots Electron), so mirror the
 *  predicate here and assert the copy stays honest by shape, not by import. */
function instanceIsLive(inst) {
  const localPort = inst ? Number(inst.local_port) : 0;
  return !!(
    inst &&
    inst.status &&
    inst.status.state === "connected" &&
    Number.isInteger(localPort) &&
    localPort > 0
  );
}

test("a connected instance with a real port is live", () => {
  assert.strictEqual(
    instanceIsLive({ id: "a", local_port: 7778, status: { state: "connected" } }),
    true,
  );
});

test("every non-connected state is refused, so no tunnel is ever opened", () => {
  for (const state of ["disconnected", "connecting", "error", undefined, "", "CONNECTED"]) {
    assert.strictEqual(
      instanceIsLive({ id: "a", local_port: 7778, status: { state } }),
      false,
      `state ${JSON.stringify(state)} must not be attempted`,
    );
  }
});

test("connected but with no usable port is refused", () => {
  // local_port 0 is the registry's UNALLOCATED sentinel — 'connected' without a
  // port cannot serve a page, and treating it as live would build a URL on :0.
  for (const port of [0, undefined, null, -1, "abc", 1.5]) {
    assert.strictEqual(
      instanceIsLive({ id: "a", local_port: port, status: { state: "connected" } }),
      false,
      `port ${JSON.stringify(port)} must not be attempted`,
    );
  }
});

test("a missing status block is refused, not assumed connected", () => {
  assert.strictEqual(instanceIsLive({ id: "a", local_port: 7778 }), false);
  assert.strictEqual(instanceIsLive(null), false);
  assert.strictEqual(instanceIsLive(undefined), false);
  assert.strictEqual(instanceIsLive({}), false);
});

test("the predicate in mochi/index.js still matches this one", () => {
  // Guard against the copy above drifting from the real gate. Compares SOURCE
  // rather than behaviour because mochi/index.js cannot be imported here.
  const fs = require("node:fs");
  const path = require("node:path");
  const src = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");
  const start = src.indexOf("function instanceIsLive(");
  assert.ok(start !== -1, "instanceIsLive must still exist in mochi/index.js");
  const body = src.slice(start, src.indexOf("\n}", start));
  for (const needle of ['state === "connected"', "local_port", "Number.isInteger", "> 0"]) {
    assert.ok(body.includes(needle), `mochi/index.js instanceIsLive lost its ${needle} check`);
  }
  void Module;
});
