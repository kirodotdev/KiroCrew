"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const {
  ACTION_GUIDE,
  ACTION_QUIT,
  ACTION_RETRY,
  LABELS,
  PLAIN_REASONS,
  STATUS_BREAKPOINT_EXIT_CODE,
  TROUBLESHOOTING_URL,
  buildRendererExhaustedNotice,
  createRendererExhaustedNotice,
  isBlockedSandboxSignature,
} = require("../renderer-exhausted-notice");
const { createRendererRecovery } = require("../renderer-recovery");
const { initGpuCrashFallback } = require("../gpu-crash-fallback");

const BACKEND = "http://127.0.0.1:5476";

// The give-up `renderer-recovery.js` reports on an affected managed Windows
// 11 host: the renderer dies on the bundled boot splash with STATUS_BREAKPOINT
// before any dashboard document loads, and the reload budget runs out.
const BLOCKED_SANDBOX_GIVE_UP = {
  reason: "crashed",
  exitCode: STATUS_BREAKPOINT_EXIT_CODE,
  attempts: 3,
  snapshot: "",
};

// A notice wired entirely to fakes: Electron main is not available here, and
// the native dialog is the one thing this module must never need to be tested.
function harness(overrides = {}) {
  const shown = [];
  const opened = [];
  const logs = [];
  let quits = 0;
  let retries = 0;
  let quitting = false;
  let response = 0;
  let parent = null;
  let loaded = false;
  const notice = createRendererExhaustedNotice({
    dialog: {
      showMessageBox: async (...args) => {
        shown.push(args);
        return { response: typeof response === "function" ? response() : response };
      },
    },
    openExternal: (url) => {
      opened.push(url);
      return Promise.resolve();
    },
    requestQuit: () => { quits += 1; },
    retry: () => { retries += 1; },
    dashboardLoaded: () => loaded,
    parentWindow: () => parent,
    isQuitting: () => quitting,
    platform: "win32",
    logPath: () => "C:\\Users\\me\\AppData\\Roaming\\kirocrew-desktop\\logs\\gateway-launch.log",
    log: (m) => logs.push(m),
    ...overrides,
  });
  return {
    notice,
    shown,
    opened,
    logs,
    get quits() { return quits; },
    get retries() { return retries; },
    /** A button index, or a function returning one per dialog shown. */
    setResponse: (index) => { response = index; },
    setQuitting: (value) => { quitting = value; },
    setParent: (window) => { parent = window; },
    setLoaded: (value) => { loaded = value; },
    /** The options the dialog was last shown with. */
    lastOptions: () => shown[shown.length - 1][shown[shown.length - 1].length - 1],
  };
}

// --- the blocked-sandbox signature ------------------------------------------

test("the blocked-sandbox signature needs Windows, STATUS_BREAKPOINT and no dashboard yet", () => {
  const base = { platform: "win32", exitCode: STATUS_BREAKPOINT_EXIT_CODE, dashboardLoaded: false };
  assert.strictEqual(isBlockedSandboxSignature(base), true);
  // Only the signed value `render-process-gone` carries; nothing else produces one.
  assert.strictEqual(isBlockedSandboxSignature({ ...base, exitCode: 2147483651 }), false);
  // Each fact removed on its own turns it back into an ordinary crash.
  assert.strictEqual(isBlockedSandboxSignature({ ...base, platform: "darwin" }), false);
  assert.strictEqual(isBlockedSandboxSignature({ ...base, platform: "linux" }), false);
  assert.strictEqual(isBlockedSandboxSignature({ ...base, exitCode: 5 }), false);
  assert.strictEqual(isBlockedSandboxSignature({ ...base, exitCode: undefined }), false);
  // The V8-abort false positive: same code, but the dashboard had rendered.
  assert.strictEqual(isBlockedSandboxSignature({ ...base, dashboardLoaded: true }), false);
  assert.strictEqual(isBlockedSandboxSignature(), false);
});

