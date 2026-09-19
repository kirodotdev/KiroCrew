"use strict";
//
// Turn an unrecoverable Windows renderer LAUNCH failure into a named cause and
// a remedy.
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
// behalf, and it deliberately offers no switch that would: silently dropping a
// security boundary because a process crashed would turn any renderer-crash bug
// into an isolation downgrade, and on a managed device that decision belongs to
// whoever owns the device's security posture. Same division of labour as
// sandbox-profile.js, which prints the AppArmor command rather than running it.
//
// It also offers no Chromium hardening switches as a remedy, which is a
// correction rather than an omission. Feature-flag names are version-specific
// and silently ignored when wrong — `--disable-features=RendererCodeIntegrity`
// has been a no-op since Chromium 118, though the Chrome POLICY of the same
// name still works — so a documented flag ladder cannot be verified from here
// and would send a reader down a dead end. The remedy below is the one that
// addresses the actual cause and can be checked by the person applying it.
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
 *
 * This value NEVER classifies a death on its own — see the note on
 * `looksLikeSandboxStartupFailure`. It only corroborates one already
 * classified by its reason.
 */
const STATUS_BREAKPOINT_SIGNED = -2147483645;
const STATUS_BREAKPOINT_UNSIGNED = 2147483651;

/**
 * `render-process-gone` reasons that PROVE a launch failure on their own.
 *
 * Electron documents `launch-failed` as the process launch itself failing, and
 * `integrity-failure` as Windows code-integrity rejecting the child. Either is
 * conclusive. Neither is REQUIRED, though — see the note on
 * `looksLikeSandboxStartupFailure` — because a sandbox `CHECK` inside a child
 * that did get created is reported as an ordinary `crashed`.
 */
const STARTUP_FAILURE_REASONS = new Set(["launch-failed", "integrity-failure"]);

/**
 * Whether this death looks like the Windows sandbox refusing to start a child.
 *
 * The load-bearing input is `sandboxedRendererRan`, NOT the reason and not the
 * exit code, because neither of those can separate the two failures that share
 * 0x80000003:
 *
 *   - The sandbox refuses a child. Chromium raises a CHECK during sandbox
 *     setup, so the process exits STATUS_BREAKPOINT. It may be reported as
 *     `launch-failed`, but a CHECK inside a child that WAS created reads as an
 *     ordinary `crashed` — so requiring a launch reason would miss the very
 *     failure this module exists for.
 *   - A V8 fatal abort. Same exit code, and the long-session crash
 *     renderer-recovery.js was built for (see its header) — so accepting the
 *     exit code alone would tell that user to relax a sandbox that is working.
 *
 * What separates them is whether a sandboxed renderer has EVER run on this
 * host. The proof chain the caller supplies is: a document finished loading =>
 * a renderer process hosted it => the sandbox admitted that process.
 *
 * That evidence gates only the AMBIGUOUS signal. The order below matters:
 *
 *   1. `launch-failed` / `integrity-failure` are conclusive on their own and
 *      override any earlier success, because they describe THIS child failing
 *      to launch. Endpoint software can start injecting mid-run — a definition
 *      update, a driver DLL loaded later, a policy push — and then a renderer
 *      that ran an hour ago says nothing about the one being replaced now.
 *   2. Only for the ambiguous `crashed` + STATUS_BREAKPOINT pair does an
 *      earlier success win, because that pair is exactly where a blocked
 *      sandbox and a V8 abort are indistinguishable, and a renderer having run
 *      makes the V8 reading the likelier one.
 *
 * Note what this deliberately does NOT require: that the document was the
 * DASHBOARD. The app's own boot splash, an HTTP error body, `about:blank` and a
 * `data:` document all count, because each of them is hosted by a renderer and
 * that is the whole question. Electron sandboxes renderers by default (the
 * `sandbox` webPreference defaults to true since Electron 20, and is only
 * disabled by `nodeIntegration: true`, which the dashboard window does not set),
 * so a loaded splash is a sandboxed renderer that ran. Narrowing this to a
 * dashboard-origin load would REINTRODUCE the V8-abort false positive above: an
 * SPA that aborts after the splash loaded would be reported as a sandbox
 * failure, sending that user to their endpoint-security team over a bug in our
 * own JavaScript.
 *
 * Two residuals, both stated rather than hidden:
 *
 *   - FALSE POSITIVE. A V8 abort during the first document, before anything has
 *     loaded, is indistinguishable from a blocked sandbox. The remedy absorbs
 *     it: step one is a `--no-sandbox` launch that still fails for such a user,
 *     and says so.
 *   - FALSE NEGATIVE. Injection that begins mid-run AND is reported only as a
 *     bare `crashed` + STATUS_BREAKPOINT, after something had already loaded,
 *     is read as a V8 abort and gets no advice. Rule 1 covers this whenever
 *     Electron reports it conclusively; the remaining sliver fails toward
 *     SILENCE rather than toward wrong advice, which is the safe direction.
 *     Closing it properly needs per-navigation renderer-start evidence (a
 *     preload handshake) rather than a lifetime flag — deliberately not built
 *     here, because new IPC surface to diagnose a crash is out of proportion to
 *     one sliver of one failure mode.
 *
 * @param {object} o
 * @param {string} [o.reason] `render-process-gone` reason.
 * @param {number|null} [o.exitCode]
 * @param {boolean} [o.sandboxedRendererRan] Whether any document has finished
 *   loading since this app started. Defaults FALSE: a caller that cannot answer
 *   gets the diagnosis rather than silence, and the remedy's first step is the
 *   one that corrects a wrong guess.
 * @returns {boolean}
 */
