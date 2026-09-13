// FRESHNESS contract: an install always applies the NEWEST build, never a
// download the feed has moved past.
//
// The field bug this file pins. Discovery downloads eagerly (auto-download is on
// by default) and then waits — for a click, or for the next natural quit. The
// only thing that noticed a newer release in that window was the 4-hourly poll,
// so an install that landed between two polls applied the stale stage: the app
// relaunched on an already-superseded build, the very next check found the newer
// one, and the whole ~350MB transfer ran again. Two downloads and two restarts
// to reach a version one download could have reached.
//
// The deferred-install-on-quit path was the worst case: "Later" arms a
// before-quit handler that consulted nothing but its own `updateReady` flag, so
// a quit hours after the download installed whatever had been staged back then.
//
// Both install entry points now re-consult the feed as their last step, and both
// must FAIL OPEN — a feed that cannot answer must never make already-downloaded
// bytes uninstallable.
const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");

/** Let queued microtasks + the async install/quit bodies run to completion. */
async function flush(times = 8) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function makeHarness({ appVersion = "1.0.0", autoDownload = true } = {}) {
  const calls = {
    quitAndInstall: [],
    notifications: [],
    states: [],
    checks: 0,
    downloads: 0,
    quits: 0,
    stops: 0,
    // onInstallFailed is the host's gateway recovery: it respawns the gateway
    // child, so it must never fire while a bundle swap is in progress.
    installFailures: 0,
  };
  const handlers = {};
  const quitHandlers = [];
  // What the NEXT checkForUpdates() does — the seam that lets a test say "the
  // feed has moved on" / "the release was retracted" / "the feed is down".
  let onCheck = null;
  // Parks stopGateway. That await is the one gap between an install claiming
  // `installing` and dispatching the installer, so it is the window a concurrent
  // quit has to be tested against.
  let holdStop = null;

  const autoUpdater = {
    setFeedURL: () => {},
    checkForUpdates: async () => {
      calls.checks += 1;
      if (onCheck) await onCheck();
    },
    downloadUpdate: async () => { calls.downloads += 1; },
    quitAndInstall: (...a) => calls.quitAndInstall.push(a),
    on: (ev, fn) => { handlers[ev] = fn; },
  };

  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => appVersion,
      once: (ev, fn) => { if (ev === "before-quit") quitHandlers.push(fn); },
      removeListener: (ev, fn) => {
        const i = quitHandlers.indexOf(fn);
        if (i >= 0) quitHandlers.splice(i, 1);
      },
      // Electron emits `before-quit` from app.quit() too, and a handler that
      // preventDefaults CANCELS the quit. Modelled because the difference is
      // load-bearing: a path that quits while its own before-quit listener is still
      // armed re-enters that listener on the way out, and a stub that just counted
      // quits could not tell that apart from a clean exit.
      quit: () => {
        const handler = quitHandlers.pop();
        if (handler) {
          let prevented = false;
          handler({ preventDefault: () => { prevented = true; } });
          if (prevented) return;
        }
        calls.quits += 1;
      },
      exit: () => {},
      relaunch: () => {},
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function (options) {
      calls.notifications.push(options);
      return { show: () => {} };
    },
    getFlavor: () => "stable",
    getAutoDownloadPreference: () => autoDownload,
    stopGateway: async () => { calls.stops += 1; if (holdStop) await holdStop; },
    onInstallFailed: () => { calls.installFailures += 1; },
    osPlatform: "darwin",
    osArch: "arm64",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (payload) => calls.states.push(payload),
    nativeAutoUpdater: { once: () => {} },
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };

  const emit = (ev, payload) => handlers[ev] && handlers[ev](payload);
  return {
    deps,
    calls,
    emit,
    /** The feed answers this on the next check. */
    feedServes: (version) => { onCheck = () => emit("update-available", { version }); },
    feedSaysUpToDate: () => { onCheck = () => emit("update-not-available", {}); },
    feedFails: (err) => { onCheck = () => { throw err; }; },
    feedSilent: () => { onCheck = null; },
    /**
     * A check that hangs until the test lands it — the seam for an ABANDONED
     * check, where the freshness gate's bounded wait expires but the request
     * itself is still running and reports back afterwards.
     */
    feedDeferred: () => {
      let settle = null;
      autoUpdater.checkForUpdates = () => {
        calls.checks += 1;
        return new Promise((resolve, reject) => { settle = { resolve, reject }; });
      };
      const land = (fn) => { fn(); settle.resolve(); };
      return {
        serves: (version) => land(() => emit("update-available", { version })),
        upToDate: () => land(() => emit("update-not-available", {})),
        fails: (err) => { emit("error", err); settle.reject(err); },
        /**
         * The SAME failure with electron-updater's two signals reversed: the
         * promise rejects first and the `error` event follows a tick later.
         * Nothing documents the order `fails` above encodes, so a dependency
         * bump could hand us this instead — and the discrimination between the
         * check's own failure and an installer's must not depend on which.
         */
        failsRejectFirst: async (err) => {
          settle.reject(err);
          await Promise.resolve();
          emit("error", err);
        },
      };
    },
    /**
     * Park stopGateway until the returned release is called, holding an install
     * open at the point where it has claimed `installing` but not yet dispatched.
     */
    holdGatewayStop: () => {
      let release = null;
      holdStop = new Promise((resolve) => { release = resolve; });
      return () => { holdStop = null; release(); };
    },
    /** Fire the before-quit handler armed by a deferred install. */
    /**
     * Fire the before-quit handler armed by a deferred install.
     *
     * Models `app.once` FAITHFULLY: Electron unregisters the listener before
     * invoking it, so the handler is popped here. Keeping it registered (as this
     * did) makes every quit look re-entrant and hides the case where a branch
     * returns with the app still running and never re-arms -- a second Cmd+Q then
     * reaches nothing and Electron exits.
     */
    fireQuit: () => {
      const prevented = { value: false };
      const handler = quitHandlers.pop();
      assert.ok(handler, "no before-quit handler was armed");
      handler({ preventDefault: () => { prevented.value = true; } });
      return prevented;
    },
    armedQuitHandlers: () => quitHandlers.length,
  };
}

