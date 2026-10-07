// Every downloaded update is installed only after the gateway has stopped: see
// the configureUpdater note in auto-update.js for why electron-updater's own
// install-on-quit stays off on every platform.
const FORCE_EXIT_AFTER_MS = 5 * 1000; // failsafe: guarantee exit after quitAndInstall

/**
 * The electron-updater feed lane: discovery against the per-channel feed,
 * the consent and automatic download paths, the staged-update state, and the
 * install handoff that stops the gateway before the platform installer swaps
 * the bundle (manually, or deferred to the natural quit).
 *
 * Created only once every gate in initAutoUpdate has passed and
 * configureUpdater has applied the update policy; it registers the six
 * electron-updater events, points the feed, and arms the launch check and the
 * poll, in that order.
 *
 * @returns {{check: Function, download: Function, install: Function, getInfo: Function, isReady: Function}}
 */
function createFeedLane({
  app,
  autoUpdater,
  dialog,
  Notification,
  getAutoDownloadPreference,
  notifyUpdateFound,
  stopGateway,
  onInstallDispatched,
  onInstallFailed,
  osPlatform,
  linux,
  macDistArch,
  nativeAutoUpdater,
  feedBase,
  uiDriven,
  log,
  reporter: { currentChannel, emit, getInfo, recordLaneVersion },
  buildFeedBase,
  classifyError,
  shouldAutoOffer,
  resolveChannel,
  channelForVersion,
  launchCheckDelayMs,
  checkIntervalMs,
}) {
  let updateReady = false;
  let downloading = false;
  let stagedVersion = null; // version electron-updater has downloaded + staged
  let stagedNotes = "";
  // Was the staged build fetched by the auto-download policy rather than asked
  // for? It decides whether turning the preference OFF also disarms the
  // install-on-quit: a stage the user never requested must not land on a user
  // who has just declined auto-updates, while a stage they explicitly
  // downloaded stays armed because the preference is not what put it there.
  let stagedWasAutomatic = false;
  // Set when startDownload() is entered from the discovery handler, and read by
  // the update-downloaded handler -- the event carries no provenance of its own.
  let downloadWasAutomatic = false;
  let foundVersion = null; // last version surfaced to the user, awaiting consent
  let installing = false;
  let quitHandled = false;
  let checking = false;
  // The channel the LAST configureFeed() pointed the updater at. Captured at
  // check time because the update-available handler's direction gate must
  // compare the candidate against the channel its FEED was configured for, not
  // against a live currentChannel() read: the preference can flip mid-flight
  // (an in-flight stable check, then the user picks insider), and re-reading it
  // in the handler would treat a stable-feed downgrade as a deliberate insider
  // switch and stage it. Null until the first configureFeed().
  let feedChannel = null;

  /**
   * Version of the update currently being fetched/held -- NOT the running
   * app's version. Every state the UI renders a version for must pass this
   * explicitly: emit() defaults `version` to app.getVersion() so the
   * check/not-available/error states report the running build, and a
   * "downloading" event that omitted it made the update card claim the app
   * was downloading the version already installed (fixed in #709; preserved
   * here through the electron-updater migration).
   */
  /**
   * Emit a failure WITH ITS PHASE. Without the phase the renderer cannot tell a
   * discovery failure from a download failure, so it labelled every error
   * "Couldn't check for updates" and unmounted the update card -- a user who
   * clicked Download saw a complaint about checking and lost the version they
   * had just consented to (#735).
   *
   * A download-phase failure also carries the pending version, so the card can
   * stay on screen and offer a retry instead of vanishing.
   *
   * @param {"check"|"download"|"install"} phase
   * @param {unknown} err
   */
  function emitError(phase, err) {
    const { code, detail, httpStatus } = classifyError(err);
    log.error(`[update] ${phase} failed (${code})`, err);
    emit("error", {
      phase,
      code,
      message: detail,
      ...(httpStatus === undefined ? {} : { httpStatus }),
      ...(phase === "download" ? { version: pendingVersion() } : {}),
    });
  }

  function pendingVersion() {
    return foundVersion || stagedVersion || app.getVersion();
  }

  function configureFeed() {
    const channel = currentChannel();
    // Record the channel this check's feed is configured for, so the
    // update-available handler compares the candidate against THIS lane rather
    // than a currentChannel() that may have changed since (see feedChannel).
    feedChannel = channel;
    // A package install reads its channel file from a per-format subdirectory,
    // so the two Linux formats never overwrite each other's metadata; a
    // single-arch mac build reads its own per-arch subdirectory for the same
    // reason (the universal build's variant is "", i.e. the channel root).
    const variant = osPlatform === "darwin" ? macDistArch : linux.format;
    const url = buildFeedBase({ base: feedBase, channel, variant });
    autoUpdater.setFeedURL({ provider: "generic", url });
    log.info(`[update] feed: ${url}`);
    return url;
  }

  /**
   * DISCOVERY ONLY. With autoDownload=false, checkForUpdates() fetches the
   * channel file, compares versions (difference-based via allowDowngrade) and
   * emits update-available / update-not-available WITHOUT downloading. The
   * download requires the explicit download() consent call below.
   */
  async function safeCheck() {
    if (checking) return;
    if (installing || quitHandled) {
      // Install activity: the gateway is stopped on purpose and the process
      // is handing off to the platform installer. The poll timer already
      // skips this window (see pollTimer below); the renderer-driven path
      // must refuse for the same reasons — a check outcome here either races
      // the handoff or, because `installing` outranks `checking` in the error
      // handler's phase derivation, a feed failure would fire the host's
      // gateway recovery in the middle of the bundle swap.
      log.info("[update] check requested during install activity — skipping");
      return;
    }
    if (downloading) {
      // A download is in flight. Re-entering the check would restart the
      // updater's flow underneath the running download; report progress
      // instead. update-downloaded/error clears the flag.
      log.info("[update] check requested while download in flight — reporting progress");
      emit("downloading", { version: pendingVersion() });
      return;
    }
    if (updateReady && stagedVersion) {
      // NOTE: deliberately NOT a short-circuit. A check must ALWAYS consult
      // the feed, even with a version already staged, because a NEWER version
      // can ship mid-session — returning early here would pin the user to the
      // stale stage until they installed or restarted. The update-available
      // handler distinguishes "the staged one is still latest" (re-surface the
      // install prompt) from "the stage is superseded" (drop it and re-find).
      log.info(`[update] ${stagedVersion} staged — checking whether it is still latest`);
    }
    checking = true;
    try {
      configureFeed(); // re-read flavor/channel each check
      emit("checking");
      await autoUpdater.checkForUpdates();
    } catch (err) {
      emitError("check", err);
    } finally {
      checking = false;
    }
  }

  /**
   * Download the version last surfaced by safeCheck.
   *
   * Reached two ways: the user's explicit Download action, and — when
   * getAutoDownloadPreference() is on — automatically from the
   * "update-available" handler. Both enter here rather than through
   * electron-updater's own autoDownload flag, which stays false: routing every
   * download through one guarded function is what keeps the decision
   * inspectable, cancellable by preference, and identical on all platforms.
   *
   * Every early return below is load-bearing for the automatic caller, which
   * fires on a 4-hourly timer and can therefore re-enter: an in-flight download
   * is not restarted, an already-staged version is not re-fetched, and a call
   * with nothing discovered discovers instead of blind-downloading.
   */
  async function startDownload({ automatic = false } = {}) {
    if (installing || quitHandled) {
      // An install dispatch is underway: the gateway is being stopped on
      // purpose and the process is handing off to the platform installer. A
      // download must not start in this window. The dangerous caller is the
      // AUTOMATIC path: a feed check already in flight when the user clicks
      // Install can return a NEWER build and fire startDownload({automatic})
      // from the update-available handler mid-dispatch. Letting it set
      // `downloading = true` would corrupt the error handler's phase
      // derivation (`downloading ? "download" : installing ? "install"`), so a
      // failure landing while the gateway is stopped would be misattributed to
      // the download phase and skip the install-phase abort that restores the
      // stopped gateway -- leaving the app with a dead dashboard. Refuse here,
      // the same way safeCheck() already refuses during install activity.
      log.info("[update] download requested during install activity — skipping");
      return;
    }
    if (downloading) { emit("downloading", { version: pendingVersion() }); return; }
    if (updateReady && stagedVersion) {
      emit("downloaded", { version: stagedVersion, notes: stagedNotes });
      return;
    }
    if (!foundVersion) {
      // Nothing discovered yet (e.g. UI raced the first check). Discover
      // first; the user can consent once "found" is surfaced.
      log.info("[update] download requested with nothing found — checking first");
      await safeCheck();
      return;
    }
    log.info(`[update] downloading ${foundVersion}`);
    downloading = true;
    downloadWasAutomatic = automatic;
    emit("downloading", { version: pendingVersion() });
    try {
      await autoUpdater.downloadUpdate();
    } catch (err) {
      downloading = false;
      emitError("download", err);
    }
  }

  // Force-exit failsafe — ONLY safe once the platform's installer has actually
  // taken over.
  //
  // Why this is event-gated and not a plain timer: on macOS the expensive work
  // happens INSIDE quitAndInstall(), not before it. Because
  // autoInstallOnAppQuit=false (deliberately -- see configureUpdater),
  // electron-updater withholds the downloaded zip from Squirrel until install
  // time, so quitAndInstall() returns immediately while Squirrel is still
  // fetching ~350MB from the loopback proxy, unpacking it and verifying its
  // signature. A 5s app.exit(0) lands in the middle of that: the staged app is
  // left on disk, ShipIt is never armed, and the user relaunches into the OLD
  // version with no error shown. Observed in the field on
  // 0.1.2-nightly.20260729t073648.
  //
  // The pre-migration client was safe with the same 5s constant because it drove
  // Squirrel directly: "update-downloaded" then meant Squirrel had ALREADY
  // staged the bundle, so quitAndInstall() was a millisecond-scale handoff. The
  // migration changed what that event means; the timer did not notice.
  //
  //
  // `before-quit-for-update` is emitted by Electron's native autoUpdater when
  // the install is genuinely armed and the app is being torn down for it -- the
  // only signal that proves the handoff happened. Until it fires, exiting can
  // only destroy the update. On darwin the failsafe therefore stays DISARMED
  // and Squirrel quits the app itself; the original hazard it guarded (a
  // renderer beforeunload or lingering child blocking the quit, letting ShipIt
  // abort with "App Still Running Error" Code=-9) is handled by exiting only
  // AFTER that event.
  function forceExitFailsafe(reason) {
    const arm = () => {
      const t = setTimeout(() => {
        log.error(`[update] still alive ${FORCE_EXIT_AFTER_MS}ms after the installer took over (${reason}) — forcing exit so the swap can proceed`);
        try { app.exit(0); } catch { process.exit(0); }
      }, FORCE_EXIT_AFTER_MS);
      if (typeof t.unref === "function") t.unref();
    };

    // The native updater is the one that emits this; electron-updater's
    // BaseUpdater re-emits it for the platforms it installs itself.
    const native = nativeAutoUpdater;
    if (native && typeof native.once === "function") {
      native.once("before-quit-for-update", () => {
        log.info(`[update] installer took over (${reason}) — arming the exit failsafe`);
        arm();
      });
      return;
    }
    // No native updater surface to listen on (tests, unexpected platform):
    // fall back to the timer rather than losing the guarantee entirely.
    arm();
  }

  // isForceRunAfter=true so the user lands back in the app after the swap.
  //
  // Windows deliberately uses isSilent=false. The assisted NSIS installer has
  // update-only hooks in build/installer.nsh that skip every decision page,
  // leave the native extraction progress visible, then relaunch and close on
  // completion. Passing /S hid that only useful feedback for several minutes,
  // making a healthy update look exactly like a crash. The installer also
  // converts /S back to this visible update mode for clients released before
  // this change, so the first upgrade into the fix is covered too.
  function notifyWindowsInstallHandoff() {
    if (osPlatform !== "win32") return;
    try {
      new Notification({
        title: "Installing Kiro Crew update",
        // Timing and automatic relaunch stay on the installer window that they
        // explain. The toast carries only the unique recovery instruction.
        body: "If Kiro Crew doesn’t reopen after the installer finishes, open it from the Start menu.",
      }).show();
    } catch { /* notifications optional */ }
  }

  function quitAndInstall() {
    notifyWindowsInstallHandoff();
    autoUpdater.quitAndInstall(false, true);
  }

  async function applyUpdateAndRestart() {
    if (installing) return;
    // REQUIRE a staged update. Without this guard an install() dispatched
    // before the download finished reaches MacUpdater.quitAndInstall()'s
    // squirrelDownloadedUpdate === false branch, which does NOT install --
    // it registers a listener and waits for Squirrel to fetch the update from
    // the loopback proxy. forceExitFailsafe would then kill the process 5s
    // later, mid-fetch, and the app dies without swapping or relaunching.
    // Once a stage exists, Squirrel has already consumed the zip and
    // quitAndInstall proceeds immediately, so the failsafe is safe to arm.
    if (!updateReady) {
      log.info("[update] install requested with nothing staged — ignoring");
      emit(foundVersion ? "found" : "not-available", foundVersion ? { version: foundVersion } : {});
      return;
    }
    installing = true;
    // Tell the renderer the install is UNDERWAY before anything goes silent:
    // the gateway is about to be stopped on purpose, and without this state
    // the dashboard renders the stoppage as an outage (offline pill, failed
    // requests) while the swap is still staging. On a failed handoff the
    // 'error' emit (phase "install") replaces this state, which is what
    // clears the renderer's installing overlay.
    emit("installing", { version: stagedVersion });
    // BEFORE stopGateway, or the watchdog can win the race and respawn the
    // gateway into the middle of the bundle swap.
    try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
    // STRICT ORDER: stop the gateway and await its exit, THEN quitAndInstall.
    // A live gateway child during the bundle swap can leave a half-replaced app.
    log.info("[update] stopping gateway before install");
    try {
      await stopGateway();
    } catch (err) {
      log.error("[update] gateway stop errored (continuing to install)", err);
    }
    // An install-phase failure can land while the gateway stops: the error
    // handler classifies it (installing outranks checking there), resets
    // `installing`, and runs the host recovery. This dispatch is already
    // dead — proceeding would install on a failure the user was just told
    // about, and aborting would run the recovery a second time.
    if (!installing) {
      log.info("[update] install failed while the gateway stopped — dispatch abandoned");
      return;
    }
    // Re-check the stage AFTER the await: a feed response already in flight
    // when the user clicked install can report a retraction or a newer build
    // while the gateway stops, and the update-available / update-not-available
    // handlers then discard the stage. Installing those bytes anyway would
    // ship a build the feed has withdrawn or superseded. A check STILL in
    // flight is the same hazard one step earlier: its response can invalidate
    // the stage the moment after this dispatch commits, and an error event it
    // produces during the bundle swap would be misattributed to the install
    // (see the phase derivation in the error handler). Aborting on `checking`
    // makes the dispatch itself the serialization point between checks and
    // installs: no check outcome — result or failure — can land past
    // quitAndInstall.
    if (!updateReady || checking) {
      log.info(
        !updateReady
          ? "[update] stage invalidated while the gateway stopped — aborting install and restoring"
          : "[update] check still in flight after the gateway stopped — aborting install and restoring",
      );
      installing = false;
      try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
      // Use the install-error renderer contract, NOT a bare found/not-available:
      // the user just clicked Install Update & Restart App and is watching an install
      // surface -- a silent state swap reads as an unexplained cancel. The
      // error/install shape has an existing renderer contract (the About
      // card, and the in-place overlay failure state) that says the install
      // did not proceed and offers the way forward.
      emit("error", {
        phase: "install",
        code: !updateReady ? "stage-invalidated" : "check-in-flight",
        message: !updateReady
          ? "the staged update was withdrawn or superseded before the install could run"
          : "a feed check was still in flight when the install was ready to run",
        ...(foundVersion ? { version: foundVersion } : {}),
      });
      return;
    }
    app.removeListener("before-quit", deferredInstallOnQuit);
    log.info("[update] gateway down — quitAndInstall");
    quitAndInstall();
    forceExitFailsafe("manual install");
  }

  // If the user chose "Later", install on the natural quit. This is OUR
  // implementation rather than autoInstallOnAppQuit=true precisely because the
  // gateway must be stopped first; before-quit can't await async work, so
  // preventDefault, stop the gateway, then quitAndInstall.
  function deferredInstallOnQuit(event) {
    if (quitHandled || !updateReady) return;
    // The opt-out has to govern the update the user opted out BECAUSE OF.
    // Without this, the nudge says "downloading, will install on your next
    // quit", the user follows it to the toggle and switches it off, and the
    // stage lands anyway — the one outcome the toggle promises will not happen.
    // Only an AUTOMATIC stage is dropped: one the user downloaded on purpose
    // stays armed, because the preference is not what put it there.
    //
    // The bytes are kept either way. This disarms the install, it does not
    // discard the stage, so an explicit Install still applies it immediately
    // with nothing to re-download.
    if (stagedWasAutomatic) {
      let stillAuto = false;
      try {
        stillAuto = !!getAutoDownloadPreference();
      } catch (err) {
        // Unreadable preference: treat as opted OUT here. This is the same
        // fail-toward-consent direction as the discovery path, and on this path
        // it is the one that cannot surprise anyone -- the app quits as asked
        // and the stage is still there to install later.
        log.error("[update] getAutoDownloadPreference threw on quit — not installing", err);
      }
      if (!stillAuto) {
        log.info(`[update] auto-download off — leaving ${stagedVersion} staged instead of `
          + "installing on quit");
        return;
      }
    }
    quitHandled = true;
    event.preventDefault();
    (async () => {
      // Same signal as the manual path: the window can stay visible for
      // several seconds while the gateway stops and the installer stages the
      // bundle, and the renderer must not read that silence as an outage.
      emit("installing", { version: stagedVersion });
      // No onInstallDispatched here: this handler only runs from before-quit,
      // where main.js has already set isQuitting -- the watchdog is covered.
      log.info("[update] deferred install on quit");
      try { await stopGateway(); } catch (err) { log.error("[update] stop on quit errored", err); }
      // Same stage re-check as the manual path: a feed response in flight at
      // quit time can invalidate the stage while the gateway stops. The user
      // asked to QUIT, so skip the install and let the quit proceed. What
      // makes the re-entry safe is the LISTENER state, not `quitHandled`: a
      // retraction handler resets `quitHandled = false` and removes this
      // listener, and it was registered with app.once so it has already been
      // consumed -- either way no live before-quit hook re-prevents the quit,
      // so app.quit() exits normally without installing the withdrawn build.
      if (!updateReady) {
        log.info("[update] stage invalidated during quit — quitting without installing");
        // The user was told the update would finish on quit; explain why it
        // did not, or the still-old version at next launch reads as a failure.
        try {
          new Notification({
            title: "Update canceled",
            body: "The staged update was withdrawn or superseded, so it was not installed. You\u2019ll be offered the latest version next launch.",
          }).show();
        } catch { /* notifications optional */ }
        app.quit();
        return;
      }
      quitAndInstall();
      forceExitFailsafe("deferred install on quit");
    })();
  }

  async function promptInstall(versionName, notes) {
    const handoffDetail = osPlatform === "win32"
      ? "Installing can take several minutes. Kiro Crew will close, show Windows installation progress, and reopen automatically."
      : "Installing can take several minutes. Kiro Crew will close and reopen automatically when the update is complete.";
    const { response } = await dialog.showMessageBox({
      type: "info",
      buttons: ["Install Update & Restart App", "Later"],
      defaultId: 0,
      cancelId: 1,
      title: "Kiro Crew update ready",
      message: `Kiro Crew ${versionName || ""} is ready to install.`.trim(),
      detail:
        (notes || "").slice(0, 500) +
        `\n\n${handoffDetail}`,
    });
    if (response === 0) {
      await applyUpdateAndRestart();
    } else {
      app.once("before-quit", deferredInstallOnQuit);
      try {
        new Notification({
          title: "Update deferred",
          body: "Kiro Crew will finish updating the next time you quit.",
        }).show();
      } catch { /* notifications optional */ }
    }
  }

  /** releaseNotes is string | {version,note}[] | null depending on the feed. */
  function notesFrom(info) {
    const n = info && info.releaseNotes;
    if (typeof n === "string") return n;
    if (Array.isArray(n)) return n.map((e) => (e && e.note) || "").filter(Boolean).join("\n\n");
    return "";
  }

  autoUpdater.on("error", (err) => {
    // The library funnels every failure through one event, so derive the phase
    // from the operation actually in flight. Read the flags BEFORE clearing
    // `downloading`, or a mid-download failure would be reported as a check
    // failure. `installing` must outrank `checking`: once an install is
    // dispatched the gateway is stopped ON PURPOSE, and a genuine installer
    // failure (observed live in the OTA lane: a Squirrel signature rejection)
    // that arrives while a check happens to be in flight would otherwise be
    // labelled "check" — onInstallFailed never fires, nothing restores the
    // stopped gateway, and the app survives with a dead dashboard. The
    // converse misattribution is the recoverable one: a straddling check's
    // feed error killing the install runs the same onInstallFailed recovery
    // the post-stopGateway abort would run anyway — and that abort refuses to
    // reach quitAndInstall while `checking` is true, so no check outcome can
    // fire recovery in the middle of an actual bundle swap. The
    // `downloading`-before-`installing` precedence is long-standing behavior,
    // preserved as-is.
    const phase = downloading ? "download" : installing ? "install" : "check";
    downloading = false;
    if (phase === "install") {
      // The dispatch is over: allow a retry (updateReady is still true -- the
      // zip is still staged) and tell the host to bring the gateway back.
      // Observed live in the OTA lane: a Squirrel signature rejection lands
      // here; without recovery the app survives with a dead dashboard.
      installing = false;
      try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
    }
    emitError(phase, err);
  });
  autoUpdater.on("checking-for-update", () => { log.info("[update] checking…"); emit("checking"); });
  autoUpdater.on("update-not-available", () => {
    downloading = false;
    foundVersion = null;
    // The feed's gate is DIFFERENCE-based (allowDowngrade=true), so "not
    // available" means the followed lane publishes exactly the running version:
    // record that, which is what makes the lane pair a definite not-ahead
    // instead of an unknown for the whole up-to-date population.
    recordLaneVersion(app.getVersion(), feedChannel);
    // Clear the STAGED state too, not just the found state. The feed reporting
    // "no update" while something is staged is exactly the retraction path
    // (a feed repointed to the running version) and the channel-switch-back
    // path -- and a stage left armed here would still install the withdrawn or
    // wrong-channel build on the next quit, because deferredInstallOnQuit only
    // checks updateReady. Disarm the quit hook as well or the listener
    // survives to fire against a stage we just invalidated.
    if (updateReady) {
      log.info(`[update] feed reports up to date -- discarding staged ${stagedVersion}`);
    }
    updateReady = false;
    stagedVersion = null;
    stagedNotes = "";
    quitHandled = false;
    app.removeListener("before-quit", deferredInstallOnQuit);
    log.info("[update] up to date");
    emit("not-available");
  });
  // DISCOVERY, before any bytes move. electron-updater's autoDownload stays
  // false so it never fetches inside checkForUpdates; whether a download
  // follows is OUR decision, made here from the preference, so the automatic
  // and the consent paths share one guarded entry point (startDownload).
  autoUpdater.on("update-available", (info) => {
    foundVersion = (info && info.version) || null;
    // What the followed lane publishes, recorded BEFORE the direction gate
    // below can null `foundVersion` out. The suppressed case is precisely the
    // one the display layer needs it for: an insider build whose preference was
    // flipped to stable reaches here with the stable lane's OLDER release, is
    // (correctly) not auto-offered, and must still be able to say "stable
    // publishes 0.4.1; you are running bytes it never shipped" instead of
    // folding its version to a stable release that does not exist.
    recordLaneVersion(foundVersion, feedChannel);
    // Direction gate — the fix for the "update to an OLDER version" nag.
    // electron-updater fires this for ANY feed version that DIFFERS from the
    // running one, because allowDowngrade=true — so on a build running ahead of
    // its channel's published latest it reports a DOWNGRADE as available. When
    // this is a same-channel version that is not newer, suppress the automatic
    // path entirely: discard any stage armed for it, report up to date, and do
    // NOT download or nag. A deliberate channel switch (followed !== default
    // lane) is exempt, and explicit user downloads are unaffected.
    if (
      foundVersion &&
      !shouldAutoOffer({
        candidate: foundVersion,
        current: app.getVersion(),
        // The channel THIS candidate's feed was configured for, captured at
        // check time (feedChannel), NOT a live currentChannel() read. If the
        // preference flipped while this check was in flight, a live read would
        // pair the new channel with the OLD feed's candidate and wrongly treat
        // a stale-feed downgrade as a deliberate switch. Falls back to a live
        // read only before the first configureFeed() has run.
        followedChannel: feedChannel || currentChannel(),
        // The lane this build follows with NO preference. Folds a promoted
        // stable build's insider-stamped bytes back to stable, so only an
        // explicit preference that MOVES the install off its default lane reads
        // as a deliberate channel switch (see shouldAutoOffer).
        defaultChannel: resolveChannel(channelForVersion(app.getVersion()), ""),
      })
    ) {
      log.info(
        `[update] feed offers ${foundVersion} but running ${app.getVersion()} is not older `
          + "on the same channel — treating as up to date (suppressing downgrade nag)",
      );
      if (updateReady || stagedVersion) {
        // A downgrade staged before this guard existed (or by a race) must not
        // survive to install on the next quit.
        updateReady = false;
        stagedVersion = null;
        stagedNotes = "";
        quitHandled = false;
        app.removeListener("before-quit", deferredInstallOnQuit);
      }
      foundVersion = null;
      emit("not-available");
      return;
    }
    // A stage is only useful if it is still the latest thing on the feed.
    // Because the RUNNING version never changes mid-session, the updater
    // reports "available" for the staged version too — so the comparison
    // below is what separates the two cases.
    if (updateReady && stagedVersion) {
      if (foundVersion === stagedVersion) {
        log.info(`[update] ${stagedVersion} already downloaded — awaiting install`);
        emit("downloaded", { version: stagedVersion, notes: stagedNotes });
        return;
      }
      // Superseded: drop the stale stage so the next download takes the NEWEST
      // build rather than installing an already-old one.
      log.info(`[update] staged ${stagedVersion} superseded by ${foundVersion} — discarding stage`);
      updateReady = false;
      stagedVersion = null;
      stagedNotes = "";
      app.removeListener("before-quit", deferredInstallOnQuit);
    }
    let autoDownload = false;
    try {
      autoDownload = !!getAutoDownloadPreference();
    } catch (err) {
      // A throwing preference reader must not cost the user the discovery
      // nudge, and it must not be read as consent either — fall back to the
      // consent path, which is the safe half.
      log.error("[update] getAutoDownloadPreference threw — treating as off", err);
    }
    log.info(`[update] found ${foundVersion} (running ${app.getVersion()}) — `
      + (autoDownload ? "auto-downloading" : "awaiting user consent"));
    // Nudge hook: main.js shows a native notification (deduped there, once per
    // version). Its copy differs by mode, so pass the mode rather than letting
    // main.js re-read the preference and risk disagreeing with this decision.
    if (typeof notifyUpdateFound === "function") {
      try { notifyUpdateFound(foundVersion, { autoDownload }); } catch (err) { log.error("[update] notifyUpdateFound threw", err); }
    }
    emit("found", {
      version: foundVersion,
      notes: notesFrom(info),
      pubDate: (info && info.releaseDate) || "",
    });
    // AFTER the "found" emit: the renderer must see the version it is about to
    // download, and startDownload() emits "downloading" over the top of it.
    // Fire-and-forget — startDownload owns its own error reporting, and this
    // handler is a synchronous event listener that cannot await.
    if (autoDownload) void startDownload({ automatic: true });
  });
  autoUpdater.on("download-progress", (p) => {
    // New capability vs. the hand-rolled updater: real progress, so the card
    // can show a percentage instead of an indeterminate "downloading".
    emit("downloading", {
      version: pendingVersion(),
      percent: p && typeof p.percent === "number" ? p.percent : undefined,
      bytesPerSecond: p && p.bytesPerSecond,
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    updateReady = true;
    downloading = false;
    stagedVersion = (info && info.version) || null;
    stagedNotes = notesFrom(info);
    stagedWasAutomatic = downloadWasAutomatic;
    log.info(`[update] downloaded ${stagedVersion} — ${uiDriven ? "notifying UI" : "prompting"}`);
    emit("downloaded", { version: stagedVersion || app.getVersion(), notes: stagedNotes });
    if (uiDriven) {
      // In-app UI owns the prompt. Still install on a natural quit if the user
      // dismisses the modal with "Later" (mirrors the native dialog's deferral).
      app.once("before-quit", deferredInstallOnQuit);
    } else {
      promptInstall(stagedVersion, stagedNotes);
    }
  });

  configureFeed();
  const launchTimer = setTimeout(safeCheck, launchCheckDelayMs);
  // The poll must keep consulting the feed even while an update is STAGED
  // (see the note in safeCheck). Gating it on !updateReady would pin a
  // long-running session to its stale stage whenever a newer version ships
  // mid-session -- the supersede path in the update-available handler is only
  // reachable if some check actually runs. safeCheck() already owns the
  // staged case: re-surface when the stage is still latest, discard and
  // re-find when it is superseded.
  //
  // INSTALL ACTIVITY is the one state the poll must still skip, and there are
  // exactly two install entry points to cover: `installing` (the manual
  // Restart & Update dispatch) and `quitHandled` (the deferred install on a
  // natural quit, which never sets `installing`). In either window the
  // gateway is being stopped on purpose and the process is about to hand off
  // to the platform installer -- a check there is useless at best, and at
  // worst its outcome (an error event, or a retraction clearing the stage
  // under a dispatch that already passed its guard) races the handoff.
  // Staged-but-idle and installing are different states; only the latter is
  // unsafe to probe.
  const pollTimer = setInterval(() => { if (!installing && !quitHandled) safeCheck(); }, checkIntervalMs);
  // Timers must never hold the process open (Electron quit, tests).
  if (typeof launchTimer.unref === "function") launchTimer.unref();
  if (typeof pollTimer.unref === "function") pollTimer.unref();

  // Renderer-callable triggers (wired to ipcMain in main.js). Background
  // timers only ever DISCOVER (safeCheck emits "found") — downloading
  // requires the explicit download() consent call.
  return {
    check: () => safeCheck(),
    download: () => startDownload(),
    install: () => applyUpdateAndRestart(),
    getInfo,
    isReady: () => updateReady,
  };
}

module.exports = { createFeedLane };