function looksLikeSandboxStartupFailure({
  reason,
  exitCode,
  sandboxedRendererRan = false,
} = {}) {
  // Conclusive, and about THIS child — so it outranks any earlier success.
  if (STARTUP_FAILURE_REASONS.has(String(reason || ""))) return true;
  // Ambiguous from here down: a renderer having run makes a V8 abort likelier
  // than a sandbox that cannot start a child.
  if (sandboxedRendererRan) return false;
  const code = Number(exitCode);
  return code === STATUS_BREAKPOINT_SIGNED || code === STATUS_BREAKPOINT_UNSIGNED;
}

/**
 * The Chromium switch name the caller must ask Electron about.
 *
 * There is deliberately no argv parser here. An earlier revision scanned
 * `process.argv` for the exact string `--no-sandbox`, which does not agree with
 * how Chromium's own `base::CommandLine` reads a command line: it also accepts
 * `--no-sandbox=<value>`, a single-dash `-no-sandbox` and Windows-style
 * `/no-sandbox`, and it stops honouring switches after a bare `--`. Every one
 * of those is a way to get the answer wrong. The caller passes
 * `app.commandLine.hasSwitch(SANDBOX_OFF_SWITCH)` instead, so the question is
 * answered by the same parser that decided the behaviour.
 */
const SANDBOX_OFF_SWITCH = "no-sandbox";

/**
 * Describe a Windows sandbox startup failure, or return null.
 *
 * @param {object} o
 * @param {string} o.platform `process.platform`.
 * @param {string} [o.reason]
 * @param {number|null} [o.exitCode] Corroborating detail only.
 * @param {number} [o.attempts] How many deaths recovery counted before giving
 *   up. Reported as a count only: recovery keeps timestamps, not per-death
 *   reason/exit-code signatures, so this cannot claim the earlier deaths
 *   matched this one.
 * @param {string} [o.execPath] `process.execPath` — the running binary, used to
 *   build an exact launch command instead of guessing an install layout.
 * @param {boolean} [o.sandboxedRendererRan] See `looksLikeSandboxStartupFailure`.
 * @param {boolean} [o.sandboxAlreadyOff] True when this launch already ran with
 *   the sandbox disabled, from `app.commandLine.hasSwitch("no-sandbox")`.
 * @returns {{cause: string, remedy: string[]}|null}
 */
