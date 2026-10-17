"use strict";
//
// The readable error for a dashboard window whose renderer could not be kept
// alive.
//
// The problem this solves: `renderer-recovery.js` reloads a dead renderer a
// bounded number of times and then gives up, and until now giving up only wrote
// one line to the launch log. The window itself stayed mapped and BLACK, with
// no tab strip, no text and no hint of what happened; the user's only way out
// was to find the tray icon and quit. On a host where Chromium cannot start a
// sandboxed child process at all (seen on corporate-managed Windows 11, where
// endpoint-security software hooks every process) that black window is the
// whole first-run experience, and nothing on screen says why.
//
// This module turns the give-up into a native dialog. Native on purpose: an
// error page would need a renderer, which is exactly the process that keeps
// dying, so a BrowserWindow could not show it. `dialog.showMessageBox` is drawn
// by the browser process and works with zero Chromium children alive.
//
// The dialog always appears when recovery gives up, on every platform, whatever
// the cause. It does not try to decide whether the death is "the sandbox" before
// speaking: a wrong classifier would leave the users it is written for staring
// at the same black window (the reason the earlier attempt at this feature was
// never merged). What IS cause-specific is one extra paragraph and one extra
// button, added when the three facts the Windows guide uses to tell a blocked
// sandbox from an ordinary renderer crash all hold: Windows, exit code
// `0x80000003` (`STATUS_BREAKPOINT`, the CHECK Chromium raises when
// child-process sandbox setup fails), and no dashboard document ever finished
// loading in this run. A renderer that showed the dashboard and died later with
// the same code is a V8 abort, not a sandbox problem, and gets the generic text.
//
// What this never does: apply `--no-sandbox`, or any other sandbox switch, on
// the user's behalf. Turning process isolation off is a security-posture change
// and the guide records why no durable opt-out exists. The dialog names the
// supported remedy (a process exclusion in the software that blocks the child)
// and opens the guide that explains how to confirm the cause first.
//
// Pure logic + injected dependencies: Electron main is not exercised by the unit
// test runner, so the text, the button wiring and the one-at-a-time guard are
// testable without a live `dialog` (same pattern as renderer-recovery.js).
//

/**
 * `details.exitCode` Chromium reports for `STATUS_BREAKPOINT` (`0x80000003`).
 * Signed, because that is how `render-process-gone` carries it; the only
 * producer of the value this module ever sees is that event.
 */
const STATUS_BREAKPOINT_EXIT_CODE = -2147483645;

/** The only platform whose child-process sandbox fails this way. */
const BLOCKED_SANDBOX_PLATFORM = "win32";

/** The guide entry the dialog's button opens. Public docs on the main branch. */
const TROUBLESHOOTING_URL =
  "https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/windows-install.md#troubleshooting";

const ACTION_RETRY = "retry";
const ACTION_GUIDE = "guide";
const ACTION_QUIT = "quit";
const LABELS = Object.freeze({
  [ACTION_RETRY]: "Try again",
  [ACTION_GUIDE]: "Open troubleshooting guide in browser",
  [ACTION_QUIT]: "Quit",
});

/** `render-process-gone` reasons in the user's words, for the generic dialog. */
const PLAIN_REASONS = Object.freeze({
  crashed: "it crashed",
  oom: "it ran out of memory",
  "abnormal-exit": "it exited unexpectedly",
  "launch-failed": "it could not start",
  "integrity-failure": "its files failed an integrity check",
  killed: "it was stopped by the system",
});

/**
 * Whether a give-up carries the blocked-sandbox signature from the Windows
 * guide. All three facts are required; any one alone is an ordinary crash.
 */
function isBlockedSandboxSignature({ platform, exitCode, dashboardLoaded } = {}) {
  if (platform !== BLOCKED_SANDBOX_PLATFORM) return false;
  if (dashboardLoaded) return false;
  return Number(exitCode) === STATUS_BREAKPOINT_EXIT_CODE;
}

/**
 * Build the dialog for a give-up. Pure: no Electron, no I/O.
 *
 * `actions[i]` names what button `i` does, so the caller maps a response index
 * back to an action without knowing the button order.
 *
 * @param {object} info
 * @param {string} [info.reason]        `render-process-gone` reason.
 * @param {number} [info.exitCode]      `render-process-gone` exit code.
 * @param {number} [info.attempts]      Reloads spent before giving up.
 * @param {string} [info.platform]
 * @param {boolean} [info.dashboardLoaded] A dashboard document finished loading
 *   at least once in this run.
 * @param {string} [info.logPath]       The launch log, named so the user can
 *   attach it to a report.
 * @param {boolean} [info.guideOpened]  The guide button was already used: the
 *   dialog comes back so the user still has Try again and Quit on screen
 *   instead of the bare black window, with Try again as the default and the
 *   guide button kept in place in case the browser tab was closed by mistake.
 * @returns {{options: object, actions: string[], blockedSandbox: boolean}}
 *   `options` is what `dialog.showMessageBox` takes.
 */
