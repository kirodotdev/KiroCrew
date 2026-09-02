/**
 * The AppArmor grant a DIRECTLY LAUNCHED Kiro Crew needs, and why the app cannot
 * just apply it itself.
 *
 * `kirocrew service install` installs a NAMED AppArmor profile and lets systemd
 * apply it to the gateway unit. A double-clicked AppImage has no unit: this
 * process execs the bundled backend directly, so nothing transitions either of
 * them into a profile. On Ubuntu 23.10+ (`kernel.apparmor_restrict_unprivileged_
 * userns=1`) that leaves the agent sandbox unbuildable and every agent spawn
 * fails closed.
 *
 * Three things this module deliberately does NOT do, each measured or ruled out:
 *
 * 1. **Re-exec the backend under `aa-exec`.** Entering a named profile needs
 *    `aa_change_onexec`, which an unprivileged unconfined process is not
 *    permitted to do — and `aa-exec` does not fail loudly when it cannot
 *    transition, it silently execs the command unconfined. So this would look
 *    like it worked while changing nothing.
 * 2. **Escalate.** `sudo` needs a TTY this GUI does not have, and shipping a
 *    polkit action would itself have to be installed as root. The privileged
 *    step belongs in a terminal, so the app's job is to name the exact command.
 * 3. **Attach a profile to the AppImage's runtime mount.** `/tmp/.mount_XXXXXX`
 *    is a fresh random path on every launch and world-writable besides. The
 *    attachment target is `$APPIMAGE`, the durable file the user launched.
 */

"use strict";

/**
 * Decide whether this launch needs the sandbox profile advisory.
 *
 * Pure: every input is injected so the decision can be tested without a Linux
 * host, an AppImage, or a live AppArmor policy. Reading the sysctl is the
 * caller's job (see `readSysctl`) for the same reason.
 *
 * @param {object} opts
 * @param {string} opts.platform - `process.platform`.
 * @param {Record<string,string|undefined>} opts.env - `process.env`.
 * @param {(path: string) => string|null} opts.readSysctl - returns the sysctl
 *   contents, or null when it cannot be read (not Ubuntu, no AppArmor).
 * @param {string} [opts.cliBin] - absolute path to the bundled `kirocrew` CLI.
 *   REQUIRED in practice: this persona installed no CLI, so there is no
 *   `kirocrew` on their PATH and a bare command would be `command not found`.
 * @returns {{appImagePath: string, command: string, reason: string}|null}
 *   null when nothing is needed — a non-Linux host, a host that does not
 *   restrict user namespaces, or a launch that is not a direct AppImage.
 */
function describeSandboxProfileNeed({ platform, env, readSysctl, cliBin }) {
  if (platform !== "linux") return null;

  // $APPIMAGE is set by the AppImage runtime to the absolute path of the file
  // the user launched. Absent means this is a dev run, a .deb install, or a
  // reused gateway — none of which this advisory addresses.
  const appImagePath = String((env && env.APPIMAGE) || "").trim();
  if (!appImagePath) return null;

  // The sysctl being exactly 1 is the discriminator for the whole feature, NOT
  // the presence of AppArmor: Debian 13 ships AppArmor and is unaffected, while
  // every Ubuntu derivative (Pop!_OS, Mint, Zorin, elementary) inherits the
  // restriction and would be missed by an /etc/os-release check.
  let sysctl = null;
  try {
    sysctl = readSysctl("/proc/sys/kernel/apparmor_restrict_unprivileged_userns");
  } catch {
    return null;
  }
  if (String(sysctl || "").trim() !== "1") return null;

  // Name the CLI by absolute path. The AppImage is documented as needing "no
  // Python, pip, npm, or Node", so the CLI exists only INSIDE this bundle —
  // printing `kirocrew` would hand the affected user `command not found` and
  // leave them with only the sandbox opt-out.
  const cli = String(cliBin || "").trim() || "kirocrew";

  return {
    appImagePath,
    // Quoted because an AppImage can legitimately live under a path with
    // spaces, and this string is meant to be pasted into a shell as-is.
    command:
      `${shellQuote(cli)} sandbox install-profile --path ${shellQuote(appImagePath)}`,
    reason:
      "kernel.apparmor_restrict_unprivileged_userns=1 and no AppArmor profile is " +
      "attached to this AppImage, so the agent sandbox cannot be built and agent " +
      "spawns will fail closed",
  };
}

/**
 * Single-quote a value for safe pasting into a POSIX shell.
 *
 * A filename is attacker-influenced for a downloaded AppImage, and this string
 * is printed for a human to paste, so `Kiro-Crew-$(...).AppImage` must not have
 * its substitution executed. Embedded single quotes are closed, escaped, and
 * reopened — the standard `'\''` dance.
 */
function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

module.exports = { describeSandboxProfileNeed, shellQuote };