// --------------------------------------------------------------------------
// Manual install (the About panel's "Install Update & Restart App")
// --------------------------------------------------------------------------

test("a SUPERSEDED stage is not installed — the newest build is fetched instead", async () => {
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.2.0"); // published while the user sat on the ready card
  await u.install();

  assert.strictEqual(
    h.calls.quitAndInstall.length,
    0,
    "installed the stale 1.1.0 — this is the field bug: the app relaunches on a superseded build and re-downloads 1.2.0",
  );
  assert.strictEqual(h.calls.checks, 1, "the install must ask the feed whether the stage is still latest");
  assert.strictEqual(h.calls.downloads, 1, "the newest build must be pursued, not just refused");
  assert.strictEqual(h.calls.stops, 0, "nothing was installed, so the gateway must not have been stopped");
  const found = h.calls.states.filter((s) => s.state === "found").map((s) => s.version);
  assert.deepStrictEqual(found, ["1.2.0"], "the renderer must be told about the newer version");
});

test("a stage that is STILL the newest installs — the gate must not break the normal path", async () => {
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0"); // nothing new since the download
  await u.install();

  assert.strictEqual(h.calls.quitAndInstall.length, 1);
  assert.strictEqual(h.calls.stops, 1, "the gateway must still be stopped before the swap");
  assert.strictEqual(h.calls.downloads, 0, "an already-staged build must never be re-downloaded");
});

test("a RETRACTED stage is not installed", async () => {
  // The feed repointed to the running version: the same signal the poll's
  // retraction path handles, now reachable at install time too.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedSaysUpToDate();
  await u.install();

  assert.strictEqual(h.calls.quitAndInstall.length, 0, "a withdrawn build must not install");
  assert.ok(
    h.calls.states.some((s) => s.state === "not-available"),
    "the renderer must be told the update went away",
  );
});

test("FAIL OPEN: an unreachable feed still installs the staged build", async () => {
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedFails(Object.assign(new Error("getaddrinfo ENOTFOUND"), { code: "ENOTFOUND" }));
  await u.install();

  assert.strictEqual(
    h.calls.quitAndInstall.length,
    1,
    "bytes the user already downloaded must not become uninstallable because the network went away",
  );
});

test("FAIL OPEN: a feed that answers nothing still installs the staged build", async () => {
  // electron-updater resolving checkForUpdates() without emitting either
  // verdict is the shape every other test harness in this directory uses, so
  // the gate must read it as "no evidence", not as "the stage is gone".
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedSilent();
  await u.install();

  assert.strictEqual(h.calls.quitAndInstall.length, 1);
});

test("FAIL OPEN: a feed that never answers does not leave the install button dead", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  h.deps.autoUpdater.checkForUpdates = () => new Promise(() => {}); // hangs forever
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const installPromise = u.install();
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the install must await the freshness answer first");
  t.mock.timers.tick(8 * 1000); // the bound elapses
  await installPromise;

  assert.strictEqual(
    h.calls.quitAndInstall.length,
    1,
    "a click must never wait on a socket forever — an unanswerable feed falls through to the staged build",
  );
});

test("a second click during the freshness check does not dispatch a second install", async () => {
  // The gate awaits the network BEFORE `installing` is set, so `installing`
  // alone no longer guards re-entry.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  await Promise.all([u.install(), u.install()]);

  assert.strictEqual(h.calls.quitAndInstall.length, 1);
  assert.strictEqual(h.calls.checks, 1, "the second click must not start a second check either");
});

// --------------------------------------------------------------------------
// The consent click (auto-download off): don't spend the bytes on a stale card
// --------------------------------------------------------------------------

test("a click on a stale 'found' card downloads the NEWEST build, not the one on the card", async () => {
  // With auto-download off the card waits for a click that can come hours
  // later. Fetching what it says wastes the whole transfer: the pre-install gate
  // then refuses the stale stage and fetches the newer build anyway.
  const h = makeHarness({ autoDownload: false });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0" }); // the card the user is looking at

  h.feedServes("1.2.0"); // published while it sat there
  await u.download();

  assert.strictEqual(h.calls.checks, 1, "a click must confirm the card before spending the bytes");
  assert.strictEqual(h.calls.downloads, 1, "the newest build must still be downloaded");
  const downloading = h.calls.states.filter((s) => s.state === "downloading").map((s) => s.version);
  assert.strictEqual(downloading.at(-1), "1.2.0", "downloaded the stale 1.1.0 — one wasted transfer");
});

test("a click whose re-check finds the release retracted downloads nothing", async () => {
  const h = makeHarness({ autoDownload: false });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0" });

  h.feedSaysUpToDate();
  await u.download();

  assert.strictEqual(h.calls.downloads, 0, "a withdrawn build must not be fetched");
});

