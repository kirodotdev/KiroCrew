"use strict";
//
// Turn an unrecoverable Windows renderer death into a named cause and a remedy.
//
// The problem this solves: when the sandbox cannot start a child process, the
// bounded reload in renderer-recovery.js can never win — every replacement
// renderer dies the same way — so it spends its three attempts and logs
// "renderer recovery exhausted", which says what stopped but not what is wrong
// or what to do. The reporter who found this had to read Chromium's own log
// lines, decode 0x80000003 by hand, and bisect four launch-flag combinations to
// discover that the sandbox was the failing component. Everything they deduced
// is available to us at the moment we give up.
//
// This module only DESCRIBES. It does not relax the sandbox on the user's
// behalf, and that restraint is deliberate: silently dropping a security
// boundary because a process crashed would turn any renderer-crash bug into an
// isolation downgrade, and on a managed device the decision belongs to whoever
// owns the device's security posture. So the app names the cause, names the
// narrowest lever, and stops. Same division of labour as sandbox-profile.js,
// which prints the AppArmor command rather than attempting it.
//
// Pure logic + injected inputs, so the mapping is testable without a Windows
// host or a dying renderer.
//

/**
 * `STATUS_BREAKPOINT` (0x80000003) as Electron reports it in `exitCode`.
 *
 * Chromium's child-process launcher raises this from a CHECK during sandbox
 * setup, so it reads as a debugger breakpoint rather than as an access
 * violation, and searching the number finds nothing about graphics. Both the
 * signed and unsigned readings are accepted: Electron surfaces the signed
 * int32, while the same value copied out of Event Viewer or a crash dump is
 * usually written unsigned.
 */
const STATUS_BREAKPOINT_SIGNED = -2147483645;
const STATUS_BREAKPOINT_UNSIGNED = 2147483651;

/**
 * `render-process-gone` reasons that mean the process never got off the ground.
 *
 * A renderer that ran and then died gives `crashed` or `oom`. These two mean it
 * failed during launch or failed its integrity check, which is what a blocked
 * sandbox and a rejected injected DLL respectively look like from here.
 */
const STARTUP_FAILURE_REASONS = new Set(["launch-failed", "integrity-failure"]);

/**
 * Whether this death looks like the Windows sandbox refusing to start a child.
 *
 * @param {object} o
 * @param {string} [o.reason] `render-process-gone` reason.
 * @param {number|null} [o.exitCode]
 * @returns {boolean}
 */
function looksLikeSandboxStartupFailure({ reason, exitCode } = {}) {
  if (STARTUP_FAILURE_REASONS.has(String(reason || ""))) return true;
  const code = Number(exitCode);
  return code === STATUS_BREAKPOINT_SIGNED || code === STATUS_BREAKPOINT_UNSIGNED;
}

/**
 * Describe a Windows sandbox startup failure, or return null.
 *
 * @param {object} o
 * @param {string} o.platform `process.platform`.
 * @param {string} [o.reason]
 * @param {number|null} [o.exitCode]
 * @param {boolean} [o.alreadyOptedOut] True when this launch already ran with
 *   the sandbox fully disabled. Then the sandbox is NOT what killed the
 *   renderer and this advice would send the reader down a dead end.
 * @returns {{cause: string, remedy: string[]}|null}
 */
function describeWindowsSandboxFailure({
  platform,
  reason,
  exitCode,
  alreadyOptedOut = false,
} = {}) {
  if (platform !== "win32") return null;
  if (alreadyOptedOut) return null;
  if (!looksLikeSandboxStartupFailure({ reason, exitCode })) return null;

  return {
    cause:
      "every replacement renderer died the same way during launch " +
      `(reason=${reason || "unknown"}, exitCode=${exitCode}), which is how ` +
      "Chromium's sandbox reports that it cannot start a child process on this " +
      "host. The usual cause is software that injects a DLL into every process " +
      "(endpoint security, or a display driver such as DisplayLink), which the " +
      "sandbox then refuses to admit.",
    // Narrowest first. Each line keeps the sandbox on except the last, and the
    // first is the one an enterprise fleet already has a Chrome policy for.
    remedy: [
      "Relax ONE hardening layer, narrowest first, by setting an environment " +
        "variable and relaunching (a command-line flag is dropped when an " +
        "instance is already running):",
      "  KIROCREW_SANDBOX_COMPAT=renderer-code-integrity",
      "  KIROCREW_SANDBOX_COMPAT=renderer-app-container",
      "  KIROCREW_SANDBOX_COMPAT=network-service-sandbox",
      "  KIROCREW_SANDBOX_COMPAT=gpu-sandbox",
      "Last resort, disables isolation for every child process:",
      "  KIROCREW_SANDBOX_COMPAT=off",
      "Better than any of these, if you own the device policy: add a " +
        "process exclusion for this app in your endpoint security product, " +
        "which fixes the cause and keeps the sandbox. The faulting module " +
        "named in the Windows Event Viewer AppCrash entry identifies it.",
    ],
  };
}

module.exports = {
  describeWindowsSandboxFailure,
  looksLikeSandboxStartupFailure,
  STATUS_BREAKPOINT_SIGNED,
  STATUS_BREAKPOINT_UNSIGNED,
  STARTUP_FAILURE_REASONS,
};