function describeWindowsSandboxFailure({
  platform,
  reason,
  exitCode,
  attempts,
  execPath,
  sandboxedRendererRan = false,
  sandboxAlreadyOff = false,
} = {}) {
  if (platform !== "win32") return null;
  if (sandboxAlreadyOff) return null;
  if (!looksLikeSandboxStartupFailure({ reason, exitCode, sandboxedRendererRan })) {
    return null;
  }

  const code = Number(exitCode);
  const breakpoint =
    code === STATUS_BREAKPOINT_SIGNED || code === STATUS_BREAKPOINT_UNSIGNED;
  const count = Number(attempts);
  // Read verbatim by a stressed user, so no "attempt(s)" hack: either a real
  // count with agreeing grammar, or a phrase that needs no number at all.
  const tally =
    Number.isFinite(count) && count > 0
      ? `After ${count} reload attempt${count === 1 ? "" : "s"}`
      : "After the reload budget was spent";
  const breakpointNote = breakpoint
    ? " — 0x80000003 STATUS_BREAKPOINT, which is how Chromium reports a failed " +
      "CHECK during sandbox setup"
    : "";

  // The launch instruction must name THIS build's executable, not a guessed
  // install layout. An earlier revision hardcoded the stable per-user NSIS path,
  // which is wrong for a nightly (different product name), for an all-users
  // install, and for a portable copy — it would either fail or launch a
  // DIFFERENT app than the one that just died, making the disconfirmation step
  // answer the wrong question. `process.execPath` is the running binary by
  // definition, so the caller passes it and nothing here has to guess. Quoted
  // because Program Files and user names contain spaces.
  const launchLine = execPath
    ? `"${execPath}" --no-sandbox`
    : "the same Kiro Crew executable your shortcut points at (right-click the " +
      "Start-menu entry > Open file location) with --no-sandbox appended";

  // Two different stories reach this point, and saying the wrong one would send
  // the reader looking for the wrong thing. If a document had already loaded,
  // the evidence is a child being REFUSED after the sandbox had been working —
  // which is what endpoint software that starts injecting mid-session looks
  // like, and is NOT "nothing ever started".
  const cause = sandboxedRendererRan
    ? `${tally}, the last child could not be launched at all ` +
      `(reason=${reason}, exitCode=${exitCode})` +
      breakpointNote +
      ". An earlier renderer on this device did run, so the sandbox was " +
      "working and something changed since: an endpoint-security definition " +
      "update, a newly loaded driver DLL, or a policy push that now injects " +
      "into every process. The sandbox refuses to admit a child once that " +
      "happens."
    : `${tally} without any document ever finishing a load, the last one ` +
      `ended with reason=${reason}, exitCode=${exitCode}` +
      breakpointNote +
      ". No renderer having loaded anything at all points at Chromium being " +
      "unable to start a child process on this host. One common cause on a " +
      "managed device is software that injects a DLL into every process " +
      "(endpoint security, or a display driver such as DisplayLink), which the " +
      "sandbox then refuses to admit. It can also mean a damaged install or a " +
      "missing bundled library, so the steps below start by telling the two " +
      "apart rather than assuming.";

  return {
    cause,
    // The cause, not a workaround: this keeps the sandbox intact and is
    // checkable by the person who applies it. No Chromium switch is offered,
    // because none can be verified from here (see the module header).
    //
    // The disconfirmation step comes FIRST for the reason named in the cause
    // string: `launch-failed` is not sandbox-specific, and an exclusion request
    // raised on a damaged install wastes the reader's time and their endpoint
    // team's. One launch settles it before anyone is asked for anything.
    remedy: [
      "First, confirm the sandbox is what is failing: launch once with " +
        "--no-sandbox. If the dashboard then loads, continue below. If it " +
        "still fails, the sandbox is NOT the cause — stop here and try " +
        "KIROCREW_DISABLE_GPU=1 (which covers a failing GPU/rendering path), " +
        "then a reinstall if that does not help either. Do not raise an " +
        "endpoint-security exclusion for this: it cannot fix it.",
      "How to pass that flag: fully exit the app FIRST — it is single-instance, " +
        "so a second launch hands its arguments to the running instance and " +
        "exits, silently ignoring the flag — then from a Command Prompt run " +
        launchLine +
        ". For the variable instead, run `set KIROCREW_DISABLE_GPU=1` in that " +
        "same window before launching it. Fuller guide: " +
        "https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/windows-install.md",
      "Do not keep --no-sandbox: it removes renderer, GPU and utility " +
        "isolation for every child process. It is a diagnostic, not a setting.",
      "Fix: add a process exclusion for this application in the endpoint " +
        "security product managing this device. That addresses the cause and " +
        "leaves the sandbox intact.",
      "To identify what to exclude: open Event Viewer > Windows Logs > " +
        "Application and read the faulting module name in the AppCrash entry " +
        "for the child process that died.",
      "Cross-check: Microsoft Edge uses the same Chromium sandbox. If Edge " +
        "renders normally on this device, the sandbox itself works and the " +
        "difference is that Edge is already excluded and this app is not.",
    ],
  };
}

