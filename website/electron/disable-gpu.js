"use strict";
//
// Opt-in + auto-detect disabling of Chromium hardware acceleration for the
// desktop shell. (Builds on PR #6985's opt-in-only version.)
//
// The problem: on hosts with no usable GPU — a VMware/VirtualBox guest, a
// Windows RDP session (SESSIONNAME=RDP-Tcp#*), an X11-forwarded or headless
// Linux box — Chromium's GPU process cannot create a rendering context.
// Two observed failure modes:
//   * at launch: render-process-gone reason="launched-failed", exitCode 18.
//   * at runtime: GPU process exited unexpectedly exit_code=-1069547419
//     (0xC0000409), renderer reason="crashed"; renderer-recovery.js loops.
// The backend Gateway is fine on :5476 — only the Electron renderer is dead.
//
// The Chromium fix is the switch trio disable-gpu / disable-gpu-compositing /
// disable-software-rasterizer (electron/electron#28164), applied before the
// app is ready through the appendSwitch seam main.js already exposes.
//
// Decision precedence (see shouldDisableGpu):
//   1. force ON  (KIROCREW_DISABLE_GPU in {0,false,no,off} | --enable-gpu)
//   2. force OFF (KIROCREW_DISABLE_GPU in {1,true,yes,on}  | --disable-gpu)
//   3. auto      (no explicit control AND a GPU-less host, e.g. RDP)
//   4. default   (hardware acceleration ON)
//
// A normal desktop with a real GPU is never affected.
//

/** Truthy env values, matching how the rest of the shell reads KIROCREW_* flags. */
const TRUTHY = new Set(["1", "true", "yes", "on"]);
/** Falsy env values — explicit force-ON of hardware acceleration. */
const FALSY = new Set(["0", "false", "no", "off"]);

/**
 * Whether this host has no usable GPU and should default to software rendering.
 * Conservative, signal-based — never guesses from "looks like a VM" alone.
 *
 * @param {object} deps
 * @param {NodeJS.ProcessEnv} [deps.env]
 * @returns {boolean}
 */
function isGpuLessHost({ env = process.env } = {}) {
  const session = env && env.SESSIONNAME;
  if (typeof session === "string" && /^RDP-/i.test(session.trim())) return true;
  return false;
}

/**
 * Whether GPU acceleration should be disabled for this launch.
 *
 * @param {object} deps
 * @param {NodeJS.ProcessEnv} [deps.env]   Defaults to process.env at call time.
 * @param {string[]}          [deps.argv]  Defaults to process.argv at call time.
 * @returns {boolean}
 */
function shouldDisableGpu({ env = process.env, argv = process.argv } = {}) {
  const raw = env && env.KIROCREW_DISABLE_GPU;
  const val = typeof raw === "string" ? raw.trim().toLowerCase() : null;

  // 1. Explicit force-ON wins over everything (escape hatch).
  if (val !== null && FALSY.has(val)) return false;
  if (Array.isArray(argv) && argv.includes("--enable-gpu")) return false;

  // 2. Explicit force-OFF (unchanged behaviour from the opt-in PR).
  if (val !== null && TRUTHY.has(val)) return true;
  if (Array.isArray(argv) && argv.includes("--disable-gpu")) return true;

  // 3. Auto-detect a GPU-less host (RDP/VM) when nothing explicit was set.
  if (isGpuLessHost({ env })) return true;

  // 4. Normal workstation with a GPU.
  return false;
}

/**
 * The Chromium switches that fully stop the GPU process from spawning.
 * @returns {string[]}
 */
function gpuDisableSwitches() {
  return [
    "disable-gpu",
    "disable-gpu-compositing",
    "disable-software-rasterizer",
  ];
}

/**
 * Apply the GPU-disable switches if this launch should run GPU-less.
 * Never throws. Must run BEFORE the app is ready.
 *
 * @param {object} deps
 * @param {(name: string, value?: string) => void} deps.appendSwitch
 * @param {NodeJS.ProcessEnv} [deps.env]
 * @param {string[]}          [deps.argv]
 * @param {(msg: string) => void} [deps.log]
 * @returns {{disabled: boolean, switches: string[]}}
 */
function initGpuPolicy({ appendSwitch, env, argv, log = () => {} } = {}) {
  const disabled = shouldDisableGpu({ env, argv });
  const applied = [];
  if (!disabled) return { disabled: false, switches: applied };

  for (const name of gpuDisableSwitches()) {
    try {
      appendSwitch(name);
      applied.push(name);
    } catch (e) {
      log(`gpu-disable switch --${name} failed: ${e && e.message}`);
    }
  }
  log(`gpu acceleration DISABLED for this launch: switches=${applied.join(",") || "none"}`);
  return { disabled: true, switches: applied };
}

module.exports = {
  shouldDisableGpu,
  isGpuLessHost,
  gpuDisableSwitches,
  initGpuPolicy,
  TRUTHY,
  FALSY,
};