// --- the dialog contents -----------------------------------------------------

test("a generic give-up names the cause, the count and the log, and offers retry or quit", () => {
  const { options, actions, blockedSandbox } = buildRendererExhaustedNotice({
    reason: "oom",
    exitCode: 137,
    attempts: 3,
    platform: "darwin",
    dashboardLoaded: true,
    logPath: "/Users/me/Library/Logs/gateway-launch.log",
  });
  assert.strictEqual(blockedSandbox, false);
  assert.strictEqual(options.type, "error");
  assert.strictEqual(options.message, "Kiro Crew's window has stopped working.");
  // Three reloads spent means the renderer died four times; the reason is
  // said in plain words before the raw codes.
  assert.match(options.detail, /stopped working 4 times in a row \(it ran out of memory\), so Kiro Crew stopped retrying/);
  // The details line says who it is for and carries raw codes only: the plain
  // words were said once, in the first sentence.
  assert.match(options.detail, /Details for IT or a bug report: reason: oom, exit code: 137\./);
  assert.strictEqual(options.detail.match(/ran out of memory/g).length, 1);
  assert.match(options.detail, /Log file to attach: .*gateway-launch\.log/);
  // The user's next step comes before the codes a report needs.
  assert.ok(options.detail.indexOf("Try again reloads") < options.detail.indexOf("Details for IT"));
  // The close button is explained, since it is a way out too.
  assert.match(options.detail, /closing this box does the same/);
  // Nothing in the generic text claims a sandbox problem.
  assert.doesNotMatch(options.detail, /sandbox/i);
  assert.deepStrictEqual(actions, [ACTION_RETRY, ACTION_QUIT]);
  assert.deepStrictEqual(options.buttons, [LABELS[ACTION_RETRY], LABELS[ACTION_QUIT]]);
  assert.strictEqual(options.defaultId, 0);
  // Esc and the close button reload; dismissing an error never quits the app.
  assert.strictEqual(options.cancelId, actions.indexOf(ACTION_RETRY));
});

test("every recoverable reason has plain words, and an unknown one shows no parenthesis", () => {
  for (const reason of ["crashed", "oom", "abnormal-exit", "launch-failed", "integrity-failure", "killed"]) {
    assert.ok(PLAIN_REASONS[reason], `${reason} has plain words`);
    const { options } = buildRendererExhaustedNotice({ reason, attempts: 3, platform: "linux" });
    assert.match(options.detail, new RegExp(`\\(${PLAIN_REASONS[reason]}\\)`));
  }
  const { options } = buildRendererExhaustedNotice({ reason: "something-new", attempts: 3, platform: "linux" });
  assert.match(options.detail, /stopped working 4 times in a row, so Kiro Crew stopped retrying/);
  assert.match(options.detail, /reason: something-new/);
});