test("two fast clicks fetch once", async () => {
  const h = makeHarness({ autoDownload: false });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0" });

  h.feedServes("1.1.0");
  await Promise.all([u.download(), u.download()]);

  assert.strictEqual(h.calls.downloads, 1);
  assert.strictEqual(h.calls.checks, 1, "the second click must not start a second check either");
});

test("the automatic download inside a check does not re-check", async () => {
  // The automatic caller runs from the update-available handler, i.e. INSIDE a
  // check: its discovery cannot be stale, and re-checking there would recurse.
  const h = makeHarness({ autoDownload: true });
  initAutoUpdate(h.deps);

  h.emit("update-available", { version: "1.1.0" });
  await flush();

  assert.strictEqual(h.calls.checks, 0);
  assert.strictEqual(h.calls.downloads, 1);
});

test("a stale click with auto-download ON still fetches exactly once", async () => {
  // The click's re-check surfaces the newer build, whose handler starts the
  // automatic download; the click must then stand down rather than fetch again.
  const h = makeHarness({ autoDownload: true });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0" });
  await flush();
  assert.strictEqual(h.calls.downloads, 1, "the automatic download runs on discovery");
  h.emit("error", new Error("download failed")); // clears `downloading`, card stays

  h.feedServes("1.2.0");
  await u.download();

  assert.strictEqual(h.calls.downloads, 2, "exactly one further fetch, for the newest build");
});

// --------------------------------------------------------------------------
// Reuse of a check that just happened
// --------------------------------------------------------------------------

test("a click on a card the check has already replaced installs nothing", async () => {
  // The gate reads the live state, so a stage the check dropped is not installable
  // by a click that was queued against the card it replaced.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.2.0");
  await u.check(); // supersede: the stage is dropped, the newer build pursued
  await u.install(); // clicked on a card the check has already replaced

  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the superseded build must not install");
});

test("a build published after the launch check is still caught by the install's own verify", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"] });
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.feedSilent();
  t.mock.timers.tick(30 * 1000); // the launch check gives up on the silent feed
  await flush();
  h.emit("update-downloaded", { version: "1.1.0" });

  // The newer build appears only AFTER that check -- nothing the launch check saw
  // could have known about it, so only the install's own verify can catch it.
  h.feedServes("1.2.0");
  await u.install();

  assert.strictEqual(h.calls.checks, 2, "the install must verify rather than trust the earlier check");
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the superseded stage must not install");
});

// --------------------------------------------------------------------------
// Deferred install on the natural quit (the "Later" path)
// --------------------------------------------------------------------------

test("install-on-quit refuses a stage the feed has moved past, and quits", async () => {
  const h = makeHarness();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" }); // arms before-quit

  h.feedServes("1.2.0");
  const prevented = h.fireQuit();
  await flush();

  assert.ok(prevented.value, "the handler must take the quit over to run its own teardown");
  assert.strictEqual(
    h.calls.quitAndInstall.length,
    0,
    "installed the stale build on quit — the exact path that then re-downloads the newer one on relaunch",
  );
  assert.strictEqual(h.calls.stops, 0, "the gateway must not be stopped for an install that will not happen");
  assert.strictEqual(h.calls.quits, 1, "the user asked to quit; honour it");
  assert.match(
    h.calls.notifications.at(-1).body,
    /withdrawn or superseded/i,
    "a promised update that did not land must be explained, or the old version at next launch reads as a failure",
  );
});

test("the quit-time refusal does not claim a newer version for a RETRACTED build", async () => {
  // Both refusals arrive through the same "superseded" answer -- a retraction
  // clears the stage too -- so copy that announces a newer release would be a
  // false statement of fact on this half, and the user would wait for an offer
  // that never comes.
  const h = makeHarness();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedSaysUpToDate();
  h.fireQuit();
  await flush();

  assert.strictEqual(h.calls.quitAndInstall.length, 0, "a withdrawn build must not install");
  assert.strictEqual(h.calls.quits, 1);
  assert.doesNotMatch(h.calls.notifications.at(-1).body, /newer version has been published/i);
  assert.match(h.calls.notifications.at(-1).body, /withdrawn or superseded/i);
});

test("install-on-quit does not start a download it cannot finish", async () => {
  // Discovering 1.2.0 seconds before the process exits must not begin a ~350MB
  // fetch: the exit kills it, and the next launch re-finds and downloads it.
  const h = makeHarness();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.2.0");
  h.fireQuit();
  await flush();

  assert.strictEqual(h.calls.downloads, 0);
});

test("install-on-quit still installs when the stage is the newest", async () => {
  const h = makeHarness();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  h.fireQuit();
  await flush();

  assert.strictEqual(h.calls.quitAndInstall.length, 1);
  assert.strictEqual(h.calls.stops, 1, "the gateway must stop before the bundle swap");
  assert.strictEqual(h.calls.quits, 0, "quitAndInstall owns the exit on this path");
});

test("an installer failure on the quit path is reported as an install failure", async () => {
  // This path preventDefaults the quit and hands off, and nothing else notices if
  // the handoff is REFUSED: the exit failsafe arms only on
  // `before-quit-for-update`, which a rejected installer never emits, so the app
  // survives its own failed quit. The phase must therefore derive as "install"
  // here — that is what tells the failure apart from a check's, and it only holds
  // if the dispatch claimed `installing` before calling through. What the install
  // phase then does with a QUIT-path failure is the opposite of the manual path's
  // recovery: it completes the quit (see the dedicated test below).
  const h = makeHarness();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  h.fireQuit();
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "precondition: the install was dispatched");

  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  }));
  await flush();

  assert.strictEqual(h.calls.quits, 1, "the quit this install prevented must still complete");
  assert.strictEqual(h.calls.installFailures, 0, "and nothing must respawn a gateway the app is leaving behind");
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.ok(failure, "the renderer must be taken off the installing overlay this path emitted");
  assert.strictEqual(failure.phase, "install", "misread as a check, this failure is never acted on at all");
});