/**
 * Render an advice object as the text of a native error box.
 *
 * Why a dialog at all, when the diagnosis is already logged: for this specific
 * failure the log is the ONLY channel the app has, and it is a channel the
 * affected user cannot reach from where they are standing. No renderer can
 * start, so there is no in-app surface — what they see is a window that goes
 * grey and vanishes. Reaching the remedy then requires already knowing that a
 * launcher log exists and where. A diagnosis nobody reads is the same defect
 * this module was written to fix, one step further out.
 *
 * Nothing here is re-authored. The strings are the SAME cause and remedy the log
 * receives, so the two surfaces cannot drift into telling different stories, and
 * a test pins that. Only the numbering, the headings and the log pointer are
 * added.
 *
 * Every remedy step is included rather than the first one or two. Truncating
 * would leave the actual cure — the process exclusion — reachable only through
 * the log, which is the gap this exists to close; the disconfirmation step alone
 * tells the reader what is wrong but not what fixes it. The result is long for a
 * message box, which is the right trade on a terminal failure where the user has
 * no other text on screen: `early-boot-guard.js` puts a whole stack trace here
 * for the same reason.
 *
 * @param {{cause: string, remedy: string[]}} advice From
 *   `describeWindowsSandboxFailure`.
 * @param {object} [o]
 * @param {string} [o.logPath] Absolute path of the launcher log, named so the
 *   reader can quote it to whoever manages the device. Omitted cleanly when the
 *   caller cannot resolve it.
 * @returns {{title: string, content: string}}
 */
function buildSandboxFailureDialog(advice, { logPath } = {}) {
  const { cause, remedy } = advice || {};
  const steps = (Array.isArray(remedy) ? remedy : []).map(
    (line, index) => `${index + 1}. ${line}`,
  );
  const where = String(logPath || "");
  const sections = [String(cause || "")];
  if (steps.length) sections.push(`What to do:\n\n${steps.join("\n\n")}`);
  if (where) sections.push(`This was also written to:\n${where}`);
  return {
    title: "Kiro Crew cannot open its window on this device",
    content: sections.join("\n\n"),
  };
}

module.exports = {
  buildSandboxFailureDialog,
  describeWindowsSandboxFailure,
  looksLikeSandboxStartupFailure,
  SANDBOX_OFF_SWITCH,
  STATUS_BREAKPOINT_SIGNED,
  STATUS_BREAKPOINT_UNSIGNED,
};