test("a blocked-sandbox give-up adds the cause, the supported fix, and leads with the guide", () => {
  const { options, actions, blockedSandbox } = buildRendererExhaustedNotice({
    ...BLOCKED_SANDBOX_GIVE_UP,
    platform: "win32",
    dashboardLoaded: false,
    logPath: "C:\\logs\\gateway-launch.log",
  });
  assert.strictEqual(blockedSandbox, true);
  // The heading names the cause, since it is the most prominent line.
  assert.strictEqual(options.message, "Security software is blocking Kiro Crew's window.");
  // Try again is explained as useful only once IT has acted.
  assert.match(options.detail, /Try again reloads the window once more; it helps only after IT has added the app to the allow list/);
  assert.match(options.detail, /Kiro Crew's display process exited 4 times/);
  // Cause first, then who to ask and for what, then the codes a report needs.
  assert.match(options.detail, /^Most likely, security software on this Windows device is blocking/);
  assert.match(options.detail, /ask your IT team to add the Kiro Crew app to that software's allow list \(a "process exclusion"\)/);
  assert.match(options.detail, /reason: crashed, exit code: -2147483645 \/ 0x80000003/);
  assert.ok(options.detail.indexOf("ask your IT team") < options.detail.indexOf("-2147483645"));
  assert.ok(options.detail.indexOf("Try again reloads") < options.detail.indexOf("Details for IT"));
  // The posture is stated to the user, not just kept in code, and "security"
  // names only the software, never Kiro Crew's own side.
  assert.match(options.detail, /will not weaken its own safeguards/);
  assert.strictEqual(options.detail.match(/security/g).length, 1);
  // The guide button says a browser will open.
  assert.match(options.buttons[0], /in browser$/);
  assert.deepStrictEqual(actions, [ACTION_GUIDE, ACTION_RETRY, ACTION_QUIT]);
  assert.strictEqual(options.buttons[0], LABELS[ACTION_GUIDE]);
  assert.strictEqual(options.defaultId, 0, "a blocked sandbox never recovers by reloading");
  assert.strictEqual(options.cancelId, actions.indexOf(ACTION_RETRY));
});

test("after the guide was opened, the buttons keep their places and Try again becomes the default", () => {
  const { options, actions, blockedSandbox } = buildRendererExhaustedNotice({
    ...BLOCKED_SANDBOX_GIVE_UP,
    platform: "win32",
    dashboardLoaded: false,
    guideOpened: true,
  });
  assert.strictEqual(blockedSandbox, true);
  assert.match(options.detail, /security software on this Windows device/);
  assert.match(options.detail, /guide is open in your browser/);
  assert.deepStrictEqual(actions, [ACTION_GUIDE, ACTION_RETRY, ACTION_QUIT], "same order as before the guide");
  assert.strictEqual(options.defaultId, 1, "Try again is now the default");
  assert.strictEqual(options.cancelId, 1);
  // Off the blocked-sandbox path the flag changes nothing.
  const generic = buildRendererExhaustedNotice({ reason: "oom", platform: "darwin", guideOpened: true });
  assert.doesNotMatch(generic.options.detail, /guide/);
  assert.deepStrictEqual(generic.actions, [ACTION_RETRY, ACTION_QUIT]);
});

test("the dialog never carries a sandbox switch, on either path", () => {
  for (const info of [
    { reason: "crashed", exitCode: 1, platform: "linux", dashboardLoaded: true },
    { ...BLOCKED_SANDBOX_GIVE_UP, platform: "win32", dashboardLoaded: false },
  ]) {
    const { options } = buildRendererExhaustedNotice(info);
    const text = `${options.message}\n${options.detail}\n${options.buttons.join("\n")}`;
    assert.doesNotMatch(text, /--no-sandbox|--disable-gpu-sandbox|RendererCodeIntegrity/);
  }
});

test("a missing log path and missing details still produce a complete dialog", () => {
  const { options } = buildRendererExhaustedNotice({});
  assert.match(options.detail, /stopped working 2 times in a row/);
  assert.match(options.detail, /reason: unknown, exit code: unknown/);
  assert.doesNotMatch(options.detail, /Log file/);
  assert.ok(options.buttons.length >= 2);
});

// --- showing it ------------------------------------------------------------

test("a give-up on an affected Windows host shows the sandbox dialog; the guide button opens the guide and asks again", async () => {
  const h = harness();
  const responses = [0, 2]; // the guide first, then Quit on the dialog that comes back (same button places)
  h.setResponse(() => responses.shift());
  const outcome = await h.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  assert.strictEqual(h.shown.length, 2, "the dialog comes back after the guide opens");
  const first = h.shown[0][h.shown[0].length - 1];
  assert.match(first.detail, /security software on this Windows device/);
  assert.match(first.detail, /gateway-launch\.log/, "the real log path is named");
  assert.strictEqual(first.buttons[0], LABELS[ACTION_GUIDE]);
  assert.deepStrictEqual(h.opened, [TROUBLESHOOTING_URL]);
  // The second dialog keeps the cause, says the guide is open, keeps every
  // button in its place and moves the default to Try again.
  const second = h.lastOptions();
  assert.match(second.detail, /security software on this Windows device/);
  assert.match(second.detail, /guide is open in your browser/);
  assert.deepStrictEqual(second.buttons, first.buttons, "button places do not move");
  assert.strictEqual(second.defaultId, 1);
  assert.strictEqual(outcome, ACTION_QUIT);
  assert.strictEqual(h.quits, 1);
  assert.strictEqual(h.retries, 0);
  assert.match(h.logs.join("\n"), /renderer recovery exhausted .*blockedSandbox=true/);
});

test("the guide never leaves the user with the bare black window: it is followed by Try again or Quit", async () => {
  const h = harness();
  const responses = [0, 1]; // the guide, then Try again
  h.setResponse(() => responses.shift());
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  assert.deepStrictEqual(h.opened, [TROUBLESHOOTING_URL]);
  assert.strictEqual(h.retries, 1);
  assert.strictEqual(h.quits, 0);
  // The guide can be reopened from the returning dialog, each time on a click.
  const again = harness();
  const clicks = [0, 0, 0, 2]; // guide, guide again, guide again, Quit
  again.setResponse(() => clicks.shift());
  assert.strictEqual(await again.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_QUIT);
  assert.deepStrictEqual(again.opened, [TROUBLESHOOTING_URL, TROUBLESHOOTING_URL, TROUBLESHOOTING_URL]);
  assert.strictEqual(again.shown.length, 4);
  // Off the blocked-sandbox path there is no guide button, so a single answer ends it.
  const generic = harness({ platform: "linux" });
  generic.setResponse(0);
  assert.strictEqual(await generic.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  assert.strictEqual(generic.shown.length, 1);
  assert.deepStrictEqual(generic.opened, []);
});

test("Quit calls the app's own quit path and nothing else", async () => {
  const h = harness();
  h.setResponse(2);
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_QUIT);
  assert.strictEqual(h.quits, 1);
  assert.strictEqual(h.retries, 0);
  assert.deepStrictEqual(h.opened, []);
});

test("Try again resets the budget through the injected retry", async () => {
  const h = harness();
  h.setResponse(1);
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  assert.strictEqual(h.retries, 1);
  assert.strictEqual(h.quits, 0);
});

test("once the dashboard has loaded, the same exit code is an ordinary crash", async () => {
  // The latch is the gpu-crash-fallback one, read at show time, not at
  // construction: a dashboard that loads after the notice exists still counts.
  const h = harness();
  h.setLoaded(true);

  h.setResponse(0); // leading button is now "Try again"
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  const options = h.lastOptions();
  assert.doesNotMatch(options.detail, /security software|IT team/);
  assert.deepStrictEqual(options.buttons, [LABELS[ACTION_RETRY], LABELS[ACTION_QUIT]]);
  assert.match(h.logs.join("\n"), /blockedSandbox=false/);

  // A latch that cannot be read counts as loaded: the generic text is always
  // true, a wrong sandbox hint is not.
  const unreadable = harness({ dashboardLoaded: () => { throw new Error("gone"); } });
  unreadable.setResponse(1);
  assert.strictEqual(await unreadable.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_QUIT);
  assert.doesNotMatch(unreadable.lastOptions().detail, /security software|IT team/);
});

test("the latch is read from gpu-crash-fallback's init, so there is one listener and one fact", () => {
  // The notice has no latch of its own: nothing to feed, nothing to drift.
  assert.strictEqual(typeof createRendererExhaustedNotice({
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    openExternal: () => {}, requestQuit: () => {}, retry: () => {},
  }).noteDocumentLoaded, "undefined");
  const listeners = new Map();
  const app = {
    getVersion: () => "0.9.0",
    commandLine: { appendSwitch() {}, removeSwitch() {} },
    on: (event, fn) => listeners.set(event, fn),
    relaunch() {},
  };
  const { dashboardLoaded } = initGpuCrashFallback({
    app, store: { get: () => null, set() {}, path: "/virtual/config.json" },
    backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => {},
  });
  assert.strictEqual(dashboardLoaded(), false);
  const contents = (url) => {
    let onLoad = null;
    return { getURL: () => url, on: (event, fn) => { if (event === "did-finish-load") onLoad = fn; }, finishLoad: () => onLoad() };
  };
  const splash = contents("file:///C:/app/resources/loading.html");
  listeners.get("web-contents-created")(null, splash);
  splash.finishLoad();
  assert.strictEqual(dashboardLoaded(), false, "the boot splash never counts");
  const dashboard = contents(`${BACKEND}/?token=abc`);
  listeners.get("web-contents-created")(null, dashboard);
  dashboard.finishLoad();
  assert.strictEqual(dashboardLoaded(), true);
});

test("off Windows the dialog is the generic one even with STATUS_BREAKPOINT", async () => {
  const h = harness({ platform: "darwin" });
  h.setResponse(1);
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_QUIT);
  assert.doesNotMatch(h.lastOptions().detail, /security software|IT team/);
});

test("the dialog attaches to the window only while it is alive and visible", async () => {
  const h = harness();
  h.setResponse(2); // Quit: one dialog per show
  const visible = { isDestroyed: () => false, isVisible: () => true };
  h.setParent(visible);
  await h.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  assert.strictEqual(h.shown[0].length, 2);
  assert.strictEqual(h.shown[0][0], visible);

  // Hidden to tray: a sheet on a hidden window would be invisible, so no parent.
  h.setParent({ isDestroyed: () => false, isVisible: () => false });
  await h.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  assert.strictEqual(h.shown[1].length, 1);

  h.setParent({ isDestroyed: () => true, isVisible: () => true });
  await h.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  assert.strictEqual(h.shown[2].length, 1);

  // A parent lookup that throws is treated as "no parent", not as a failure.
  const throwing = harness({ parentWindow: () => { throw new Error("gone"); } });
  throwing.setResponse(2);
  assert.strictEqual(await throwing.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_QUIT);
  assert.strictEqual(throwing.shown[0].length, 1);
});

test("one dialog at a time: deaths while it is up are logged, not stacked", async () => {
  let release;
  const h = harness({
    dialog: {
      showMessageBox: () => new Promise((resolve) => { release = resolve; }),
    },
  });
  const first = h.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  assert.strictEqual(h.notice.showing, true);
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), "skipped-already-showing");
  release({ response: 2 });
  assert.strictEqual(await first, ACTION_QUIT);
  assert.strictEqual(h.notice.showing, false);
  // After it closes, a later give-up may show again.
  const second = h.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  assert.strictEqual(h.notice.showing, true);
  release({ response: 2 });
  assert.strictEqual(await second, ACTION_QUIT);
});