test("FAIL OPEN on quit: a feed that never answers must not hold the app open", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  h.deps.autoUpdater.checkForUpdates = () => new Promise(() => {}); // hangs forever
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the quit path must await the freshness answer first");
  t.mock.timers.tick(8 * 1000); // the bound elapses
  await flush();

  assert.strictEqual(
    h.calls.quitAndInstall.length,
    1,
    "the quit took over the exit; a hanging feed must not strand the app between quitting and installing",
  );
});

test("FAIL OPEN on quit: an unreachable feed still installs the staged build", async () => {
  const h = makeHarness();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedFails(Object.assign(new Error("socket hang up"), { code: "ECONNRESET" }));
  h.fireQuit();
  await flush();

  assert.strictEqual(h.calls.quitAndInstall.length, 1);
});

// --------------------------------------------------------------------------
// The ABANDONED check: fail-open's own side effect
//
// The bound the gate puts on the feed ends the WAIT, not the request. Fail-open
// then sends that caller straight on to install, so the request's outcome can
// land AFTER the install is dispatched — the one thing the post-stopGateway
// abort exists to stop. Aborting cannot be the answer (aborting is what
// fail-open refuses to do), so the late outcome must be inert instead.
// --------------------------------------------------------------------------

test("an abandoned check FAILING after the dispatch must not fire the gateway recovery", async (t) => {
  // Recovery respawns the gateway child. Firing it here would put a live gateway
  // in the middle of the bundle swap — exactly what the strict stop-then-install
  // order exists to prevent — and tell the user the install failed while it runs.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  const feed = h.feedDeferred();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const installPromise = u.install();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await installPromise;
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  feed.fails(Object.assign(new Error("ETIMEDOUT"), { code: "ETIMEDOUT" }));
  await flush();
  // The suppression is DEFERRED by a macrotask (it has to ask whether the error
  // was the abandoned request's own — see the error handler), so drive the clock
  // or this test would pass because the decision never ran.
  t.mock.timers.tick(1);
  await flush();

  assert.strictEqual(h.calls.installFailures, 0, "the gateway must not be respawned during the swap");
  assert.ok(
    !h.calls.states.some((s) => s.state === "error"),
    "an install that is proceeding must not be reported as failed",
  );
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "and no second dispatch");
});

test("a GENUINE install failure inside the abandoned window is still reported", async (t) => {
  // The twin of the test above, and the reason the suppression cannot be a bare
  // flag test. `error` is the one event both the check and the installer are
  // funnelled through, and the window is wide: the request was abandoned BECAUSE
  // it gave no answer in 8s, so a hung socket holds it open for the OS TCP
  // timeout while the install handoff it released takes about a second. Squirrel
  // rejecting the bundle in there must still reach the host — the gateway was
  // stopped on purpose and only onInstallFailed brings it back, or the app
  // survives with a dead dashboard and the renderer sits on the installing
  // overlay forever.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  h.feedDeferred(); // deliberately never landed: the request is still hanging
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const installPromise = u.install();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await installPromise;
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  // Not the feed's error — the installer's, arriving while the abandoned request
  // is still outstanding.
  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), { code: "ERR_UPDATER_INVALID_SIGNATURE" }));
  await flush();
  t.mock.timers.tick(1);
  await flush();

  assert.strictEqual(h.calls.installFailures, 1, "the failure must reach the host's install-failure hook");
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.ok(failure, "the renderer must be told the install failed, not left on the overlay");
  assert.strictEqual(failure.phase, "install", "and it must be attributed to the install, not the check");
});

test("an abandoned check finding a NEWER build after the dispatch changes nothing", async (t) => {
  // Two hazards in one event: dropping the stage would invalidate a dispatch that
  // has already passed every guard, and the automatic download would spend ~350MB
  // as the process exits. The next launch finds this version and offers it then.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness({ autoDownload: true });
  const feed = h.feedDeferred();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const installPromise = u.install();
  await flush();
  t.mock.timers.tick(8 * 1000);
  await installPromise;
  assert.strictEqual(h.calls.quitAndInstall.length, 1);

  feed.serves("1.2.0");
  await flush();

  assert.strictEqual(h.calls.downloads, 0, "no fetch may start while the bundle is being swapped");
  assert.ok(!h.calls.states.some((s) => s.state === "found"), "and no card may replace the running install");
});

test("an abandoned check reporting UP TO DATE after the dispatch does not pull the stage", async (t) => {
  // update-not-available clears `updateReady` and the staged version — the
  // retraction path. Landing that mid-swap would strip the bytes from an install
  // already under way.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  const feed = h.feedDeferred();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const installPromise = u.install();
  await flush();
  t.mock.timers.tick(8 * 1000);
  await installPromise;

  feed.upToDate();
  await flush();

  assert.ok(
    !h.calls.states.some((s) => s.state === "not-available"),
    "the renderer must not be told the update went away while it is installing",
  );
  assert.strictEqual(h.calls.quitAndInstall.length, 1);
});

