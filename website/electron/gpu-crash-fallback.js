"use strict";
//
// Automatic software-rendering fallback for a Windows host whose GPU process
// cannot start.
//
// The problem this solves: on some Windows hosts (VDI, remote sessions, a GPU
// driver Chromium cannot use) the Electron GPU process dies the moment it is
// launched. Chromium's own recovery restarts it in the next mode of its chain
// (hardware -> SwiftShader -> display-compositor-only), but every mode still
// runs in a separate GPU *process*, and on these hosts every one of them dies
// the same way. The renderer loses its GPU channel and dies with it, so
// `renderer-recovery.js` spends its three reloads, and then Chromium aborts the
// whole app with `FATAL: GPU process isn't usable. Goodbye.` What the user sees
// is "the app starts and exits within seconds, no window, no logs". Observed on
// a reporter's host as:
//
//     GPU process exited unexpectedly: exit_code=-2147483645
//     renderer died (reason=crashed, exitCode=-2147483645) — reloading dashboard
//     renderer died 4x within 60000ms — giving up to avoid a reload loop
//     FATAL:gpu_data_manager_impl_private.cc:416 GPU process isn't usable. Goodbye.
//
// `--disable-gpu` alone did not help that host, because it only shortens
// Chromium's mode chain; a GPU process is still spawned for the display
// compositor, and it still dies. What did recover the host is
// `--in-process-gpu`: GPU work runs on a thread inside the browser process, so
// there is no GPU child to die, with `--use-angle=swiftshader` so that thread
// never touches the unusable driver.
//
// This module makes that recovery automatic and bounded:
//
//   1. When a GPU child process dies BEFORE the dashboard has ever finished
//      loading in this run, the decision "this host needs software rendering"
//      is persisted to the electron-store and the app relaunches itself once.
//   2. On the next start, the persisted decision is read before Chromium
//      initializes and the software-rendering switches are applied, so the
//      relaunch (and every later launch) never spawns the GPU process at all.
//   3. The decision is keyed to the app version. A new build gets one attempt at
//      hardware rendering again, because a newer Electron or driver may have
//      fixed the host; if the GPU process still dies, the fallback re-arms and
//      relaunches once more. That is the whole retry budget: one relaunch per
//      app version, never a loop.
//
// Why ONE startup death is enough: the trigger is already narrow — a GPU
// process that dies of a crash-class reason on Windows before the dashboard
// has rendered a single frame. A host whose GPU works does not lose its GPU
// process in the first seconds of boot; the hosts that do are the ones this
// exists for, and on them Chromium's own retries all die the same way and end
// in `GPU process isn't usable. Goodbye.` The cost of being wrong on a healthy
// host is bounded and visible: software rendering for this app version only
// (the next build tries hardware again), announced on every boot by a log line
// that names the store key to delete. Waiting for a second death would risk
// Chromium aborting the app first, which is the reporter's symptom.
//
// What deliberately does NOT trigger it:
//   - A GPU crash AFTER the dashboard has loaded. That is a working host whose
//     GPU hiccuped mid-session; Chromium's own fallback and renderer-recovery
//     handle it, and relaunching would kill the user's in-flight work to fix a
//     problem that is not "the app cannot start".
//   - A GPU crash while software rendering is already active. There is no
//     further mode to fall to; the log says so and the app is left to the
//     existing recovery paths.
//   - Any platform other than Windows. The switch set was validated on the
//     reporter's Windows host; the opt-in `disable-gpu.js` path covers the rest.
//   - Renderer-only deaths. A renderer that dies without a GPU death is not a
//     rendering problem, and software rendering would not fix it.
//
// What is NEVER applied here: `--no-sandbox`. Dropping the Chromium sandbox is a
// security-posture change and not a rendering fallback. A host whose sandbox
// blocks every child still fails after this fallback, and that failure is left
// to be diagnosed, not papered over.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so every decision has to be testable without a live `app`
// (same pattern as disable-gpu.js / renderer-recovery.js).
//

