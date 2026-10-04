// Every downloaded update is installed only after the gateway has stopped: see
// the configureUpdater note in auto-update.js for why electron-updater's own
// install-on-quit stays off on every platform.
const FORCE_EXIT_AFTER_MS = 5 * 1000; // failsafe: guarantee exit after quitAndInstall
// How long the pre-install freshness check (verifyStageIsLatest) may take
// before the install proceeds on the stage it already has. Bounded on BOTH
// install paths: one of them runs inside before-quit, where a feed that hangs
// must not hold the app open, and on the other a click must not sit dead
// waiting on a socket. Bytes the user already downloaded still install when the
// network is gone.
const FRESHNESS_CHECK_TIMEOUT_MS = 8 * 1000;

/**
 * Race `promise` against FRESHNESS_CHECK_TIMEOUT_MS: resolves to its value, or to
 * "timeout" once the bound elapses. The timer never holds the process open and is
 * cleared either way. Only the WAIT is bounded; the promise keeps running.
 */
async function withinFreshnessBound(promise) {
  let timer = null;
  const bounded = new Promise((resolve) => {
    timer = setTimeout(() => resolve("timeout"), FRESHNESS_CHECK_TIMEOUT_MS);
    if (timer && typeof timer.unref === "function") timer.unref();
  });
  try {
    return await Promise.race([promise, bounded]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Native notices are optional: a platform without them must not fail the path. */
function showNotice(Notification, title, body) {
  try { new Notification({ title, body }).show(); } catch { /* notifications optional */ }
}

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
  // The install claiming `installing` was dispatched from the quit handler, not
  // from a click. Read only on the failure path, where the two want opposite
  // things: a click's install can be recovered from in place (bring the gateway
  // back, leave the app running, let the user retry), but this one has a quit
  // waiting behind it that already stopped the gateway and already got its
  // preventDefault. Recovery here would leave the app alive with a dead
  // dashboard — see reportUpdaterFailure.
  let deferredInstalling = false;
  let quitHandled = false;
  let checking = false;
  // Resolves when the check that owns `checking` settles; null whenever no check
  // is in flight.
  //
  // Held so a caller that finds a check ALREADY running can wait for its answer.
  // Without it the only two options are both wrong for the freshness gates:
  // re-entering is refused outright (safeCheck's first line), and proceeding
  // reads `foundVersion`/`stagedVersion` while the running check is in the middle
  // of replacing them — which is how the download gate could spend the ~350MB it
  // exists to protect on the very stale build it was checking for.
  let checkInFlight = null;
  let releaseCheckInFlight = null;
  // A pre-install freshness check is in flight (see verifyStageIsLatest). Kept
  // apart from `checking` because it doubles as the install path's re-entrancy
  // guard: the verify awaits the feed BEFORE `installing` is set, so without it
  // two fast clicks would both reach the dispatch.
  let verifyingStage = false;
  // The quit path's own freshness gate is in flight. The quit itself is already
  // being handled, so a second Cmd+Q during the wait is held rather than obeyed.
  let quitPathVerifying = false;

  //: A quit that arrived while the MANUAL path owned the install, prevented rather
  //: than obeyed. That path has multi-second awaits (the freshness verify, then
  //: stopGateway) before it reaches quitAndInstall, and simply standing down inside
  //: them lets Electron exit first -- the user asked to install and restart and gets
  //: a plain quit with the stage uninstalled. So the quit is held and replayed by
  //: `honorDeferredQuit` once the path either installs (which quits anyway) or aborts.
  let quitDeferredForInstall = false;
  // Suppress the automatic download of a newer build for the CURRENT verify.
  // Set only by the quit-time caller: discovering a newer release seconds
  // before the process exits must not start a ~350MB fetch the exit will kill.
  let verifyQuiet = false;
  // A freshness check whose bounded WAIT expired while its request stayed in
  // flight (see verifyStageIsLatest). The fail-open contract sends that caller
  // straight on to install, so the abandoned request's outcome can now land
  // past the install dispatch -- precisely what the post-stopGateway `checking`
  // abort exists to prevent. That abort cannot be the answer here (aborting is
  // what fail-open refuses to do), so the late outcome is made INERT instead:
  // the handlers below decline to touch the stage, the install, or a download
  // for it. Cleared when the request settles, or when an install gate joins
  // the request and needs its answer (see verifyStageIsLatest).
  let abandonedCheck = false;
  // The error an abandoned request REJECTED with, captured by its own rejection
  // continuation. This, not the timing of the flag above, is what identifies an
  // error as the abandoned check's own.
  //
  // Why identity and not "the flag cleared by now": the flag clears on either
  // settle mode, and it clears in a microtask, so reading it one macrotask later
  // is a claim about electron-updater's emit/reject ORDER — undocumented, mocked
  // in every test, and wrong in two reachable races even today. A genuine
  // installer error that arrives just before the abandoned request's verdict
  // lands reads as "the flag cleared, so that was the check's" and is swallowed
  // with the gateway stopped; a second, genuine error arriving inside the same
  // tick as the first is swallowed the same way. A rejection object is evidence
  // that survives reordering: whether the library emits before it rejects
  // (today), after, or a tick later, the abandoned request's own failure is the
  // one whose object this holds. Null until it rejects, and null again once an
  // `error` event has been matched against it -- one rejection explains exactly
  // one event.
  let abandonedError = null;
  // The error the CURRENT ordinary check rejected with, claimed by `safeCheck`'s own
  // catch. The sibling of `abandonedError`, for the check nobody gave up on.
  //
  // It exists because the guard against this hazard is one-directional. `safeCheck`
  // refuses to START a check while `installing` (see its early return, which names
  // this exact consequence), but nothing stops an install from starting while a check
  // is already in flight — and in that order the check's own feed failure reaches the
  // shared `error` event while `installing` outranks `checking`, so it is reported as
  // the install's and fires the host's gateway recovery in the middle of the
  // dispatch's `stopGateway()`.
  //
  // Cleared when the NEXT check is claimed, never in `checkSettled()`: the deferred
  // comparison that reads it runs one macrotask AFTER the settle, so clearing on
  // settle would blind the very comparison it exists for.
  let inFlightCheckError = null;
  /**
   * Release the state a check owns FOR ITS WHOLE LIFETIME. Called only by the
   * check's owner (safeCheck's finally, or verifyStageIsLatest once the request
   * settles), never from an event handler: electron-updater emits a check's
   * outcome before it releases its own in-flight promise, so a waiter woken
   * inside the emit would be handed the same request back. Idempotent.
   */
  function checkSettled() {
    checking = false;
    verifyQuiet = false;
    abandonedCheck = false;
    // Wake anyone waiting on this check BEFORE returning, and drop the promise so
    // a later waiter cannot latch onto an already-resolved one. Nulled first so
    // the callback cannot observe a settled check that still looks in flight.
    const release = releaseCheckInFlight;
    checkInFlight = null;
    releaseCheckInFlight = null;
    if (release) release();
  }
  /**
   * Give up on the check in flight: its late outcome becomes inert (see
   * `abandonedCheck`), and its rejection is captured afresh for the identity
   * match (see `abandonedError`).
   */
  function abandonCheck() {
    abandonedCheck = true;
    abandonedError = null;
  }
  /**
   * A gate joined an abandoned check and needs its answer: the outcome counts
   * again, and its failure is no longer one the identity match should spend.
   */
  function liftAbandonment() {
    abandonedCheck = false;
    abandonedError = null;
  }
  /**
   * Claim `checking` and publish the promise that resolves when it settles.
   *
   * Every `checking = true` goes through here: the promise is only useful to a
   * waiter if it exists for the WHOLE window the flag does, and a flag set
   * directly would be a window with nothing to wait on.
   */
  function claimCheck() {
    checking = true;
    // The previous check's claimed error must not answer for this one.
    inFlightCheckError = null;
    checkInFlight = new Promise((resolve) => { releaseCheckInFlight = resolve; });
  }
  // A manual download's own pre-download check is in flight. The download path's
  // re-entrancy guard, for the same reason `verifyingStage` is the install
  // path's: the gate awaits the feed BEFORE `downloading` is set.
  let preparingDownload = false;
  // The channel the LAST configureFeed() pointed the updater at. Captured at
  // check time because the update-available handler's direction gate must
  // compare the candidate against the channel its FEED was configured for, not
  // against a live currentChannel() read: the preference can flip mid-flight
  // (an in-flight stable check, then the user picks insider), and re-reading it
  // in the handler would treat a stable-feed downgrade as a deliberate insider
  // switch and stage it. Null until the first configureFeed().
  let feedChannel = null;
  /**
   * A freshness gate is waiting on the check in flight. Its `checking`, its
   * failure, and its restatement of the offer stay internal: the gate's own
   * next state (`downloading`, `installing`, or the handlers' verdict) is what
   * the renderer needs, and each of those interim states would unmount or
   * replace the surface the user just clicked.
   */
  function gateOwnsCheck() {
    return preparingDownload || verifyingStage;
  }

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
    claimCheck();
    try {
      configureFeed(); // re-read flavor/channel each check
      // Not for a freshness gate's own re-check: `checking` is what the renderer
      // renders as "the user asked whether there is an update", and it UNMOUNTS
      // the update card while it is true (showUpdateCard in AboutPanel). Emitted
      // from inside a Download click, the user's own click makes the offer and
      // its button vanish for a feed round trip and then come back — read as "my
      // click dismissed the update". The log line still records every check.
      if (!preparingDownload) emit("checking");
      await autoUpdater.checkForUpdates();
    } catch (err) {
      // Claim this failure as THIS check's before reporting it. electron-updater
      // emits `error` and then rejects with the same object
      // (`AppUpdater.checkForUpdates`: `this.emit("error", e, …); throw e`), so the
      // shared event handler has already seen it and deferred its decision by one
      // macrotask precisely so this line can run first. The claim is what lets that
      // handler tell a check's own feed failure from the installer's.
      inFlightCheckError = err;
      // A check the freshness gate joined and then abandoned: its own failure
      // must be recognisable to the `error` handler, as an own abandoned check's
      // is through its rejection continuation.
      // Not reported to the renderer either, exactly as an own abandoned check's
      // failure is not: the install it was abandoned for is running with the
      // gateway stopped, and an `error` state would replace the installing overlay
      // with a dead dashboard mid-swap.
      if (abandonedCheck) {
        abandonedError = err;
        log.info("[update] abandoned check failed after the install was dispatched — not reporting", err);
      } else if (!gateOwnsCheck()) {
        // A gate's check failure is internal (see the `error` handler).
        emitError("check", err);
      }
    } finally {
      checkSettled();
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
   *
   * A CLICK also re-checks first (see the freshness gate below), so the bytes
   * that get spent are the newest ones. The automatic caller does not: it is
   * invoked from inside a check, so its discovery is already current.
   */
  async function startDownload({ automatic = false } = {}) {
    if (downloading) { emit("downloading", { version: pendingVersion() }); return; }
    // A click's own re-check is still in flight (below). Standing down loses
    // nothing — that call downloads whatever the check surfaces — and covers
    // both re-entrant callers: a second click, and the automatic caller the
    // check's own update-available handler fires, which would otherwise fetch
    // the version the click is in the middle of replacing.
    if (preparingDownload) return;
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
    // FRESHNESS GATE — never spend a ~350MB transfer on a build the feed has
    // already moved past. With auto-download OFF the "found" card waits for a
    // click that can come hours later; downloading what it says would fetch the
    // stale build, and the pre-install gate would then correctly refuse it and
    // fetch the newer one — two transfers to land one update, which is the waste
    // this gate exists to prevent. EVERY manual click re-confirms: one bounded
    // round trip is cheap beside the transfer it guards, and skipping it on a
    // recent check would trust an answer that a channel switch may have
    // invalidated in the meantime.
    if (!automatic) {
      const asked = foundVersion;
      log.info(`[update] confirming ${asked} is still the newest before downloading`);
      preparingDownload = true;
      // The click is answered at once, before the round trip: `update:download`
      // resolves without awaiting this call, and the gate's own `checking` and
      // `found` are suppressed (see the handlers), so without this emit the
      // card would sit on an enabled Download button for up to the bound. Every
      // return below is followed by a state the check or this call emits, so
      // this interim one never stands as the last word.
      emit("downloading", { version: asked });
      // The check this gate waits on (set below). Another check owning the feed
      // after the wait is one this gate did not ask.
      let ownCheck = null;
      try {
        // A check already in flight is WAITED FOR, not re-issued. safeCheck
        // refuses to re-enter one (`if (checking) return`), so calling it here
        // would be a silent no-op and the gate would then re-read state the
        // running check is in the middle of replacing — concluding "still the
        // newest" from the very verdict it was sent to re-confirm and spending the
        // transfer on the stale build. Its answer is the one this gate needs, so
        // this waits for it.
        //
        // BOUNDED like the install gate, and fail-open the same way: a hung feed
        // must not hold the click. Past the bound the check is abandoned (its late
        // answer is inert, and its failure cannot be misread as this download's)
        // and the build the card offered downloads. A later gate that joins the
        // same check lifts the abandonment, this one included.
        if (checkInFlight) {
          log.info("[update] a check is already in flight — waiting for its answer");
          // A check an earlier click abandoned (a Retry after that click's
          // download failed): this gate reads its verdict from what the check's
          // handlers do, so the answer must reach them, or a retraction landing
          // inside this bound is ignored and the build the card offered downloads
          // anyway. Its failure must not stay recorded as an abandoned one either:
          // the `error` handler swallows a gate's check failure before the
          // identity match could spend it. Kept while an install runs (reachable
          // only with a versionless stage, which the return above lets through):
          // that install proceeded on the promise the abandonment keeps.
          if (!installing) liftAbandonment();
        }
        // safeCheck() claims its check before its first await, so either way
        // `checkInFlight` is now the one this gate is waiting on.
        const waitedOn = checkInFlight || safeCheck();
        ownCheck = checkInFlight;
        const waited = await withinFreshnessBound(waitedOn);
        if (waited === "timeout" && checking) {
          abandonCheck();
          log.info(`[update] the feed did not answer in ${FRESHNESS_CHECK_TIMEOUT_MS}ms — downloading ${asked}`);
        }
      } finally {
        preparingDownload = false;
      }
      // The check may have moved everything underneath this call. Each return
      // below still leaves the renderer on a state that replaces the interim
      // `downloading`: an automatic download reports its own progress, a newer
      // check owning the feed and the channel re-check emit `checking`, a stage
      // emits `downloaded` here, and a retraction or suppressed downgrade has
      // already emitted `not-available`.
      if (downloading) return; // an automatic download won the race
      if (checking && checkInFlight !== ownCheck) {
        // A NEWER check now owns the feed: the new-lane re-check an earlier
        // click queued behind the check this one joined, or one the user
        // started. It has re-pointed `feedChannel`, so the channel guard below
        // would pass while `foundVersion` still holds the old check's answer,
        // and electron-updater would download that check's build. Its handlers
        // report what it finds; `checking` replaces the interim `downloading`,
        // since the gate kept that check's own `checking` off the renderer.
        log.info("[update] a newer check owns the feed — letting it answer instead of downloading");
        if (!gateOwnsCheck()) emit("checking");
        return;
      }
      if (feedChannel !== currentChannel()) {
        // The preference moved during the wait: what the card offers came from a
        // lane this install no longer follows. Ask the new lane instead of
        // spending the transfer on the old one's build.
        log.info(`[update] channel changed before the download (${feedChannel} -> ${currentChannel()}) — re-checking instead`);
        // Behind the check that still holds `checking` (an abandoned one, past the
        // bound), or safeCheck would stand down and the new lane go unasked.
        // `checkInFlight` resolves only from the check's owner, after
        // electron-updater has released its own in-flight promise, so the
        // re-check issues a new request instead of being handed the old one.
        // `checking` now, while it waits: the old check can hold its socket for a
        // minute, and the interim `downloading` must not stand for that long.
        if (checkInFlight) {
          emit("checking");
          void checkInFlight.then(() => safeCheck());
        } else {
          void safeCheck();
        }
        return;
      }
      if (updateReady && stagedVersion) {
        emit("downloaded", { version: stagedVersion, notes: stagedNotes });
        return;
      }
      if (!foundVersion) {
        log.info(`[update] ${asked} is no longer offered — nothing to download`);
        return;
      }
      if (foundVersion !== asked) {
        log.info(`[update] ${asked} was superseded by ${foundVersion} — downloading the newer build`);
      }
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

  /**
   * Re-consult the feed and report whether the STAGED build is still the newest
   * thing the followed channel publishes. The last thing before any install.
   *
   * WHY an install needs this. Discovery downloads eagerly (auto-download is on
   * by default) and then WAITS — for a click, or for the next natural quit. The
   * feed keeps moving in that window, and the other supersede checks are driven
   * by the 4-hourly poll, so an install landing between two polls has no fresh
   * verdict of its own to act on. Without this call it applies the stale stage:
   * the app relaunches on an already-superseded build, the next poll finds the
   * newer one, and the whole ~350MB transfer runs a second time — two downloads
   * and two restarts to reach a version one download reaches. The
   * deferred-install-on-quit path needs it most, since nothing between "Later"
   * and the quit consults the feed otherwise.
   *
   * It drives an ORDINARY check rather than parsing the feed itself, so the
   * verdict comes from exactly the logic every other check goes through — the
   * direction gate, the retraction path in `update-not-available`, and the
   * supersede path in `update-available` that drops a stale stage. The answer
   * is therefore read back out of the state those handlers left behind, which
   * is also what keeps this from becoming a second, divergent copy of the
   * "is this worth installing" rule.
   *
   * A check ALREADY in flight is waited for rather than re-issued (safeCheck
   * refuses to re-enter one), bounded by the same timeout: its answer is the one
   * this gate needs, and returning "unknown" without waiting would leave that
   * check live at the dispatch, where the quit path then refuses to install.
   *
   * FAIL-OPEN by contract. "unknown" — feed unreachable, or no answer inside
   * `FRESHNESS_CHECK_TIMEOUT_MS` — means install what we have: bytes the user
   * already downloaded must not become uninstallable because the network went
   * away, so an unanswerable feed installs what is staged. A stage that carries
   * no version is "unknown" too, since there is nothing to compare. Only a
   * POSITIVE answer that the stage is gone stops an install.
   *
   * EVERY call asks the feed. There is no reuse of a recent answer: the saving was
   * one bounded round trip, and the state it needed (a settle timestamp plus a
   * forfeit flag to stop an abandoned check licensing the reuse) could still hand
   * back a verdict a channel switch had invalidated.
   *
   * @param {{quiet?:boolean}} [o] `quiet` suppresses the automatic download of a
   *   newer build (the quitting caller).
   * @returns {Promise<"latest"|"superseded"|"unknown">}
   */
  async function verifyStageIsLatest({ quiet = false } = {}) {
    if (!updateReady) return "superseded";
    if (!stagedVersion) return "unknown";
    const staged = stagedVersion;
    if (checking) {
      // A QUIET caller needs the quiet on the check already running: a newer
      // version in its answer would otherwise start a ~350MB automatic download
      // seconds before the process exits. Safe to set because checkSettled()
      // clears it, and the check that will call it is the one in flight.
      if (quiet) verifyQuiet = true;
      // Lift any abandonment of this check: no install is running when a gate
      // starts (both install paths refuse to start one while another runs), so
      // whoever abandoned it was the download gate, or an install that has since
      // failed, and neither still needs its answer inert. This gate reads its
      // verdict from what the check's handlers do to the stage, so the answer
      // must reach them, or a newer build or a retraction landing inside this
      // bound is ignored and the stale stage reads as "latest".
      liftAbandonment();
      verifyingStage = true;
      try {
        if (await withinFreshnessBound(checkInFlight) === "timeout") {
          // Abandon the joined check exactly as an own check is abandoned below:
          // its late outcome becomes inert, so the install this "unknown"
          // promises proceeds instead of being aborted at the post-stopGateway
          // `checking` guard after the gateway was already stopped for it.
          // safeCheck's catch records its rejection for the identity match.
          abandonCheck();
          log.info(`[update] the check in flight did not answer in ${FRESHNESS_CHECK_TIMEOUT_MS}ms — installing the staged ${staged}`);
          return "unknown";
        }
      } finally {
        verifyingStage = false;
      }
      // That check's own handlers have already applied its verdict to the stage.
      return stageVerdict(staged);
    }
    verifyingStage = true;
    verifyQuiet = quiet;
    claimCheck();
    try {
      configureFeed();
      const settled = autoUpdater.checkForUpdates();
      // The rejection is folded into the race rather than left to the catch
      // below: a check that fails AFTER the timeout already won would
      // otherwise be an unhandled rejection.
      const outcome = await withinFreshnessBound(settled.then(() => "checked", () => "failed"));
      if (outcome === "timeout") {
        // Only the WAIT ended; the request is still running. Its flags describe
        // that request, not this wait, so they stay held until it lands —
        // clearing `checking` here would let its outcome slip past the
        // post-stopGateway abort and fire the host's gateway recovery in the
        // middle of the bundle swap, and clearing `verifyQuiet` would let a
        // newer version it discovers start a ~350MB fetch as the app exits.
        // Marked abandoned so that outcome is inert (see abandonedCheck) rather
        // than aborting the install the fail-open contract just promised. The
        // rejection continuation KEEPS the error rather than just releasing the
        // flags: it is what lets the shared `error` handler tell this request's
        // own failure from an installer's, whichever order they arrive in.
        abandonCheck();
        settled.then(checkSettled, (err) => { abandonedError = err; checkSettled(); });
        log.info(`[update] freshness check did not answer in ${FRESHNESS_CHECK_TIMEOUT_MS}ms — installing the staged ${staged}`);
        return "unknown";
      }
      checkSettled();
      if (outcome !== "checked") {
        log.info(`[update] freshness check failed — installing the staged ${staged}`);
        return "unknown";
      }
    } catch (err) {
      // configureFeed() threw, so no request went out and no `error` event fired.
      // A feed we could not reach is not evidence that the stage is stale: the
      // failure is logged and folded into "unknown", and the renderer is not told,
      // as with every gate's own check failure (see the `error` handler).
      checkSettled();
      log.error(`[update] freshness check failed — installing the staged ${staged}`, err);
      return "unknown";
    } finally {
      verifyingStage = false;
    }
    return stageVerdict(staged);
  }

  /** Read a check's verdict back out of the stage its handlers left behind. */
  function stageVerdict(staged) {
    if (feedChannel !== currentChannel()) {
      // The channel preference moved while the check was in flight, so its answer
      // describes a lane this install no longer follows. Ask the new lane rather
      // than install on the old one's verdict; its own handlers then report what
      // it serves (safeCheck stands down on the quit path, where the next launch
      // asks instead).
      log.info(`[update] channel changed during the freshness check (${feedChannel} -> ${currentChannel()}) — not installing on the old lane's verdict`);
      void safeCheck();
      return "superseded";
    }
    return updateReady && stagedVersion === staged ? "latest" : "superseded";
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
    // `verifyingStage` as well as `installing`: the freshness gate below awaits
    // the feed before `installing` is set, so it is what makes a second click
    // during that window a no-op instead of a second dispatch.
    //
    // `quitHandled` is the SYMMETRIC half, and it is the same hazard read from the
    // other side. The quit-time install claims only `quitHandled` and then spends
    // its own multi-second await on the freshness gate and stopGateway before it
    // sets `installing` -- so during that window `installing` and `verifyingStage`
    // are both false, and a click arriving here would dispatch a SECOND
    // quitAndInstall() against the same install directory, mid-bundle-swap, which
    // is the half-replaced app this whole ordering exists to prevent. Standing down
    // loses nothing: that path is already installing this stage and is quitting.
    if (installing || verifyingStage || quitHandled) {
      log.info("[update] install click ignored: an install already owns this stage");
      return;
    }
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
    // From the freshness gate on, this call OWNS the dispatch, so every exit that
    // does not hand off to the installer replays a quit held while it ran (see
    // honorDeferredQuit). The finally states that once; the returns above it
    // are stand-downs, where another dispatch owns the stage and the parked quit.
    // The replay runs after each exit's own renderer emit, so the card is up
    // before the window goes.
    let dispatched = false;
    try {
      // FRESHNESS GATE — always install the NEWEST, never a stage the feed has
      // moved past (see verifyStageIsLatest). A "ready to install" card can sit
      // unclicked for days; installing it blind lands a superseded build and the
      // next check immediately re-downloads the newer one. Bounded, because this
      // runs inside the click: a feed that never answers must fall through to the
      // install the user asked for, not leave the button doing nothing.
      if (await verifyStageIsLatest() === "superseded") {
        // Deliberately no state emit: the check's own handlers have ALREADY told
        // the renderer what replaced this stage — "found"/"downloading" for a
        // newer release, "not-available" for a retraction — and an error card
        // stacked over a running download would contradict them.
        log.info("[update] install refused: the stage is no longer the newest build — pursuing the newest instead");
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
      //
      // An ABANDONED check is the one exception, and it is not a hole: the
      // freshness gate stopped waiting for it and promised this install would
      // proceed anyway, so aborting here would defeat the fail-open contract that
      // sent us past the gate. The gate abandons every check it waits out, so a
      // live check reaches this guard only when the gate did not wait at all (a
      // stage with no version to compare). Its outcome is neutralized at the handlers instead
      // (see abandonedCheck), which is what keeps the guarantee above true.
      if (!updateReady || (checking && !abandonedCheck)) {
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
      dispatched = true;
      forceExitFailsafe("manual install");
    } finally {
      if (!dispatched) honorDeferredQuit();
    }
  }

  // If the user chose "Later", install on the natural quit. This is OUR
  // implementation rather than autoInstallOnAppQuit=true precisely because the
  // gateway must be stopped first; before-quit can't await async work, so
  // preventDefault, stop the gateway, then quitAndInstall.
  /**
   * Replay a quit that `deferredInstallOnQuit` held while the manual path owned the
   * install. Called on every exit from that path that does NOT itself quit, so the
   * user's request is honored late instead of silently dropped -- an app that ignores
   * Cmd+Q is the failure this pairs with, and it is worse than a late quit because
   * nothing tells the user their request was discarded.
   *
   * Idempotent: the flag is cleared first, so a caller reached twice cannot stack two
   * quits, and a path that both aborts and later fails cannot double-quit.
   */
  function honorDeferredQuit() {
    if (!quitDeferredForInstall) return;
    quitDeferredForInstall = false;
    // Drop the listener the defer branch re-armed. The quit being replayed is the
    // user's plain quit, not a request to install: re-entering this handler on the way
    // out would find `installing` cleared by the abort and fall through to the
    // quit-time install, so an abort would end in the very dispatch it just refused.
    app.removeListener("before-quit", deferredInstallOnQuit);
    log.info("[update] the manual install path finished — honoring the quit it deferred");
    app.quit();
  }

  function deferredInstallOnQuit(event) {
    if (quitPathVerifying) {
      // A second Cmd+Q while the quit path's own freshness gate is asking the
      // feed. main.js has already begun stopping the gateway, and this quit is
      // already being handled: prevent it again, or Electron exits mid-check
      // without installing. Re-armed because the listener is registered once.
      event.preventDefault();
      app.once("before-quit", deferredInstallOnQuit);
      return;
    }
    if (quitHandled || !updateReady) return;
    // `installing` and `verifyingStage` stand for "the manual path already owns this
    // install". Standing down for them would land a SECOND quitAndInstall() against
    // the same install directory plus a second forceExitFailsafe -- but returning
    // WITHOUT preventing is not the way to avoid that, because that path has not
    // quit yet. It is mid-await (the freshness verify, then stopGateway), so an
    // unprevented quit lets Electron exit before quitAndInstall() runs: the user
    // asked to install and restart and gets a plain quit with the stage still
    // uninstalled. Hold the quit and let that path replay it via
    // `honorDeferredQuit` when it finishes -- on success quitAndInstall quits
    // anyway, and on every abort the parked quit is the user's own request being
    // honored late rather than dropped. No notifyInstallCanceled here, unlike the
    // two returns below: the install IS still proceeding.
    if (installing || verifyingStage) {
      log.info("[update] quit requested while the manual install path owns the install — deferring it");
      quitDeferredForInstall = true;
      // The app is now on its way out: a newer build the manual path's gate
      // finds must not start a ~350MB download the replayed quit will kill.
      if (verifyingStage) verifyQuiet = true;
      event.preventDefault();
      // RE-ARM, because this listener was registered with `app.once` and Electron has
      // already unregistered it by the time we get here. Every other exit from this
      // handler either quits or hands off, so being consumed is correct for them --
      // this is the one branch that returns with the app still running. Without the
      // re-arm a SECOND Cmd+Q (the natural reaction to an app that did not quit)
      // reaches only main.js's handler and Electron exits before quitAndInstall(),
      // which is the exact "plain quit with the stage uninstalled" outcome this
      // branch exists to prevent. Re-entry is safe: `quitDeferredForInstall` is
      // already latched, so a second pass through here just prevents again.
      app.once("before-quit", deferredInstallOnQuit);
      return;
    }
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
      // Same signal as the manual path, and FIRST: main.js has already begun
      // stopping the gateway, so for the gate's round trip below the dashboard
      // would otherwise read the stoppage as an outage. A refusal quits anyway.
      emit("installing", { version: stagedVersion });
      // FRESHNESS GATE, before anything is torn down. This is the path the
      // stale-install bug actually took: "Later" arms this handler, the feed
      // moves on, and the quit that follows installs the superseded build
      // because nothing between the two ever consults the feed — the poll is
      // 4-hourly and the quit does not wait for it. Bounded and fail-open (see
      // verifyStageIsLatest): a quit must not hang on the network, and an
      // offline quit still installs what was already downloaded. The listener
      // is re-armed for the wait so a second Cmd+Q is held, not obeyed.
      quitPathVerifying = true;
      app.once("before-quit", deferredInstallOnQuit);
      let freshness;
      try {
        freshness = await verifyStageIsLatest({ quiet: true });
      } finally {
        quitPathVerifying = false;
        app.removeListener("before-quit", deferredInstallOnQuit);
      }
      if (freshness === "superseded") {
        log.info("[update] staged build is no longer the newest — quitting without installing");
        // The user was told the update would finish on quit; explain why it did
        // not, or the still-old version at next launch reads as a failure.
        notifyInstallCanceled();
        app.quit();
        return;
      }
      // No onInstallDispatched here: this handler only runs from before-quit,
      // where main.js has already set isQuitting -- the watchdog is covered. Nor
      // is it needed for the failure path's sake: that path quits rather than
      // recovering (see reportUpdaterFailure), so there is no gateway to restore.
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
      // `checking` is the manual path's abort too (see applyUpdateAndRestart) and
      // it is here for the same reason, which the freshness gate did not remove:
      // the gate abandons any check it waits out, but it does not wait for one at
      // all when the stage carries no version (nothing to compare), so a poll
      // straddling the quit can still reach this line live in that case.
      // Its late failure would then arrive with `installing` claimed below and be
      // reported as the installer's — recovery for a feed error, mid-swap. Only a
      // check WE abandoned is exempt: that one the identity check in the `error`
      // handler can tell apart, and refusing for it would break the fail-open
      // promise the gate just made.
      if (!updateReady || (checking && !abandonedCheck)) {
        // The user was told the update would finish on quit; explain why it did
        // not, or the still-old version at next launch reads as a failure. The two
        // arms get DIFFERENT copy because they are different facts: an invalidated
        // stage really was withdrawn or superseded, while a straddling check leaves
        // the same build staged and re-offers it, so the retraction wording would be
        // contradicted by the next launch.
        if (!updateReady) {
          log.info("[update] stage invalidated — quitting without installing");
          notifyInstallCanceled();
        } else {
          log.info("[update] a check straddles the quit — quitting without installing");
          notifyInstallDeferredForCheck();
        }
        app.quit();
        return;
      }
      // Claim the install BEFORE dispatching it, so a genuine installer failure
      // on this path is REPORTED as one: the error handler derives the phase from
      // the flags, and without this a Squirrel rejection here reads as
      // `phase: "check"` — the renderer stays on the installing overlay emitted
      // above instead of the retryable install-failure card, a second dispatch is
      // not blocked mid-swap, and onInstallFailed is never even called. The
      // failsafe is no backstop either: it only arms on `before-quit-for-update`,
      // which a rejected installer never emits, so the app survives the failed
      // quit until the user notices and quits again.
      //
      // `deferredInstalling` alongside it, because the two install paths want
      // OPPOSITE things from a failure and only this flag can tell them apart:
      // the manual path recovers in place, this one finishes the quit it
      // preventDefault'ed. See reportUpdaterFailure.
      //
      // Set both here rather than beside the "installing" event so the freshness
      // gate above still awaits the feed with the flag clear (`installing`
      // outranks `checking`, so an early claim would misattribute that check's
      // own failure), and so neither quit-time refusal leaves it latched.
      installing = true;
      deferredInstalling = true;
      quitAndInstall();
      forceExitFailsafe("deferred install on quit");
    })();
  }

  /**
   * "The quit did not install after all." Both quit-time refusals share one
   * message because they are the same fact to the user and neither can claim
   * more than this: the freshness gate reports "superseded" for a build the feed
   * moved past AND for one it withdrew (update-not-available clears the stage
   * the same way), so copy that announced a newer version would be a wrong
   * statement of fact on the retraction half.
   */
  function notifyInstallCanceled() {
    showNotice(Notification, "Update canceled", "The staged update was withdrawn or superseded, so it was not installed. You\u2019ll be offered the latest version next launch.");
  }

  /**
   * "The quit tried to install and the installer refused." Separate copy from
   * notifyInstallCanceled because the two are different facts and that one's
   * reason -- withdrawn or superseded -- would be a false statement here: the
   * build was still the newest, the installer rejected it. The one thing both
   * promise is the same, and is what makes the failure survivable: the stage is
   * still on disk, so the offer comes back.
   */
  function notifyDeferredInstallFailed() {
    showNotice(Notification, "Update not installed", "The update could not be installed on quit. You\u2019ll be offered it again next launch.");
  }

  /**
   * "A check was still running, so the quit did not risk installing the wrong
   * build." Separate copy again, for the same reason: nothing was withdrawn or
   * superseded on this path, and the SAME version is re-offered next launch, so
   * notifyInstallCanceled's reason would be contradicted by what the user sees a
   * moment later -- the specific way an updater loses trust.
   */
  function notifyInstallDeferredForCheck() {
    showNotice(Notification, "Update not installed", "A version check was still running at quit, so the update was left for next launch rather than risk installing a superseded build.");
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
      showNotice(Notification, "Update deferred", "Kiro Crew will finish updating the next time you quit.");
    }
  }

  /** releaseNotes is string | {version,note}[] | null depending on the feed. */
  function notesFrom(info) {
    const n = info && info.releaseNotes;
    if (typeof n === "string") return n;
    if (Array.isArray(n)) return n.map((e) => (e && e.note) || "").filter(Boolean).join("\n\n");
    return "";
  }

  /**
   * Attribute a failure to the operation in flight, then report it.
   *
   * The library funnels every failure through one event, so derive the phase
   * from the operation actually in flight. Read the flags BEFORE clearing
   * `downloading`, or a mid-download failure would be reported as a check
   * failure. `installing` must outrank `checking`: once an install is dispatched
   * the gateway is stopped ON PURPOSE, and a genuine installer failure (observed
   * live in the OTA lane: a Squirrel signature rejection) that arrives while a
   * check happens to be in flight would otherwise be labelled "check" —
   * onInstallFailed never fires, nothing restores the stopped gateway, and the
   * app survives with a dead dashboard. The converse misattribution is the
   * recoverable one: a straddling check's feed error killing the install runs the
   * same onInstallFailed recovery the post-stopGateway abort would run anyway.
   * The `downloading`-before-`installing` precedence is long-standing behavior,
   * preserved as-is.
   */
  function failurePhase() {
    return downloading ? "download" : installing ? "install" : "check";
  }

  // `phase` is read when the `error` EVENT fires and passed in by the deferred
  // callers: one macrotask later the flags it derives from have moved (a failed
  // download has already cleared `downloading`), and re-deriving then would
  // relabel that download failure as a check failure.
  function reportUpdaterFailure(err, phase = failurePhase()) {
    downloading = false;
    if (phase === "install") {
      // The dispatch is over: allow a retry (updateReady is still true -- the
      // zip is still staged).
      installing = false;
      if (deferredInstalling) {
        // Dispatched from the quit handler, so recovery is the WRONG answer here
        // even though this is the same Squirrel rejection: that quit was
        // preventDefault'ed and is still waiting, the gateway is already stopped,
        // and `before-quit-for-update` never fires for a refused installer -- so
        // the failsafe never arms either. Bringing the gateway back would leave
        // the app running with a window the user asked to close, and the host's
        // recovery could not do it anyway (it bails while the app is quitting).
        // Finish the quit instead: the stage survives on disk and is offered
        // again next launch, which is what the notification promises.
        deferredInstalling = false;
        log.error("[update] deferred install on quit failed — quitting without installing", err);
        notifyDeferredInstallFailed();
        emitError(phase, err);
        app.quit();
        return;
      }
      // Tell the host to bring the gateway back. Observed live in the OTA lane: a
      // Squirrel signature rejection lands here; without recovery the app
      // survives with a dead dashboard.
      try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
      // Recovering in place is the right answer for a manual failure -- but if the
      // user pressed Cmd+Q while this install was running, that quit was held rather
      // than obeyed. Recovery restores the gateway; it must not also swallow the
      // request to leave. Emit the error first so the failure is on screen for the
      // brief moment before the app goes.
      if (quitDeferredForInstall) {
        emitError(phase, err);
        honorDeferredQuit();
        return;
      }
    }
    emitError(phase, err);
  }

  autoUpdater.on("error", (err) => {
    if (gateOwnsCheck() && failurePhase() === "check") {
      // A freshness gate's own re-check (or the check it joined) failed. The gate
      // folds that into "unknown" and proceeds: the download starts, or the
      // install emits `installing`. Reporting it would land a "Couldn't check for
      // updates" state between the click and that outcome, and on the quit path,
      // AFTER `installing`, unmounting the installing overlay while the gateway
      // stops. No install is running while a gate runs, so the failure cannot be
      // the installer's.
      log.info("[update] a freshness gate's check failed — proceeding on the stage it has", err);
      return;
    }
    if (abandonedCheck || abandonedError) {
      // The freshness gate stopped waiting for this check and let the install
      // proceed (fail-open). Reporting the check's OWN failure now would derive
      // phase "install" — `installing` outranks `checking` — reset `installing`,
      // and fire the host's gateway recovery in the middle of the bundle swap.
      // That failure changes nothing: the stage is exactly what it was, and it is
      // already being installed.
      //
      // But `error` is the one event BOTH the check and the installer are
      // funnelled through (`update-available`/`update-not-available` can only come
      // from a check, which is why those suppress unconditionally), and this flag
      // says only "a check was abandoned" — not "this error is that check's". The
      // window is wide, not instantaneous: an abandoned request is by definition
      // one that gave no answer in 8s, so a hung socket holds it open for the OS
      // TCP timeout while the install handoff it released takes about a second.
      // Deciding here would swallow exactly the Squirrel rejection the phase note
      // above exists for, latching `installing` with the gateway deliberately
      // stopped, no `error` state for the renderer (it sits on the installing
      // overlay) and nothing to restore the gateway.
      //
      // So ask WHICH error this is instead of assuming. The abandoned request's
      // own failure is the one it REJECTED with, and its rejection continuation
      // keeps that object in `abandonedError` — so the question is settled by
      // identity, not by which of the two arrived first.
      //
      // The one macrotask of delay is still needed, but only to give the
      // rejection somewhere to land: electron-updater today emits `error` and
      // then rejects, so at this instant the object may not have been captured
      // yet, and every microtask drains before any macrotask. Unlike the flag,
      // the ANSWER read one tick later does not depend on that order — a library
      // that rejects first, or emits a tick late, produces the same match.
      // Identity ALONE decides it, and deliberately so: "a failure is on record"
      // would be enough for one error but not for two, and two is the case that
      // matters. If the installer refuses and the abandoned request then fails in
      // the same tick, both events queue their decision before either rejection
      // lands, so a record-based read answers both with the first evidence it
      // finds and gets them exactly backwards — suppressing the installer's and
      // reporting the check's. Matching the object is the only read that keeps
      // them apart. The cost is a library that emits an error object it does not
      // then reject with: this would report that check's failure as the
      // install's. Nothing does that today, and it would take a deliberate
      // rewrap to start.
      const phase = failurePhase();
      setTimeout(() => {
        if (err === abandonedError) {
          // Spent — not because the verdict needs it (identity would answer a
          // later error correctly anyway) but so a single failed check does not
          // route every error for the rest of the process through this deferral.
          abandonedError = null;
          log.info("[update] abandoned freshness check failed after the install was dispatched — ignoring", err);
          return;
        }
        // No rejection carrying this object: the request is still running, or it
        // answered successfully, or it failed with something else. Either way
        // this error is someone else's — the installer's, on this very path.
        log.info("[update] failure during an abandoned freshness check was not the check's own — reporting", err);
        reportUpdaterFailure(err, phase);
      }, 0);
      return;
    }
    if (checking && installing) {
      // A check was already running when this install dispatched — the one order
      // `safeCheck`'s early return cannot prevent, since it only refuses to START a
      // check during an install. `installing` outranks `checking` in the phase
      // derivation, so this check's own feed failure would be reported as the
      // install's and fire the host's gateway recovery while the dispatch is still
      // inside `stopGateway()`, respawning a gateway whose predecessor is still
      // flushing.
      //
      // Ask WHOSE failure this is rather than assuming, exactly as the abandoned case
      // does. One macrotask of delay lets the rejection land in `inFlightCheckError`
      // (the library emits, then throws the same object), and identity — not arrival
      // order — decides. A genuine installer failure never matches, so it still
      // reaches recovery, which is what the deliberately-stopped gateway needs.
      const phase = failurePhase();
      setTimeout(() => {
        if (err === inFlightCheckError) {
          // `safeCheck`'s own catch already reported this as phase "check", which is
          // what it is. Reporting it again as the install's would be the misdiagnosis.
          log.info("[update] a check straddling an install dispatch failed — reported as the check's own, not the install's", err);
          return;
        }
        reportUpdaterFailure(err, phase);
      }, 0);
      return;
    }
    reportUpdaterFailure(err);
  });
  // Suppressed for the freshness gates' own re-checks, for the reason spelled out
  // at the emit in safeCheck: `checking` unmounts the update card, so a gate
  // running inside a Download or Install click would blank the surface the user
  // just acted on. This handler needs both flags where safeCheck needs only one —
  // verifyStageIsLatest calls autoUpdater.checkForUpdates() directly, so this is
  // the only place its check would reach the renderer.
  autoUpdater.on("checking-for-update", () => {
    log.info("[update] checking…");
    if (!gateOwnsCheck()) emit("checking");
  });
  autoUpdater.on("update-not-available", () => {
    if (abandonedCheck) {
      // Same reason as the error handler: this is the outcome of a check the
      // freshness gate stopped waiting for, and an install is already running on
      // the promise that it would. Clearing the stage now would pull the bytes
      // out from under a dispatch that has passed every guard.
      //
      // Known consequence, accepted: a POSITIVE retraction landing in this window
      // is treated as unknown, so a withdrawn build can still install. That is
      // fail-open behaving as specified rather than an oversight -- and it is why
      // the `!updateReady` half of the post-stopGateway abort below is unreachable
      // for an abandoned check: nothing on this path ever clears the stage.
      //
      // Not settled here: the check's owner settles it once electron-updater has
      // released its own in-flight promise. A waiter woken from inside this emit
      // would call checkForUpdates() too early and be handed this request back.
      log.info("[update] abandoned freshness check reports up to date after the install was dispatched — ignoring");
      return;
    }
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
    if (abandonedCheck) {
      // Same reason as the error handler: an install is already running on the
      // fail-open promise this check's wait made. Dropping the stage for the
      // newer build here would invalidate a dispatch that has passed every
      // guard, and starting its download would spend ~350MB during the swap.
      // The next launch's check finds this version and offers it then. Not
      // settled here, for the reason given in update-not-available.
      log.info(`[update] abandoned freshness check found ${(info && info.version) || "a newer build"} after the install was dispatched — ignoring`);
      return;
    }
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
        // Not for an install gate's own re-check: the renderer is already on
        // this stage, the manual path emits `installing` next, and the quit
        // path has emitted it already. A `downloaded` here would replace the
        // installing overlay with the "Update ready" dialog while the gateway
        // stops.
        if (!verifyingStage) emit("downloaded", { version: stagedVersion, notes: stagedNotes });
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
    // Not while quitting quietly: the download below is suppressed then, so a
    // "downloading" nudge would be false, and its dedupe would also swallow the
    // real one next launch.
    if (typeof notifyUpdateFound === "function" && !verifyQuiet) {
      try { notifyUpdateFound(foundVersion, { autoDownload }); } catch (err) { log.error("[update] notifyUpdateFound threw", err); }
    }
    // Not for the download gate's own re-check: it emits `downloading` for
    // whatever this leaves in `foundVersion`, and a `found` in between would
    // put the Download button back under the user's click.
    if (!preparingDownload) {
      emit("found", {
        version: foundVersion,
        notes: notesFrom(info),
        pubDate: (info && info.releaseDate) || "",
      });
    }
    // AFTER the "found" emit: the renderer must see the version it is about to
    // download, and startDownload() emits "downloading" over the top of it.
    // Fire-and-forget — startDownload owns its own error reporting, and this
    // handler is a synchronous event listener that cannot await.
    //
    // `verifyQuiet` marks the quitting caller of verifyStageIsLatest: the
    // process is seconds from exiting, so a fetch started here would only be
    // killed mid-flight. The next launch's check re-finds this version and
    // downloads it then, which is one transfer rather than one and a fragment.
    if (autoDownload && verifyQuiet) {
      log.info(`[update] not auto-downloading ${foundVersion} — the app is quitting`);
    } else if (autoDownload) {
      void startDownload({ automatic: true });
    }
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
  // natural quit, which sets `installing` only at the last moment, once its
  // freshness gate has released the feed — so `quitHandled` is what covers the
  // whole of that window and this condition still needs both). In either the
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