test("FAIL OPEN on quit: an abandoned check's late discovery does not fetch as the app exits", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness({ autoDownload: true });
  const feed = h.feedDeferred();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  t.mock.timers.tick(8 * 1000);
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "the quit took over the exit; it must still install");

  feed.serves("1.2.0");
  await flush();

  assert.strictEqual(h.calls.downloads, 0);
  assert.strictEqual(h.calls.quits, 0, "quitAndInstall owns the exit on this path");
});

test("a check that is GENUINELY in flight still aborts the install after the gateway stops", async () => {
  // The relaxation above is scoped to the abandoned case. An ordinary concurrent
  // check has made no fail-open promise, and its outcome can still invalidate the
  // stage or be misread as an install failure, so the dispatch must still be the
  // serialization point — abort, restore the gateway, and say so.
  const h = makeHarness();
  const feed = h.feedDeferred();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const checkPromise = u.check(); // hangs, holding `checking`
  await flush();
  await u.install();

  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the install must not race a live check");
  assert.strictEqual(h.calls.stops, 1, "the gateway was stopped for a dispatch that then aborted…");
  assert.strictEqual(h.calls.installFailures, 1, "…so the host must be told to bring it back");
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.strictEqual(failure.phase, "install");
  assert.strictEqual(failure.code, "check-in-flight");

  feed.upToDate(); // let the check land so nothing is left pending
  await checkPromise;
});

test("a genuine installer failure on the QUIT path inside the abandoned window is reported", async (t) => {
  // The two relaxations compose here, and this is the combination that has to
  // survive both: the quit path's freshness gate is the thing that abandons the
  // check, so "abandoned check" and "deferred install" are not independent
  // states — they are the SAME sequence. Suppressing on the flag alone would
  // swallow the installer's refusal; deriving the phase without the dispatch's
  // `installing` claim would report it as a check, and nothing on this path acts
  // on a check failure — the app would survive the quit it had already prevented,
  // gateway stopped, renderer still on the installing overlay.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  h.feedDeferred(); // deliberately never landed: the request is still hanging
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  }));
  await flush();
  t.mock.timers.tick(1); // the deferred "was this the abandoned request's own?" decision
  await flush();

  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.ok(failure, "the renderer must not be left on the installing overlay");
  assert.strictEqual(failure.phase, "install", "and it must be attributed to the install, not the check");
  // The quit finishes rather than the gateway coming back — see the dedicated
  // test below for why recovery is the wrong answer on this path.
  assert.strictEqual(h.calls.quits, 1, "the quit this install prevented must still complete");
  assert.strictEqual(h.calls.installFailures, 0, "and nothing must respawn a gateway the app is leaving behind");
});

test("a FAILED deferred install finishes the quit instead of surviving with a dead dashboard", async () => {
  // The failure mode the phase claim exists for, on the path that cannot recover
  // from it. `quitAndInstall()` rejected (observed live: a Squirrel signature
  // rejection), so: the quit was already preventDefault'ed, the gateway is
  // already stopped, and `before-quit-for-update` never fired so the force-exit
  // failsafe never armed. Recovering in place — the manual path's answer — would
  // strand the app running with a window the user asked to close; the host's
  // recovery could not do it anyway, because it bails while the app is quitting.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });
  h.feedServes("1.1.0"); // still the newest: nothing else may stop this install

  h.fireQuit();
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "the install must have been dispatched");
  assert.strictEqual(h.calls.quits, 0, "and the quit is still parked behind it");

  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  }));
  await flush();

  assert.strictEqual(h.calls.quits, 1, "the prevented quit must complete — the app must not survive it");
  assert.strictEqual(
    h.calls.installFailures,
    0,
    "and the gateway must not be respawned into a process that is exiting",
  );
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.strictEqual(failure.phase, "install", "the failure is the installer's, not a check's");
  const notice = h.calls.notifications.at(-1);
  assert.match(
    notice.body,
    /offered it again next launch/,
    "the user was promised an install on quit; the stage survives, so say so",
  );
  assert.doesNotMatch(
    notice.body,
    /withdrawn or superseded/,
    "that is the OTHER refusal's reason and would be a false statement here",
  );
  // `deferredInstalling` must not LATCH: left standing it would quit the app on the
  // next failure instead of recovering in place, which is the same "app leaves
  // without explanation" bug in the opposite direction.
  //
  // This is deliberately NOT probed by clicking Install here. `quitHandled` is set
  // and the app is quitting, so the entry guard in applyUpdateAndRestart correctly
  // refuses that click -- and it must, or the click would dispatch a second
  // quitAndInstall() against the same install directory. Both in-process resets of
  // `quitHandled` discard the stage, so the same stage genuinely only returns at the
  // next launch, in a fresh process where the flag starts false. The recover-in-place
  // half is pinned on a fresh instance by the two manual-path tests above.
  assert.strictEqual(
    h.calls.quitAndInstall.length,
    1,
    "and the refused click must not dispatch a second install against the same directory",
  );

  // What IS observable here: a further failure must not be credited to a deferred
  // install and quit the app again. One quit, from the one dispatch that earned it.
  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  }));
  await flush();

  assert.strictEqual(h.calls.quits, 1, "a latched deferredInstalling would quit a second time");
});