function buildRendererExhaustedNotice(info = {}) {
  const blockedSandbox = isBlockedSandboxSignature(info);
  const deaths = Math.max(1, Number(info.attempts) || 0) + 1;
  const reason = String(info.reason || "unknown");
  const exitCode = info.exitCode === undefined ? "unknown" : String(info.exitCode);

  // Order, for a native message box that is read top-down: what happened and
  // what to do first, in the user's words; the codes a report needs last.
  const paragraphs = [];
  if (blockedSandbox) {
    paragraphs.push(
      "Most likely, security software on this Windows device is blocking the " +
        "part of Kiro Crew that draws its window. Kiro Crew will not weaken its " +
        "own safeguards to get around this.",
      "To fix it, ask your IT team to add the Kiro Crew app to that software's " +
        "allow list (a \"process exclusion\"). The troubleshooting guide shows how " +
        "to confirm the cause and what to allow.",
    );
    if (info.guideOpened) {
      paragraphs.push("The troubleshooting guide is open in your browser.");
    }
  } else {
    paragraphs.push(
      `The window stopped working ${deaths} times in a row` +
        (PLAIN_REASONS[reason] ? ` (${PLAIN_REASONS[reason]})` : "") +
        ", so Kiro Crew stopped retrying.",
    );
  }
  // Every way out is named, including the close button: a user who dismisses
  // the box the usual way must not find the app gone.
  paragraphs.push(
    blockedSandbox
      ? "Try again reloads the window once more; it helps only after IT has added " +
        "the app to the allow list. Closing this box does the same as Try again. " +
        "Quit stops Kiro Crew."
      : "Try again reloads the window once more; closing this box does the same. " +
        "Quit stops Kiro Crew.",
  );
  // Raw codes only: the plain words were said once, in the first sentence.
  paragraphs.push(
    "Details for IT or a bug report: " +
      (blockedSandbox
        ? `Kiro Crew's display process exited ${deaths} times before anything was drawn ` +
          `(reason: ${reason}, exit code: ${exitCode} / 0x80000003).`
        : `reason: ${reason}, exit code: ${exitCode}.`) +
      (info.logPath ? `\nLog file to attach: ${info.logPath}` : ""),
  );

  // A blocked sandbox never recovers by reloading, so the guide is the default
  // there. Once it has been opened the default moves to Try again, but the
  // buttons keep their places, and the guide button stays so a browser tab
  // closed by mistake is not the end of the road.
  const actions = blockedSandbox
    ? [ACTION_GUIDE, ACTION_RETRY, ACTION_QUIT]
    : [ACTION_RETRY, ACTION_QUIT];
  const defaultAction = blockedSandbox && !info.guideOpened ? ACTION_GUIDE : ACTION_RETRY;

  return {
    options: {
      type: "error",
      // The heading is the most prominent line of a native box, so it names
      // the cause when one is known instead of the shared symptom.
      message: blockedSandbox
        ? "Security software is blocking Kiro Crew's window."
        : "Kiro Crew's window has stopped working.",
      detail: paragraphs.join("\n\n"),
      buttons: actions.map((action) => LABELS[action]),
      defaultId: actions.indexOf(defaultAction),
      // Esc and the close button reload rather than quit: a dismissal is not a
      // decision to stop the app, and a reload is bounded by the recovery budget.
      cancelId: actions.indexOf(ACTION_RETRY),
      noLink: true,
    },
    actions,
    blockedSandbox,
  };
}

/**
 * Create the notice. `show` is what `renderer-recovery.js`'s `onGiveUp` calls.
 *
 * @param {object} deps
 * @param {{showMessageBox: Function}} deps.dialog  Electron `dialog`.
 * @param {(url: string) => any} deps.openExternal  Opens the guide in the OS browser.
 * @param {() => void} deps.requestQuit             The app's own quit path.
 * @param {() => void} deps.retry                   Reset the reload budget and
 *   load the dashboard once more.
 * @param {() => boolean} [deps.dashboardLoaded]  Has a dashboard document
 *   finished loading in this run? Owned by gpu-crash-fallback.js (one latch,
 *   one listener for the whole app); the boot splash is a file URL and never
 *   counts there, which is what makes it the right fact for this signature.
 * @param {() => object|null} [deps.parentWindow]   The dashboard window, so the
 *   dialog attaches to it while it is visible.
 * @param {() => boolean} [deps.isQuitting]
 * @param {string} [deps.platform]
 * @param {() => string} [deps.logPath]
 * @param {(msg: string) => void} [deps.log]
 * @returns {{show: (info: object) => Promise<string>, showing: boolean}}
 *   `show` resolves to how the dialog ended: "retry" | "quit" |
 *   "skipped-quitting" | "skipped-already-showing" | "dialog-failed". Opening
 *   the guide does not end it: the window is still dead, so the dialog comes
 *   back until the user picks Try again or Quit.
 */
