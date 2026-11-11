// A build from a channel the install no longer follows is never installed or
// downloaded on fail-open: the staged bytes and the offer each carry the channel
// whose feed served them, so a switch made BEFORE the click is caught even when
// the selected channel's feed fails or hangs. Also: a Retry after a recovered
// install failure holds a quit pressed while it runs, and the freshness bound is
// the documented one. The initAutoUpdate fixture is auto-update-test-helpers.js;
// the before-quit emitter and the feed seams these cases need sit on top of it.

const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");
const { FRESHNESS_CHECK_TIMEOUT_MS } = require("../runtime/update/freshness-gate");
const { makeDeps } = require("./auto-update-test-helpers");

/** Let queued microtasks and the async install and quit bodies run out. */
async function flush(times = 8) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function laneHarness({ channel = "insider" } = {}) {
  const { deps, calls, handlers, states } = makeDeps({ osPlatform: "linux" });
  const lane = { channel };
  const notices = [];
  const quitHandlers = [];
  let onCheck = null;
  let holdStop = null;
  calls.quits = 0;
  deps.getChannelPreference = () => lane.channel;
  deps.getAutoDownloadPreference = () => false;
  deps.nativeAutoUpdater = { once: () => {} };
  deps.Notification = function (options) {
    notices.push(options);
    return { show: () => {} };
  };
  deps.stopGateway = async () => { if (holdStop) await holdStop; };
  deps.autoUpdater.checkForUpdates = async () => {
    calls.checkForUpdates += 1;
    if (onCheck) await onCheck();
  };
  // `app.once` as Electron runs it: the listener is dropped before it is called,
  // and a listener that prevents the quit cancels it.
  deps.app.once = (ev, fn) => { if (ev === "before-quit") quitHandlers.push(fn); };
  deps.app.removeListener = (ev, fn) => {
    const i = quitHandlers.indexOf(fn);
    if (i >= 0) quitHandlers.splice(i, 1);
  };
  const fireQuit = () => {
    let prevented = false;
    const handler = quitHandlers.pop();
    if (handler) handler({ preventDefault: () => { prevented = true; } });
    return prevented;
  };
  deps.app.quit = () => { if (!fireQuit()) calls.quits += 1; };
  const emit = (ev, payload) => handlers[ev] && handlers[ev](payload);
  return {
    deps,
    calls,
    states,
    notices,
    lane,
    emit,
    fireQuit,
    feedServes: (version) => { onCheck = () => emit("update-available", { version }); },
    feedFails: () => { onCheck = () => { throw new Error("net::ERR_INTERNET_DISCONNECTED"); }; },
    /** The next check hangs until `upToDate()` lands it; later checks answer at once. */
    feedHangs: () => {
      let settle = null;
      onCheck = () => new Promise((resolve) => { settle = resolve; });
      return { upToDate: () => { onCheck = null; emit("update-not-available", {}); settle(); } };
    },
    holdGatewayStop: () => {
      let release = null;
      holdStop = new Promise((resolve) => { release = resolve; });
      return () => { holdStop = null; release(); };
    },
  };
}

test("the freshness bound is the documented 8s", () => {
  // docs/build/release.md promises 8s on every gate, and the suites tick this
  // constant rather than a copy of it.
  assert.strictEqual(FRESHNESS_CHECK_TIMEOUT_MS, 8 * 1000);
});



test("a quit does not install a stage from the channel the user left, and says why", async () => {
  const h = laneHarness({ channel: "insider" });
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0-insider.1" });
  h.lane.channel = "";
  h.feedFails();
  assert.strictEqual(h.fireQuit(), true, "the quit-time install did not run");
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "installed the insider stage on quit");
  assert.strictEqual(h.calls.quits, 1, "the quit must still finish");
  const notice = h.notices.at(-1);
  assert.strictEqual(notice.title, "Update not installed");
  assert.match(notice.body, /changed the update channel/);
  assert.ok(!h.notices.some((n) => /withdrawn or superseded/.test(n.body)), "a channel change is not a withdrawal");
});

test("a Download click on an offer from the channel the user left asks the selected channel instead", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = laneHarness({ channel: "insider" });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0-insider.1" }); // offered on insider
  h.lane.channel = "";
  const stable = h.feedHangs();
  void u.check(); // the switch's own check, still waiting on stable's feed
  await flush();
  const click = u.download();
  await flush();
  t.mock.timers.tick(FRESHNESS_CHECK_TIMEOUT_MS);
  await click;
  await flush();
  assert.strictEqual(h.calls.downloadUpdate, 0, "downloaded the insider build on a stable-following install");
  assert.strictEqual(h.states.at(-1).state, "checking");
  stable.upToDate();
  await flush();
  assert.strictEqual(h.calls.checkForUpdates, 2, "stable is asked once the old request ends");
});


async function failedInstall(h, u) {
  h.emit("update-downloaded", { version: "1.1.0" });
  h.feedServes("1.1.0");
  await u.install();
  assert.strictEqual(h.calls.quitAndInstall.length, 1);
  h.emit("error", new Error("installer refused")); // recovered in place
  await flush();
}