test("the abandoned check's own failure stays inert when the rejection lands before the error event", async (t) => {
  // The order electron-updater fires its two signals in is undocumented, and
  // every test here mocks the updater — so a dependency bump that rejected
  // BEFORE emitting would pass a suite that reads the flag's timing instead of
  // the error's identity, and revive exactly the hazard this guard exists to
  // stop: the check's own failure deriving phase "install" and firing the host's
  // gateway recovery in the middle of the bundle swap. Same failure as the test
  // above, signals reversed; the verdict must not move.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  const feed = h.feedDeferred();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  await feed.failsRejectFirst(Object.assign(new Error("socket hang up"), { code: "ECONNRESET" }));
  await flush();
  t.mock.timers.tick(1);
  await flush();

  assert.strictEqual(h.calls.installFailures, 0, "the check's own failure must not respawn the gateway mid-swap");
  assert.strictEqual(h.calls.states.filter((s) => s.state === "error").length, 0, "nor surface as an install error");
});

test("an installer refusal and the abandoned check's own failure in one tick are told apart", async (t) => {
  // Both events queue their decision before EITHER rejection lands, so there is a
  // moment where the only thing distinguishing them is which object they carry.
  // Answering both from "a failure is on record" gets them exactly backwards:
  // the installer's refusal is credited to the check and swallowed, leaving the
  // app alive on a quit it already prevented, and the check's own network error is
  // reported as an install failure and quits the app. Two errors, two verdicts,
  // and neither may borrow the other's.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  const feed = h.feedDeferred();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  })); // the installer refuses FIRST
  feed.fails(Object.assign(new Error("socket hang up"), { code: "ECONNRESET" })); // then the check gives up
  await flush();
  t.mock.timers.tick(1);
  await flush();

  assert.strictEqual(h.calls.quits, 1, "the installer's refusal must finish the quit it was dispatched from");
  const errors = h.calls.states.filter((s) => s.state === "error");
  assert.strictEqual(errors.length, 1, "and the abandoned check's own failure must stay inert");
  assert.strictEqual(errors[0].phase, "install", "the one reported error is the installer's");
});

test("a second failure after the abandoned check's own is reported, not sheltered by it", async (t) => {
  // One rejection explains one `error` event. An abandoned request that failed is
  // evidence about ITS event and nothing after it — so if the installer then
  // refuses, the recorded failure must already be spent. Leaving it standing
  // would swallow every later error for the rest of the quit, and the quit this
  // path prevented would never complete.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  const feed = h.feedDeferred();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  feed.fails(Object.assign(new Error("socket hang up"), { code: "ECONNRESET" }));
  await flush();
  t.mock.timers.tick(1);
  await flush();
  assert.strictEqual(h.calls.quits, 0, "precondition: the check's own failure was ignored");
  assert.strictEqual(
    h.calls.states.filter((s) => s.state === "error").length, 0,
    "precondition: and it reported nothing",
  );

  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  }));
  await flush();
  t.mock.timers.tick(1);
  await flush();

  assert.strictEqual(h.calls.quits, 1, "the installer's refusal must still be acted on");
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.ok(failure, "the renderer must not be left on the installing overlay");
  assert.strictEqual(failure.phase, "install", "and it must be attributed to the install, not the check");
});

test("a genuine install failure is reported even if the abandoned check answers in the same tick", async (t) => {
  // The narrow race the flag-timing read gets wrong today: the installer refuses
  // FIRST, and the abandoned request's verdict lands before the deferred decision
  // runs. Reading "the flag cleared, so that was the check's" credits the check
  // with someone else's failure, and the quit this path prevented never finishes.
  // A successful verdict is not evidence of a failure, and there is no rejection
  // here to account for the error — so it must be reported.
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const h = makeHarness();
  const feed = h.feedDeferred();
  initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.fireQuit();
  await flush();
  t.mock.timers.tick(8 * 1000); // the WAIT ends; the request is still running
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "fail-open must have installed the staged build");

  h.emit("error", Object.assign(new Error("Could not get code signature for running application"), {
    code: "ERR_UPDATER_INVALID_SIGNATURE",
  }));
  feed.upToDate(); // the abandoned request answers, successfully, right behind it
  await flush();
  t.mock.timers.tick(1);
  await flush();

  assert.strictEqual(h.calls.quits, 1, "the failure must still finish the quit it was dispatched from");
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.ok(failure, "the renderer must not be left on the installing overlay");
  assert.strictEqual(failure.phase, "install", "and it must be attributed to the install, not the check");
});

test("an ordinary check straddling the quit blocks the install instead of being misread as the installer", async () => {
  // The freshness gate returns "unknown" WITHOUT waiting when a check is already
  // in flight, so a routine poll can still be live at the dispatch — and it is not
  // one the abandoned-check identity trick can account for, because nothing here
  // holds its promise. Its late failure would arrive with `installing` claimed and
  // be reported as the installer's: recovery fired for a feed error, in the middle
  // of the bundle swap. The manual path refuses in exactly this state; so must
  // this one. The stage survives, and the next launch offers it.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const feed = h.feedDeferred();
  u.check(); // deliberately not awaited: a poll still in flight when the user quits
  await flush();

  h.fireQuit();
  await flush();

  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the install must not be dispatched under a live check");
  assert.strictEqual(h.calls.quits, 1, "and the quit the user asked for must still happen");
  assert.strictEqual(h.calls.notifications.length, 1, "with the reason explained, or next launch reads as a failure");
  // And the reason has to be the TRUE one. Nothing was withdrawn here: the stage is
  // intact and the next launch re-offers the same version, so the retraction copy
  // would be contradicted by what the user sees a moment later -- which is how an
  // updater loses trust even when it behaved correctly.
  const deferred = h.calls.notifications.at(-1);
  assert.doesNotMatch(
    deferred.body,
    /withdrawn or superseded/i,
    "a straddling check retracts nothing, so it must not borrow the retraction's reason",
  );
  assert.match(deferred.body, /check was still running/i, "it names the check that actually caused it");
  assert.match(deferred.body, /next launch/i, "and keeps the promise that makes it survivable");

  feed.fails(Object.assign(new Error("socket hang up"), { code: "ECONNRESET" }));
  await flush();

  assert.strictEqual(h.calls.installFailures, 0, "the straddling check's failure is not the installer's");
  const errors = h.calls.states.filter((s) => s.state === "error");
  assert.ok(errors.length > 0, "it is still surfaced");
  assert.ok(errors.every((e) => e.phase === "check"), "as a check failure, which is what it is");
});

