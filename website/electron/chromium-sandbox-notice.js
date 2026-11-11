"use strict";
//
// The notice a Linux AppImage launch shows when it is running with Chromium's
// sandbox turned off.
//
// electron-builder's AppRun probes `unshare -Ur true` and, when the host refuses
// an unprivileged user namespace, starts Electron with `--no-sandbox`. An
// AppImage runs from a FUSE mount, which cannot carry Chromium's setuid
// `chrome-sandbox` helper, so user namespaces are the only sandbox it can use.
// The switch is applied before this process exists, so the app cannot undo it;
// what it can do is say so. Otherwise the only trace is the process argv, and
// the window shows the splash and nothing else.
//
// On the common host (Ubuntu 23.10+, `kernel.apparmor_restrict_unprivileged_
// userns=1`) the same restriction makes the agent sandbox fail closed, and one
// command fixes both: `kirocrew sandbox install-profile` attaches a `userns`
// profile to the AppImage path, the AppImage runtime and everything it starts
// inherit it, and AppRun's probe passes on the next launch. The command names
// the bundled CLI by absolute path because the AppImage puts no `kirocrew` on
// PATH (see sandbox-profile.js).
//
// What this never does: apply or drop a sandbox switch itself. It only explains
// the one AppRun chose and names the supported remedy.
//
// Pure logic plus injected dependencies, like renderer-exhausted-notice.js, so
// the text and the button wiring are testable without Electron.
//

const { describeSandboxProfileNeed } = require("./sandbox-profile");

const NO_SANDBOX_SWITCH = "--no-sandbox";

/** Store key set when the user ticks "Don't show this again". */
const DISMISSED_KEY = "chromiumSandboxNoticeDismissed";

/** Install guide section for hosts that deny user namespaces another way. */
const OTHER_CAUSES_URL =
  "https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/install.md#other-reasons-user-namespaces-can-be-denied";

const MESSAGE = "Kiro Crew is running with Chromium's sandbox turned off";

const WHY =
  "Why: an AppImage runs from a FUSE mount, where Chromium's own sandbox " +
  "helper cannot work, so Chromium needs unprivileged user namespaces " +
  "instead. Without them the AppImage starts Kiro Crew with --no-sandbox.";

// The packages put only `kirocrew-desktop` on PATH; the one-line install is
// what provides `kirocrew` (docs/guides/install.md, "Which path on Linux").
const NO_CLI_ON_PATH =
  "The AppImage does not install a kirocrew command. The one-line install " +
  "puts kirocrew on your PATH, and the .deb and .rpm packages set up the " +
  "sandbox for you.";

/**
 * Decide whether this launch needs the notice, and what it says.
 *
 * @param {object} opts
 * @param {string} opts.platform - `process.platform`.
 * @param {Record<string,string|undefined>} opts.env - `process.env`.
 * @param {string[]} opts.argv - `process.argv`, where AppRun's switch lands.
 * @param {(path: string) => string|null} opts.readSysctl - see sandbox-profile.js.
 * @param {string} [opts.cliBin] - absolute path to the bundled `kirocrew` CLI.
 * @returns {{message: string, detail: string, command: string}|null}
 *   null unless this is an AppImage launch running with `--no-sandbox`.
 *   `command` is empty when no single command fixes this host.
 */
function describeChromiumSandboxOff({ platform, env, argv, readSysctl, cliBin }) {
  if (platform !== "linux") return null;
  if (!String((env && env.APPIMAGE) || "").trim()) return null;
  if (!Array.isArray(argv) || !argv.includes(NO_SANDBOX_SWITCH)) return null;

  const need = describeSandboxProfileNeed({ platform, env, readSysctl, cliBin });
  if (need) {
    return {
      message: MESSAGE,
      detail:
        "Agents cannot run, and the app runs without Chromium's sandbox, " +
        "because this system blocks unprivileged user namespaces " +
        "(kernel.apparmor_restrict_unprivileged_userns=1).\n\n" +
        "To fix both, run this command in a terminal while Kiro Crew is open, " +
        "then restart Kiro Crew. Copy command puts it on your clipboard. It " +
        "asks for your password when it needs sudo, and it applies to the " +
        "AppImage where it is now, so keep the AppImage there:\n\n" +
        need.command +
        "\n\n" + WHY + "\n\n" +
        "The command uses the kirocrew CLI inside this app. " + NO_CLI_ON_PATH,
      command: need.command,
    };
  }
  return {
    message: MESSAGE,
    detail:
      "The app runs without Chromium's sandbox, because this system does not " +
      "allow unprivileged user namespaces. The install guide lists the causes " +
      "and how to fix each one.\n\n" +
      WHY + "\n\n" + NO_CLI_ON_PATH,
    command: "",
  };
}

/**
 * Show the notice once for this launch, unless the user dismissed it for good.
 *
 * @param {object} deps
 * @param {ReturnType<typeof describeChromiumSandboxOff>} deps.notice
 * @param {{showMessageBox: Function}} deps.dialog - Electron `dialog`.
 * @param {{writeText: (text: string) => void}} deps.clipboard - Electron `clipboard`.
 * @param {(url: string) => Promise<void>|void} deps.openExternal - `shell.openExternal`.
 * @param {{get: Function, set: Function}} deps.store - the app's electron-store.
 * @param {object} [deps.window] - parent BrowserWindow, when one is alive.
 * @returns {Promise<"none"|"dismissed"|"copied"|"guide"|"closed">}
 */
async function showChromiumSandboxNotice({ notice, dialog, clipboard, openExternal, store, window }) {
  if (!notice) return "none";
  if (store.get(DISMISSED_KEY) === true) return "dismissed";

  const options = {
    type: "warning",
    title: "Kiro Crew",
    message: notice.message,
    detail: notice.detail,
    buttons: [notice.command ? "Copy command" : "Open install guide in browser", "Close"],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    checkboxLabel: "Don't show this again",
    checkboxChecked: false,
  };
  const parent = window && !window.isDestroyed() ? window : null;
  const { response, checkboxChecked } = parent
    ? await dialog.showMessageBox(parent, options)
    : await dialog.showMessageBox(options);

  if (checkboxChecked) store.set(DISMISSED_KEY, true);
  if (response !== 0) return "closed";
  if (notice.command) {
    clipboard.writeText(notice.command);
    return "copied";
  }
  await openExternal(OTHER_CAUSES_URL);
  return "guide";
}

module.exports = {
  describeChromiumSandboxOff,
  showChromiumSandboxNotice,
  DISMISSED_KEY,
  OTHER_CAUSES_URL,
};