/** electron-store key holding the persisted decision (or null). */
const STORE_KEY = "gpuSoftwareFallback";

/** `details.type` Electron reports for the GPU child in `child-process-gone`. */
const GPU_PROCESS_TYPE = "GPU";

/**
 * `details.reason` values meaning "the GPU process died on its own". A
 * `clean-exit` is Chromium shutting it down; `killed` is the OS or the app
 * tearing it down (including our own quit); `oom` is memory, which software
 * rendering only makes worse. None of those says the host cannot run a GPU
 * process, so none of them arms the fallback.
 */
const FALLBACK_REASONS = new Set(["crashed", "abnormal-exit", "launch-failed"]);

/** The only platform the fallback fires on; see the header. */
const FALLBACK_PLATFORM = "win32";

/**
 * The Chromium switches that move rendering fully in-process and off the GPU
 * driver. Returned as data so a test can pin the exact spelling: an unknown
 * switch is silently ignored by Chromium, so a typo here fails silently.
 *
 * This is the reporter's tested set minus its two sandbox switches
 * (`--no-sandbox`, `--disable-gpu-sandbox`), which are never applied here.
 *
 * @returns {Array<[string] | [string, string]>} `[name]` or `[name, value]`.
 */
function softwareRenderingSwitches() {
  return [
    ["disable-gpu"],
    ["disable-gpu-compositing"],
    ["in-process-gpu"],
    ["use-angle", "swiftshader"],
    ["use-gl", "angle"],
  ];
}

/**
 * The opt-in `disable-gpu.js` switch that contradicts software rendering.
 * `--disable-software-rasterizer` tells Chromium it may NOT fall back to
 * SwiftShader, so if an operator also set `KIROCREW_DISABLE_GPU=1` (env and
 * argv survive `app.relaunch()`), the software-mode boot would request
 * SwiftShader with `--use-angle=swiftshader` while forbidding it — the same
 * no-window exit this module exists to fix. It is removed before the software
 * switches are applied. Spelling pinned against `gpuDisableSwitches()` by test.
 */
const RASTERIZER_DISABLE_SWITCH = "disable-software-rasterizer";

/**
 * The persisted decision, if it applies to THIS build. Anything else — no
 * record, a malformed record, a record written by another app version — reads
 * as "not armed", so a stale or hand-edited store can never keep a host in
 * software mode past the build that needed it.
 *
 * @param {object} deps
 * @param {{get: (key: string) => unknown}} deps.store
 * @param {string} deps.version   `app.getVersion()`.
 * @param {string} [deps.platform]
 * @returns {null | {version: string, at: string, reason: string, exitCode: unknown}}
 */
function readArmedFallback({ store, version, platform = process.platform } = {}) {
  if (platform !== FALLBACK_PLATFORM) return null;
  let record;
  try {
    record = store.get(STORE_KEY);
  } catch {
    return null;
  }
  if (!record || typeof record !== "object") return null;
  if (typeof record.version !== "string" || record.version !== version) return null;
  return record;
}

/**
 * Apply the software-rendering switches. Never throws.
 *
 * Must run BEFORE the app is ready: Chromium reads these during initialization,
 * so a later append is accepted and then ignored — the same timing constraint
 * as `disable-gpu.js`.
 *
 * @param {object} deps
 * @param {(name: string, value?: string) => void} deps.appendSwitch
 * @param {(name: string) => void} [deps.removeSwitch]  Drops a switch an
 *   earlier policy appended; runs first so `--disable-software-rasterizer`
 *   from the `disable-gpu.js` opt-in cannot veto the SwiftShader request.
 * @param {(msg: string) => void} [deps.log]
 * @returns {string[]} the switches applied, spelled `name` or `name=value`.
 */