test("nothing is shown during quit, and a quit that began under the dialog wins", async () => {
  const h = harness();
  h.setQuitting(true);
  assert.strictEqual(await h.notice.show(BLOCKED_SANDBOX_GIVE_UP), "skipped-quitting");
  assert.strictEqual(h.shown.length, 0);

  let release;
  const h2 = harness({
    dialog: { showMessageBox: () => new Promise((resolve) => { release = resolve; }) },
  });
  const pending = h2.notice.show(BLOCKED_SANDBOX_GIVE_UP);
  h2.setQuitting(true);
  release({ response: 1 }); // "Try again" — must not reload a quitting app
  assert.strictEqual(await pending, "skipped-quitting");
  assert.strictEqual(h2.retries, 0);
  assert.strictEqual(h2.quits, 0, "the quit already under way is not requested twice");

  // A quit that begins while the guide is opening stops the dialog coming back.
  const h3 = harness({
    openExternal: () => { h3.setQuitting(true); return Promise.resolve(); },
  });
  const h3Clicks = [0, 1];
  h3.setResponse(() => h3Clicks.shift());
  assert.strictEqual(await h3.notice.show(BLOCKED_SANDBOX_GIVE_UP), "skipped-quitting");
  assert.strictEqual(h3.shown.length, 1);
  assert.strictEqual(h3.notice.showing, false);
});