// --------------------------------------------------------------------------
// The gates run INSIDE another action's click, so they must not report
// themselves as that user's own "check for updates".
// --------------------------------------------------------------------------

test("a Download click's own re-check does not blank the card the user just clicked", async () => {
  // `checking` is the renderer's state for "the user asked whether there is an
  // update", and it UNMOUNTS the whole update card (showUpdateCard in
  // AboutPanel gates on !checking). Emitted from inside this gate, the offer, the
  // Download button and the progress region all vanish for a feed round trip and
  // then come back — which reads as "my click dismissed the update".
  const h = makeHarness({ autoDownload: false });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0" }); // the card the user clicks

  const feed = h.feedDeferred();
  const click = u.download();
  await flush();
  h.emit("checking-for-update"); // electron-updater's own signal for the same check
  await flush();

  assert.ok(
    !h.calls.states.some((s) => s.state === "checking"),
    "the gate's own round trip must not be reported as the user's check",
  );

  feed.serves("1.1.0");
  await click;
  assert.strictEqual(h.calls.downloads, 1, "and the click still downloads");
});

test("an Install click's own freshness check does not blank the ready card either", async () => {
  // Same reason, other gate: this one runs before the "installing" state is
  // emitted, so suppressing it is what keeps the ready card on screen for the
  // whole wait instead of blanking the surface the user just acted on.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const feed = h.feedDeferred();
  const click = u.install();
  await flush();
  h.emit("checking-for-update");
  await flush();

  assert.ok(
    !h.calls.states.some((s) => s.state === "checking"),
    "the freshness gate's round trip must not be reported as the user's check",
  );

  feed.serves("1.1.0");
  await click;
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "and the install still proceeds");
});

test("a Download click waits for a check already in flight instead of re-reading its state", async () => {
  // safeCheck refuses to re-enter a running check, so calling it here would be a
  // silent no-op — and the gate would then conclude "still the newest" from the
  // very verdict it was sent to re-confirm, spending the ~350MB transfer on the
  // stale build. That is the waste this gate exists to prevent, arrived at through
  // the gate itself.
  const h = makeHarness({ autoDownload: false });
  const u = initAutoUpdate(h.deps);
  h.emit("update-available", { version: "1.1.0" }); // the card

  const feed = h.feedDeferred();
  u.check(); // a routine poll, still in flight when the user clicks
  await flush();

  const click = u.download();
  await flush();
  assert.strictEqual(h.calls.downloads, 0, "the click must not fetch on state the running check is replacing");
  assert.strictEqual(h.calls.checks, 1, "and must not issue a second check, which would be refused anyway");

  feed.serves("1.2.0"); // the poll's answer: a newer build
  await click;

  assert.strictEqual(h.calls.downloads, 1, "the click is honoured, not swallowed");
  const downloading = h.calls.states.filter((s) => s.state === "downloading").map((s) => s.version);
  assert.strictEqual(downloading.at(-1), "1.2.0", "downloaded the stale 1.1.0 — one wasted transfer");
});

test("FAIL OPEN on quit: a STRADDLING check's late discovery does not fetch as the app exits", async () => {
  // The gate returns "unknown" without making a request when a check is already in
  // flight, so it has no continuation of its own to hang the quiet on. It still
  // needs the quiet: that foreign check is about to answer, and a newer version in
  // the answer starts a ~350MB automatic download seconds before the process exits.
  // The abandoned-check twin of this is covered above; this is the short-circuit.
  const h = makeHarness({ autoDownload: true });
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  const feed = h.feedDeferred();
  u.check(); // a routine poll, still in flight when the user quits
  await flush();

  const release = h.holdGatewayStop();
  h.fireQuit();
  await flush();

  feed.serves("1.2.0"); // the poll answers mid-teardown: a newer build
  release();
  await flush();

  assert.strictEqual(h.calls.downloads, 0, "a quitting app must not start a transfer it cannot finish");
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "and the superseded stage must not install");
  assert.strictEqual(h.calls.quits, 1, "the quit the user asked for still happens");
});