function applySoftwareRendering({ appendSwitch, removeSwitch = () => {}, log = () => {} } = {}) {
  const applied = [];
  try {
    removeSwitch(RASTERIZER_DISABLE_SWITCH);
  } catch (e) {
    log(`gpu-fallback: could not remove --${RASTERIZER_DISABLE_SWITCH}: ${e && e.message}`);
  }
  for (const [name, value] of softwareRenderingSwitches()) {
    try {
      if (value === undefined) appendSwitch(name);
      else appendSwitch(name, value);
      applied.push(value === undefined ? name : `${name}=${value}`);
    } catch (e) {
      // One rejected switch must not cost us the others, nor the boot.
      log(`gpu-fallback switch --${name} failed: ${e && e.message}`);
    }
  }
  return applied;
}

/**
 * Create the crash-time coordinator.
 *
 * @param {object} deps
 * @param {{get: (k: string) => unknown, set: (k: string, v: unknown) => void}} deps.store
 * @param {string} deps.version           `app.getVersion()`; keys the decision.
 * @param {string} deps.backendUrl        The dashboard origin; a finished load
 *   of a document at this origin is what ends the startup phase.
 * @param {boolean} deps.softwareActive   Whether this run already booted in
 *   software mode (the armed record was applied).
 * @param {() => void} deps.relaunch      Relaunch the app; called at most once.
 * @param {() => boolean} [deps.isQuitting]
 * @param {string} [deps.platform]
 * @param {(msg: string) => void} [deps.log]
 * @param {() => string} [deps.now]       ISO timestamp for the record.
 */
function createGpuCrashFallback({
  store,
  version,
  backendUrl,
  softwareActive = false,
  relaunch,
  isQuitting = () => false,
  platform = process.platform,
  log = () => {},
  now = () => new Date().toISOString(),
} = {}) {
  let dashboardLoaded = false;
  let relaunching = false;

  /**
   * Record that a top-level document finished loading. Only a document at the
   * dashboard's own origin ends the startup phase: the boot splash
   * (`loading.html`) loads long before the gateway is ready, and on the
   * affected hosts the GPU process is already dead by then, so counting it
   * would make the fallback miss exactly the failure it exists for.
   */
  function noteDocumentLoaded(url) {
    if (dashboardLoaded) return;
    if (isDashboardDocument(url, backendUrl)) dashboardLoaded = true;
  }

  /**
   * Decide what to do about a `child-process-gone` event.
   *
   * @returns {string} one of "ignored-not-gpu" | "ignored-reason" |
   *   "ignored-platform" | "ignored-quitting" | "ignored-after-dashboard" |
   *   "ignored-software-active" | "ignored-already-relaunching" |
   *   "persist-failed" | "relaunched".
   */
  function handleChildGone(details = {}) {
    if (details.type !== GPU_PROCESS_TYPE) return "ignored-not-gpu";
    const reason = String(details.reason || "");
    const detail = `reason=${reason}, exitCode=${details.exitCode}`;
    if (!FALLBACK_REASONS.has(reason)) {
      log(`gpu process gone (${detail}) — not a crash, no fallback`);
      return "ignored-reason";
    }
    if (platform !== FALLBACK_PLATFORM) {
      log(`gpu process crashed (${detail}) — software fallback is Windows-only, leaving it to Chromium`);
      return "ignored-platform";
    }
    if (isQuitting()) {
      log(`gpu process gone during quit (${detail}) — not recovering`);
      return "ignored-quitting";
    }
    if (softwareActive) {
      log(
        `gpu process crashed (${detail}) with software rendering already active — ` +
          `nothing further to fall back to`,
      );
      return "ignored-software-active";
    }
    if (dashboardLoaded) {
      log(`gpu process crashed (${detail}) after the dashboard loaded — leaving it to Chromium's own recovery`);
      return "ignored-after-dashboard";
    }
    if (relaunching) return "ignored-already-relaunching";

    // Persist FIRST, and only relaunch once the record is verifiably on disk.
    // A relaunch without the record would boot in hardware mode, crash the
    // same way and relaunch again: the loop this module exists to prevent.
    const record = { version, at: now(), reason, exitCode: details.exitCode };
    let persisted = false;
    try {
      store.set(STORE_KEY, record);
      const back = store.get(STORE_KEY);
      persisted = !!back && typeof back === "object" && back.version === version;
    } catch (e) {
      log(`gpu-fallback: could not persist the decision: ${e && e.message}`);
    }
    if (!persisted) {
      log(`gpu process crashed (${detail}) before the dashboard loaded, but the fallback could not be persisted — not relaunching`);
      return "persist-failed";
    }

    relaunching = true;
    log(
      `gpu process crashed (${detail}) before the dashboard loaded — ` +
        `relaunching once with software rendering (persisted for ${version})`,
    );
    try {
      relaunch();
    } catch (e) {
      // Never let a failed relaunch escape into Electron's event emitter. The
      // record stays: the user's next manual launch still gets software mode.
      log(`gpu-fallback relaunch failed: ${e && e.message}`);
    }
    return "relaunched";
  }

  return {
    handleChildGone,
    noteDocumentLoaded,
    get dashboardLoaded() {
      return dashboardLoaded;
    },
  };
}