function createRendererExhaustedNotice({
  dialog,
  openExternal,
  requestQuit,
  retry,
  dashboardLoaded = () => false,
  parentWindow = () => null,
  isQuitting = () => false,
  platform = process.platform,
  logPath = () => "",
  log = () => {},
} = {}) {
  if (!dialog || typeof dialog.showMessageBox !== "function") {
    throw new Error("createRendererExhaustedNotice: dialog is required");
  }
  if (typeof openExternal !== "function") {
    throw new Error("createRendererExhaustedNotice: openExternal is required");
  }
  if (typeof requestQuit !== "function") {
    throw new Error("createRendererExhaustedNotice: requestQuit is required");
  }
  if (typeof retry !== "function") {
    throw new Error("createRendererExhaustedNotice: retry is required");
  }

  let showing = false;

  function dashboardHasLoaded() {
    try {
      return Boolean(dashboardLoaded());
    } catch {
      // An unreadable latch reads as "loaded": the generic text is always
      // true, while a wrong sandbox hint would send a user to IT for nothing.
      return true;
    }
  }

  function visibleParent() {
    let window = null;
    try {
      window = parentWindow();
    } catch {
      return undefined;
    }
    if (!window || typeof window.isDestroyed !== "function" || window.isDestroyed()) return undefined;
    if (typeof window.isVisible === "function" && !window.isVisible()) return undefined;
    return window;
  }

  function safeLogPath() {
    try {
      return String(logPath() || "");
    } catch {
      return "";
    }
  }

  async function askOnce(info, guideOpened) {
    const { options, actions, blockedSandbox } = buildRendererExhaustedNotice({
      ...info,
      platform,
      dashboardLoaded: dashboardHasLoaded(),
      logPath: safeLogPath(),
      guideOpened,
    });
    log(guideOpened
      ? "troubleshooting guide opened — showing the error dialog again"
      : `renderer recovery exhausted (reason=${info.reason}, exitCode=${info.exitCode}, ` +
        `blockedSandbox=${blockedSandbox}) — showing the error dialog`);
    const parent = visibleParent();
    const result = parent
      ? await dialog.showMessageBox(parent, options)
      : await dialog.showMessageBox(options);
    return actions[result && result.response] || ACTION_QUIT;
  }

  async function openGuide() {
    // openExternal may throw or reject; neither may escape into Electron.
    const failed = (e) => log(`could not open the troubleshooting guide: ${e && e.message}`);
    try {
      await Promise.resolve(openExternal(TROUBLESHOOTING_URL)).catch(failed);
    } catch (e) {
      failed(e);
    }
  }

  async function show(info = {}) {
    if (isQuitting()) return "skipped-quitting";
    // A renderer can keep dying while the dialog is up; one dialog is enough.
    if (showing) return "skipped-already-showing";
    showing = true;
    let action;
    try {
      action = await askOnce(info, false);
      while (action === ACTION_GUIDE) {
        // The guide is open, but the window behind the dialog is still dead.
        // Closing here would hand the user back the black window this exists
        // to replace, so ask again; each pass is one user click, never a loop
        // the code drives on its own.
        await openGuide();
        if (isQuitting()) return "skipped-quitting";
        action = await askOnce(info, true);
      }
    } catch (e) {
      // Even the native dialog failing must not take the main process down;
      // the log line above already names the failure.
      log(`renderer error dialog failed: ${e && e.message}`);
      return "dialog-failed";
    } finally {
      showing = false;
    }
    if (isQuitting()) return "skipped-quitting";

    if (action === ACTION_RETRY) {
      try {
        retry();
      } catch (e) {
        log(`renderer retry after the error dialog failed: ${e && e.message}`);
      }
      return ACTION_RETRY;
    }
    requestQuit();
    return ACTION_QUIT;
  }

  return {
    show,
    get showing() {
      return showing;
    },
  };
}

module.exports = {
  STATUS_BREAKPOINT_EXIT_CODE,
  BLOCKED_SANDBOX_PLATFORM,
  TROUBLESHOOTING_URL,
  ACTION_RETRY,
  ACTION_GUIDE,
  ACTION_QUIT,
  LABELS,
  PLAIN_REASONS,
  isBlockedSandboxSignature,
  buildRendererExhaustedNotice,
  createRendererExhaustedNotice,
};
