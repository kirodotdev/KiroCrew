"use strict";
//
// Opt-in disabling of Chromium hardware acceleration for the desktop shell.
//
// The problem this solves: on hosts with no usable GPU — a VMware/VirtualBox
// guest, a Windows RDP session (SESSIONNAME=RDP-Tcp#*), an X11-forwarded or
// headless Linux box — Chromium's GPU process cannot create a rendering
// context and aborts. Electron then reports the window as
// `render-process-gone` with `reason: "launched-failed"` (exitCode 18 on
// Windows), and `renderer-recovery.js` reloads it three times before giving
// up. The backend Gateway is fine and still listening on :5476; only the
// Electron renderer is dead, so the whole app looks broken on an otherwise
// healthy machine. Observed live in `chromium.log`:
//
//     ContextResult::kFatalFailure: Failed to create shared context for virtualization.
//     GetGpuDriverOverlayInfo: Failed to retrieve video device
//
// The Chromium-level fix is `--disable-gpu` (plus `--disable-gpu-compositing`
// and `--disable-software-rasterizer`, which together stop the GPU process
// from spawning at all — see electron/electron#28164). But a switch the user
// has to remember to pass is a switch that is never set on the launch that
// actually crashed, and — critically — the app's single-instance handoff drops
// argv from a second launch, so `KiroCrew.exe --disable-gpu` does nothing once
// an instance already holds the lock. So the decision is made HERE, before the
// app is ready, from durable inputs (an env var, or argv on the winning
// instance), and applied through the same `appendSwitch` seam `main.js`
// already exposes to `native-logging.js`.
//
// This is deliberately OFF by default: a normal desktop with a real GPU should
// keep hardware acceleration. It is opt-in via `KIROCREW_DISABLE_GPU`
// (mirroring the existing `KIROCREW_*` env conventions: KIROCREW_HOME,
// KIROCREW_PORT, KIROCREW_DEBUG) or the `--disable-gpu` command-line flag.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so the decision has to be testable without a live `app`
// (same pattern as native-logging.js / renderer-recovery.js / perf-metrics.js).
//

/** Truthy env values, matching how the rest of the shell reads KIROCREW_* flags. */
const TRUTHY = new Set(["1", "true", "yes", "on"]);

/**
 * Whether GPU acceleration should be disabled for this launch.
 *
 * Inputs, in order of intent (any one is enough):
 *   1. `KIROCREW_DISABLE_GPU` env set to a truthy value (1/true/yes/on).
 *   2. `--disable-gpu` present in argv.
 *
 * @param {object} deps
 * @param {NodeJS.ProcessEnv} [deps.env]   Defaults to process.env at call time.
 * @param {string[]}          [deps.argv]  Defaults to process.argv at call time.
 * @returns {boolean}
 */
function shouldDisableGpu({ env = process.env, argv = process.argv } = {}) {
  const raw = env && env.KIROCREW_DISABLE_GPU;
  if (typeof raw === "string" && TRUTHY.has(raw.trim().toLowerCase())) return true;
  if (Array.isArray(argv) && argv.includes("--disable-gpu")) return true;
  return false;
}

/**
 * The Chromium switches that fully stop the GPU process from spawning.
 *
 * Returned as data (not applied inline) so a test can assert the exact switch
 * names: these are Chromium's spelling, and a typo fails silently (an unknown
 * switch is ignored). `--disable-gpu` alone is not enough on modern Chromium —
 * the GPU process still spawns for rasterization — so the compositing and
 * software-rasterizer switches accompany it (electron/electron#28164).
 *
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
 * Apply the GPU-disable switches if the launch opted in. Never throws.
 *
 * Must run BEFORE the app is ready: Chromium reads these switches during
 * initialization, so appending them later is accepted and then ignored — the
 * same timing constraint as the native-logging switches.
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
      // One rejected switch must not cost us the others, nor the boot.
      log(`gpu-disable switch --${name} failed: ${e && e.message}`);
    }
  }
  log(`gpu acceleration DISABLED for this launch: switches=${applied.join(",") || "none"}`);
  return { disabled: true, switches: applied };
}

module.exports = {
  shouldDisableGpu,
  gpuDisableSwitches,
  initGpuPolicy,
  TRUTHY,
};