/**
 * Whether `url` is a top-level document at the dashboard's own origin.
 * Origin comparison, not prefix: `http://127.0.0.1:5476` must not match
 * `http://127.0.0.1:54760`.
 */
function isDashboardDocument(url, backendUrl) {
  try {
    return new URL(String(url)).origin === new URL(String(backendUrl)).origin;
  } catch {
    return false;
  }
}

/**
 * Wire the whole thing into a live Electron `app`. Must run in the
 * single-instance winner, before `app` is ready, and after the opt-in GPU
 * policy so the log reads in cause order. Never throws.
 *
 * @param {object} deps
 * @param {import("electron").App} deps.app
 * @param {object} deps.store             electron-store instance.
 * @param {string} deps.backendUrl
 * @param {() => boolean} deps.isQuitting
 * @param {() => void} deps.requestQuit   The app's own quit path, so the
 *   gateway is stopped the same way a user quit stops it.
 * @param {(msg: string) => void} [deps.log]
 * @param {string} [deps.platform]        Defaults to process.platform.
 * @returns {{softwareActive: boolean, switches: string[]}}
 */
function initGpuCrashFallback({
  app,
  store,
  backendUrl,
  isQuitting,
  requestQuit,
  log = () => {},
  platform = process.platform,
} = {}) {
  const version = app.getVersion();
  const armed = readArmedFallback({ store, version, platform });
  let switches = [];
  if (armed) {
    switches = applySoftwareRendering({
      appendSwitch: (name, value) => app.commandLine.appendSwitch(name, value),
      removeSwitch: (name) => app.commandLine.removeSwitch(name),
      log,
    });
    log(
      `gpu: software rendering ACTIVE (armed ${armed.at} after GPU process ` +
        `${armed.reason} exitCode=${armed.exitCode}); switches=${switches.join(",") || "none"}; ` +
        `to try hardware rendering again, remove "${STORE_KEY}" from ${store.path}`,
    );
  }

  const fallback = createGpuCrashFallback({
    store,
    version,
    backendUrl,
    softwareActive: !!armed,
    isQuitting,
    platform,
    log,
    relaunch: () => {
      app.relaunch();
      requestQuit();
    },
  });

  app.on("web-contents-created", (_event, contents) => {
    contents.on("did-finish-load", () => {
      try {
        fallback.noteDocumentLoaded(contents.getURL());
      } catch {
        // A destroyed webContents has no URL; the latch simply stays unset.
      }
    });
  });
  app.on("child-process-gone", (_event, details) => {
    fallback.handleChildGone(details || {});
  });

  return { softwareActive: !!armed, switches };
}

module.exports = {
  STORE_KEY,
  GPU_PROCESS_TYPE,
  FALLBACK_REASONS,
  FALLBACK_PLATFORM,
  RASTERIZER_DISABLE_SWITCH,
  softwareRenderingSwitches,
  readArmedFallback,
  applySoftwareRendering,
  createGpuCrashFallback,
  isDashboardDocument,
  initGpuCrashFallback,
};