test("a failing dialog, retry, guide or log path never escapes into Electron", async () => {
  const failing = harness({
    dialog: { showMessageBox: async () => { throw new Error("no display"); } },
  });
  assert.strictEqual(await failing.notice.show(BLOCKED_SANDBOX_GIVE_UP), "dialog-failed");
  assert.strictEqual(failing.notice.showing, false);
  assert.match(failing.logs.join("\n"), /error dialog failed: no display/);

  const badRetry = harness({ retry: () => { throw new Error("reload broke"); } });
  badRetry.setResponse(1);
  assert.strictEqual(await badRetry.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  assert.match(badRetry.logs.join("\n"), /retry after the error dialog failed: reload broke/);

  // A guide that fails to open still brings the dialog back (Try again / Quit).
  const badGuide = harness({ openExternal: () => Promise.reject(new Error("no browser")) });
  const badGuideClicks = [0, 1]; // the guide (which fails), then Try again
  badGuide.setResponse(() => badGuideClicks.shift());
  assert.strictEqual(await badGuide.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  assert.strictEqual(badGuide.shown.length, 2);
  assert.match(badGuide.logs.join("\n"), /could not open the troubleshooting guide: no browser/);

  const throwingGuide = harness({ openExternal: () => { throw new Error("sync"); } });
  const throwingClicks = [0, 1];
  throwingGuide.setResponse(() => throwingClicks.shift());
  assert.strictEqual(await throwingGuide.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_RETRY);
  assert.match(throwingGuide.logs.join("\n"), /could not open the troubleshooting guide: sync/);

  const badLog = harness({ logPath: () => { throw new Error("no home"); } });
  badLog.setResponse(2);
  assert.strictEqual(await badLog.notice.show(BLOCKED_SANDBOX_GIVE_UP), ACTION_QUIT);
  assert.doesNotMatch(badLog.lastOptions().detail, /Log file/);
});

test("every collaborator the dialog needs is required up front", () => {
  const deps = {
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    openExternal: () => {},
    requestQuit: () => {},
    retry: () => {},
  };
  for (const missing of Object.keys(deps)) {
    const partial = { ...deps };
    delete partial[missing];
    assert.throws(() => createRendererExhaustedNotice(partial), new RegExp(`${missing} is required`));
  }
  assert.throws(() => createRendererExhaustedNotice({ ...deps, dialog: {} }), /dialog is required/);
});

// --- end to end with the recovery coordinator -------------------------------

test("the recovery coordinator's give-up reaches the dialog with the renderer's own payload", async () => {
  const h = harness();
  h.setResponse(2);
  let pending = null;
  let t = 0;
  const rec = createRendererRecovery({
    reload: () => {},
    now: () => t,
    onGiveUp: (info) => { pending = h.notice.show(info); },
  });
  // Four deaths inside the window: three reloads, then the give-up.
  const death = { reason: "crashed", exitCode: STATUS_BREAKPOINT_EXIT_CODE };
  assert.strictEqual(rec.handleGone(death), "reloaded");
  assert.strictEqual(rec.handleGone(death), "reloaded");
  assert.strictEqual(rec.handleGone(death), "reloaded");
  assert.strictEqual(rec.handleGone(death), "gave-up");
  assert.ok(pending, "the give-up showed the dialog");
  assert.strictEqual(await pending, ACTION_QUIT);
  const options = h.lastOptions();
  assert.match(options.detail, /display process exited 4 times before anything was drawn/);
  assert.match(options.detail, /reason: crashed, exit code: -2147483645/);
  assert.match(options.detail, /security software on this Windows device/);
  assert.strictEqual(h.quits, 1);
});
