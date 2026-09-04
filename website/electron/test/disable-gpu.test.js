"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  shouldDisableGpu,
  gpuDisableSwitches,
  initGpuPolicy,
} = require("../disable-gpu.js");

test("shouldDisableGpu: off by default (no env, no flag)", () => {
  assert.equal(shouldDisableGpu({ env: {}, argv: ["node", "main.js"] }), false);
});

test("shouldDisableGpu: truthy KIROCREW_DISABLE_GPU values enable it", () => {
  for (const v of ["1", "true", "TRUE", "yes", "on", " On "]) {
    assert.equal(
      shouldDisableGpu({ env: { KIROCREW_DISABLE_GPU: v }, argv: [] }),
      true,
      `expected ${JSON.stringify(v)} to enable`
    );
  }
});

test("shouldDisableGpu: falsey/garbage env values do NOT enable it", () => {
  for (const v of ["0", "false", "no", "off", "", "maybe"]) {
    assert.equal(
      shouldDisableGpu({ env: { KIROCREW_DISABLE_GPU: v }, argv: [] }),
      false,
      `expected ${JSON.stringify(v)} to stay disabled`
    );
  }
});

test("shouldDisableGpu: --disable-gpu argv flag enables it", () => {
  assert.equal(
    shouldDisableGpu({ env: {}, argv: ["node", "main.js", "--disable-gpu"] }),
    true
  );
});

test("gpuDisableSwitches: exact Chromium switch names, no leading dashes", () => {
  assert.deepEqual(gpuDisableSwitches(), [
    "disable-gpu",
    "disable-gpu-compositing",
    "disable-software-rasterizer",
  ]);
});

test("initGpuPolicy: no-op and appends nothing when opted out", () => {
  const seen = [];
  const res = initGpuPolicy({
    appendSwitch: (n) => seen.push(n),
    env: {},
    argv: [],
  });
  assert.equal(res.disabled, false);
  assert.deepEqual(seen, []);
});

test("initGpuPolicy: appends all switches when opted in", () => {
  const seen = [];
  const res = initGpuPolicy({
    appendSwitch: (n) => seen.push(n),
    env: { KIROCREW_DISABLE_GPU: "1" },
    argv: [],
  });
  assert.equal(res.disabled, true);
  assert.deepEqual(seen, [
    "disable-gpu",
    "disable-gpu-compositing",
    "disable-software-rasterizer",
  ]);
  assert.deepEqual(res.switches, seen);
});

test("initGpuPolicy: a throwing appendSwitch does not abort the rest", () => {
  const seen = [];
  const res = initGpuPolicy({
    appendSwitch: (n) => {
      if (n === "disable-gpu-compositing") throw new Error("boom");
      seen.push(n);
    },
    env: { KIROCREW_DISABLE_GPU: "true" },
    argv: [],
  });
  assert.equal(res.disabled, true);
  // The middle switch threw; the other two still applied.
  assert.deepEqual(seen, ["disable-gpu", "disable-software-rasterizer"]);
});

test("main wires GPU policy in the lock winner before Electron becomes ready", () => {
  // Chromium consumes these switches during initialization. Pin the call site,
  // not just the pure policy, so a refactor cannot leave a fully tested helper
  // disconnected or let a rejected second instance mutate the primary launch.
  const source = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const lock = source.indexOf("if (!app.requestSingleInstanceLock())");
  const winner = source.indexOf("} else {", lock);
  const nativeLogging = source.indexOf("initNativeLogging({", winner);
  const gpuPolicy = source.indexOf("initGpuPolicy({", nativeLogging);
  const secondInstance = source.indexOf('app.on("second-instance"', gpuPolicy);
  const ready = source.indexOf("app.whenReady().then", gpuPolicy);

  assert.ok(lock >= 0 && winner > lock, "expected the single-instance winner branch");
  assert.ok(nativeLogging > winner, "native logging must remain inside the lock winner");
  assert.ok(gpuPolicy > nativeLogging, "GPU policy must follow native logging in the winner");
  assert.ok(secondInstance > gpuPolicy, "GPU policy must precede second-instance registration");
  assert.ok(ready > gpuPolicy, "GPU policy must run before app ready");
});