test("a quit during a manual install does not hand the installer a second dispatch", async () => {
  // The manual path claims `installing` and then awaits stopGateway. A Cmd+Q in
  // that window reaches the quit handler, whose own `checking` abort cannot see
  // this state — so without an entry guard it runs the whole quit-time install:
  // a second quitAndInstall() against the same install directory, a second
  // gateway stop, and a second force-exit failsafe.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  const release = h.holdGatewayStop();
  const click = u.install();
  await flush();
  assert.strictEqual(h.calls.stops, 1, "the install is parked on the gateway stop");

  const prevented = h.fireQuit();
  await flush();
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the quit must not dispatch an install of its own");
  // It must PREVENT rather than stand down. The install underway does end in a quit
  // of its own -- but not yet: it is parked on the gateway stop, so an unprevented
  // quit lets Electron exit first and the user who asked to install and restart gets
  // a plain quit with the stage uninstalled.
  assert.ok(prevented.value, "the quit must be held, not obeyed, while the manual path owns the install");
  assert.strictEqual(h.calls.quits, 0, "and nothing may quit before the handoff");

  release();
  await click;
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "exactly one handoff to the platform installer");
  assert.strictEqual(h.calls.stops, 1, "and the gateway is stopped once, not twice");
});

test("an install click during a quit-time install does not hand the installer a second dispatch", async () => {
  // The mirror of the test above, and the same hazard from the other side. The quit
  // path claims only `quitHandled`, then spends its own await on the freshness gate
  // and stopGateway before it sets `installing` -- so in that window both `installing`
  // and `verifyingStage` read false, and a click that got past the entry guard would
  // dispatch a second quitAndInstall() against the same install directory mid-swap.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  const release = h.holdGatewayStop();
  h.fireQuit(); // the quit-time install takes over and parks on the gateway stop
  await flush();
  assert.strictEqual(h.calls.stops, 1, "precondition: the quit-time install is parked mid-stop");
  assert.strictEqual(h.calls.quitAndInstall.length, 0, "precondition: it has not dispatched yet");

  await u.install(); // the user clicks Install while that is still in flight
  await flush();

  assert.strictEqual(h.calls.stops, 1, "the click must not stop the gateway a second time");

  release();
  await flush();

  assert.strictEqual(
    h.calls.quitAndInstall.length,
    1,
    "exactly one handoff to the platform installer, not two against the same install directory",
  );
});

test("a SECOND quit during the same manual install is still held, not obeyed", async () => {
  // The listener is registered with `app.once`, so Electron unregisters it before
  // invoking it. Every other exit from the handler quits or hands off, which makes
  // being consumed correct for them -- but the defer branch returns with the app
  // still running, so unless it RE-ARMS, a second Cmd+Q (the natural reaction to an
  // app that did not quit) reaches only main.js's handler and Electron exits before
  // quitAndInstall() runs. That is the exact outcome the defer branch exists to stop.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  const release = h.holdGatewayStop();
  const click = u.install();
  await flush();

  const first = h.fireQuit();
  await flush();
  assert.ok(first.value, "precondition: the first quit is held");

  // Impatient second Cmd+Q. There must still be a handler to receive it.
  const second = h.fireQuit();
  await flush();
  assert.ok(second.value, "the second quit must be held too, or Electron exits mid-install");
  assert.strictEqual(h.calls.quits, 0, "and nothing may quit before the handoff");

  release();
  await click;
  assert.strictEqual(h.calls.quitAndInstall.length, 1, "the install the user asked for still happens, exactly once");
});

test("the replayed quit is a plain quit, not a fresh quit-time install", async () => {
  // `honorDeferredQuit` calls app.quit(), which fires before-quit again. The listener
  // the defer branch re-armed is still registered at that point, and by then the abort
  // has cleared `installing` -- so without removing it first, the replayed quit falls
  // through to the quit-time install branch and dispatches the very install the abort
  // just refused.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  // A poll is ALREADY in flight when the user clicks -- the only way to reach the
  // post-stopGateway abort with the stage still valid. `safeCheck` refuses to start a
  // check once `installing` is set, so this has to come first. The freshness gate then
  // fails open on the in-flight check and the install proceeds.
  h.feedDeferred();
  u.check();
  await flush();

  const release = h.holdGatewayStop();
  const click = u.install();
  await flush();
  h.fireQuit();
  await flush();

  // The gateway comes back up and the abort fires: `checking` is still true and this
  // check was never abandoned, so the dispatch is refused with the stage untouched.
  release();
  await click;

  assert.strictEqual(h.calls.quits, 1, "the held quit is honored");
  assert.strictEqual(
    h.calls.quitAndInstall.length,
    0,
    "and it must not become an install the abort just refused",
  );
});

test("a quit held during a manual install is honored when that install aborts", async () => {
  // The other half of holding the quit: if the install never happens, the request to
  // leave must not vanish with it. An app that silently ignores Cmd+Q is worse than
  // one that quits late, because nothing tells the user it was dropped.
  const h = makeHarness();
  const u = initAutoUpdate(h.deps);
  h.emit("update-downloaded", { version: "1.1.0" });

  h.feedServes("1.1.0");
  const release = h.holdGatewayStop();
  const click = u.install();
  await flush();

  const prevented = h.fireQuit();
  await flush();
  assert.ok(prevented.value, "precondition: the quit is held");
  assert.strictEqual(h.calls.quits, 0, "precondition: and not yet acted on");

  // The stage is invalidated while the gateway is down, so the post-stopGateway
  // abort runs instead of the handoff.
  h.emit("update-not-available", {});
  release();
  await click;

  assert.strictEqual(h.calls.quitAndInstall.length, 0, "the invalidated stage must not install");
  assert.strictEqual(h.calls.quits, 1, "and the quit the user asked for must still happen");
  const failure = h.calls.states.filter((s) => s.state === "error").at(-1);
  assert.ok(failure, "with the abort reported before the window goes");
  assert.strictEqual(failure.phase, "install", "as an install failure, which is what the user was watching");
});
