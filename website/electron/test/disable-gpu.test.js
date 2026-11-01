"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const {
  shouldDisableGpu,
  isGpuLessHost,
  gpuDisableSwitches,
  initGpuPolicy,
} = require("../disable-gpu");

// --- isGpuLessHost ---------------------------------------------------------

test("isGpuLessHost: RDP session is GPU-less", () => {
  assert.strictEqual(isGpuLessHost({ env: { SESSIONNAME: "RDP-Tcp#0" } }), true);
});

test("isGpuLessHost: console session is not GPU-less", () => {
  assert.strictEqual(isGpuLessHost({ env: { SESSIONNAME: "Console" } }), false);
});

test("isGpuLessHost: no SESSIONNAME is not GPU-less", () => {
  assert.strictEqual(isGpuLessHost({ env: {} }), false);
});

// --- shouldDisableGpu: explicit controls (PR #6985 behaviour) --------------

test("force OFF via env truthy", () => {
  assert.strictEqual(shouldDisableGpu({ env: { KIROCREW_DISABLE_GPU: "1" }, argv: [] }), true);
});

test("force OFF via --disable-gpu flag", () => {
  assert.strictEqual(shouldDisableGpu({ env: {}, argv: ["--disable-gpu"] }), true);
});

// --- shouldDisableGpu: new force-ON override -------------------------------

test("force ON via env falsy beats RDP auto-detect", () => {
  assert.strictEqual(
    shouldDisableGpu({ env: { KIROCREW_DISABLE_GPU: "0", SESSIONNAME: "RDP-Tcp#0" }, argv: [] }),
    false
  );
});

test("force ON via --enable-gpu beats RDP auto-detect", () => {
  assert.strictEqual(
    shouldDisableGpu({ env: { SESSIONNAME: "RDP-Tcp#0" }, argv: ["--enable-gpu"] }),
    false
  );
});

// --- shouldDisableGpu: auto-detect -----------------------------------------

test("auto-detect: RDP host disables GPU with no explicit control", () => {
  assert.strictEqual(shouldDisableGpu({ env: { SESSIONNAME: "RDP-Tcp#0" }, argv: [] }), true);
});

test("auto-detect: normal workstation keeps GPU", () => {
  assert.strictEqual(shouldDisableGpu({ env: { SESSIONNAME: "Console" }, argv: [] }), false);
});

test("default: empty env keeps GPU", () => {
  assert.strictEqual(shouldDisableGpu({ env: {}, argv: [] }), false);
});

// --- switches + initGpuPolicy ----------------------------------------------

test("gpuDisableSwitches returns the exact trio", () => {
  assert.deepStrictEqual(gpuDisableSwitches(), [
    "disable-gpu",
    "disable-gpu-compositing",
    "disable-software-rasterizer",
  ]);
});

test("initGpuPolicy applies all switches when disabled (RDP auto)", () => {
  const applied = [];
  const res = initGpuPolicy({
    appendSwitch: (n) => applied.push(n),
    env: { SESSIONNAME: "RDP-Tcp#0" },
    argv: [],
  });
  assert.strictEqual(res.disabled, true);
  assert.deepStrictEqual(applied, [
    "disable-gpu",
    "disable-gpu-compositing",
    "disable-software-rasterizer",
  ]);
});

test("initGpuPolicy applies nothing on a normal workstation", () => {
  const applied = [];
  const res = initGpuPolicy({
    appendSwitch: (n) => applied.push(n),
    env: { SESSIONNAME: "Console" },
    argv: [],
  });
  assert.strictEqual(res.disabled, false);
  assert.deepStrictEqual(applied, []);
});

test("initGpuPolicy survives a throwing appendSwitch", () => {
  const res = initGpuPolicy({
    appendSwitch: () => { throw new Error("boom"); },
    env: { KIROCREW_DISABLE_GPU: "1" },
    argv: [],
    log: () => {},
  });
  assert.strictEqual(res.disabled, true);
  assert.deepStrictEqual(res.switches, []); // all failed, but no throw
});
