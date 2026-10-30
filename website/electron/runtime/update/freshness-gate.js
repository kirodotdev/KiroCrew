// The feed lane's check lifecycle and its freshness gate: the state that
// describes the one electron-updater check that can be in flight (who claimed
// it, who is waiting on it, whether anyone gave up on it), the operations that
// move that state, and the pre-install verdict `verifyStageIsLatest` reads out
// of it. feed-lane.js composes one gate per lane and reads the flags through it;
// the stage (`updateReady`, `stagedVersion`) and the feed's channel stay the
// lane's, handed in as readers, because the gate judges them and never owns them.

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

/**
 * One lane's check lifecycle and freshness gate.
 *
 * The returned object carries the check flags as plain fields, so the lane's
 * handlers read and write them directly (`gate.checking`, `gate.abandonedError`),
 * and the lifecycle operations and the gate's verdict as methods. Every
 * `checking = true` goes through `claimCheck`, every release through
 * `checkSettled`; the fields are shared state, not a snapshot.
 *
 * @param {object} deps
 * @param {{checkForUpdates: Function}} deps.autoUpdater
 * @param {{info: Function, error: Function}} deps.log
 * @param {() => string} deps.currentChannel the channel the user follows now
 * @param {() => string|null} deps.feedChannel the channel the LAST configureFeed()
 *   pointed the updater at (see feed-lane.js)
 * @param {() => {ready: boolean, version: string|null}} deps.stage the lane's
 *   staged build, read live
 * @param {() => string} deps.configureFeed re-point the updater at the followed
 *   channel's feed
 * @param {() => Promise<void>} deps.safeCheck the lane's ordinary check
 * @param {(state: string, extra?: object) => void} deps.emit report an update
 *   state to the renderer
 */
function createFreshnessGate({
  autoUpdater,
  log,
  currentChannel,
  feedChannel,
  stage,
  configureFeed,
  safeCheck,
  emit,
}) {
  const gate = {
    checking: false,
    // Resolves when the check that owns `checking` settles; null whenever no check
    // is in flight.
    //
    // Held so a caller that finds a check ALREADY running can wait for its answer.
    // Without it the only two options are both wrong for the freshness gates:
    // re-entering is refused outright (safeCheck's first line), and proceeding
    // reads `foundVersion`/`stagedVersion` while the running check is in the middle
    // of replacing them — which is how the download gate could spend the ~350MB it
    // exists to protect on the very stale build it was checking for.
    checkInFlight: null,
    // A pre-install freshness check is in flight (see verifyStageIsLatest). Kept
    // apart from `checking` because it doubles as the install path's re-entrancy
    // guard: the verify awaits the feed BEFORE `installing` is set, so without it
    // two fast clicks would both reach the dispatch.
    verifyingStage: false,
    // A manual download's own pre-download check is in flight. The download path's
    // re-entrancy guard, for the same reason `verifyingStage` is the install
    // path's: the gate awaits the feed BEFORE `downloading` is set.
    preparingDownload: false,
    // Suppress the automatic download of a newer build for the CURRENT verify.
    // Set only by the quit-time caller: discovering a newer release seconds
    // before the process exits must not start a ~350MB fetch the exit will kill.
    verifyQuiet: false,
    // A freshness check whose bounded WAIT expired while its request stayed in
    // flight (see verifyStageIsLatest). The fail-open contract sends that caller
    // straight on to install, so the abandoned request's outcome can now land
    // past the install dispatch -- precisely what the post-stopGateway `checking`
    // abort exists to prevent. That abort cannot be the answer here (aborting is
    // what fail-open refuses to do), so the late outcome is made INERT instead:
    // the lane's handlers decline to touch the stage, the install, or a download
    // for it. Cleared when the request settles, or when an install gate joins
    // the request and needs its answer (see verifyStageIsLatest).
    abandonedCheck: false,
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
    abandonedError: null,
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
    inFlightCheckError: null,
  };
  // The resolver of `checkInFlight`; private, since only claim and settle touch it.
  let releaseCheckInFlight = null;

  /**
   * Release the state a check owns FOR ITS WHOLE LIFETIME. Called only by the
   * check's owner (safeCheck's finally, or verifyStageIsLatest once the request
   * settles), never from an event handler: electron-updater emits a check's
   * outcome before it releases its own in-flight promise, so a waiter woken
   * inside the emit would be handed the same request back. Idempotent.
   */
  function checkSettled() {
    gate.checking = false;
    gate.verifyQuiet = false;
    gate.abandonedCheck = false;
    // Wake anyone waiting on this check BEFORE returning, and drop the promise so
    // a later waiter cannot latch onto an already-resolved one. Nulled first so
    // the callback cannot observe a settled check that still looks in flight.
    const release = releaseCheckInFlight;
    gate.checkInFlight = null;
    releaseCheckInFlight = null;
    if (release) release();
  }
  /**
   * Give up on the check in flight: its late outcome becomes inert (see
   * `abandonedCheck`), and its rejection is captured afresh for the identity
   * match (see `abandonedError`).
   */
  function abandonCheck() {
    gate.abandonedCheck = true;
    gate.abandonedError = null;
  }
  /**
   * A gate joined an abandoned check and needs its answer: the outcome counts
   * again, and its failure is no longer one the identity match should spend.
   */
  function liftAbandonment() {
    gate.abandonedCheck = false;
    gate.abandonedError = null;
  }
  /**
   * Claim `checking` and publish the promise that resolves when it settles.
   *
   * Every `checking = true` goes through here: the promise is only useful to a
   * waiter if it exists for the WHOLE window the flag does, and a flag set
   * directly would be a window with nothing to wait on.
   */
  function claimCheck() {
    gate.checking = true;
    // The previous check's claimed error must not answer for this one.
    gate.inFlightCheckError = null;
    gate.checkInFlight = new Promise((resolve) => { releaseCheckInFlight = resolve; });
  }
  /**
   * A freshness gate is waiting on the check in flight. Its `checking`, its
   * failure, and its restatement of the offer stay internal: the gate's own
   * next state (`downloading`, `installing`, or the handlers' verdict) is what
   * the renderer needs, and each of those interim states would unmount or
   * replace the surface the user just clicked.
   */
  function ownsCheck() {
    return gate.preparingDownload || gate.verifyingStage;
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
    const { ready, version: staged } = stage();
    if (!ready) return "superseded";
    if (!staged) return "unknown";
    if (gate.checking) {
      // A QUIET caller needs the quiet on the check already running: a newer
      // version in its answer would otherwise start a ~350MB automatic download
      // seconds before the process exits. Safe to set because checkSettled()
      // clears it, and the check that will call it is the one in flight.
      if (quiet) gate.verifyQuiet = true;
      // Lift any abandonment of this check: no install is running when a gate
      // starts (both install paths refuse to start one while another runs), so
      // whoever abandoned it was the download gate, or an install that has since
      // failed, and neither still needs its answer inert. This gate reads its
      // verdict from what the check's handlers do to the stage, so the answer
      // must reach them, or a newer build or a retraction landing inside this
      // bound is ignored and the stale stage reads as "latest".
      liftAbandonment();
      gate.verifyingStage = true;
      try {
        if (await withinFreshnessBound(gate.checkInFlight) === "timeout") {
          // Abandon the joined check exactly as an own check is abandoned below:
          // its late outcome becomes inert, so the install this "unknown"
          // promises proceeds instead of being aborted at the post-stopGateway
          // `checking` guard after the gateway was already stopped for it.
          // safeCheck's catch records its rejection for the identity match.
          abandonCheck();
          if (refuseStageFromChangedChannel({ quiet })) return "superseded";
          log.info(`[update] the check in flight did not answer in ${FRESHNESS_CHECK_TIMEOUT_MS}ms — installing the staged ${staged}`);
          return "unknown";
        }
      } finally {
        gate.verifyingStage = false;
      }
      // That check's own handlers have already applied its verdict to the stage.
      return stageVerdict(staged, { quiet });
    }
    gate.verifyingStage = true;
    gate.verifyQuiet = quiet;
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
        settled.then(checkSettled, (err) => { gate.abandonedError = err; checkSettled(); });
        if (refuseStageFromChangedChannel({ quiet })) return "superseded";
        log.info(`[update] freshness check did not answer in ${FRESHNESS_CHECK_TIMEOUT_MS}ms — installing the staged ${staged}`);
        return "unknown";
      }
      checkSettled();
      if (outcome !== "checked") {
        if (refuseStageFromChangedChannel({ quiet })) return "superseded";
        log.info(`[update] freshness check failed — installing the staged ${staged}`);
        return "unknown";
      }
    } catch (err) {
      // configureFeed() threw, so no request went out and no `error` event fired.
      // A feed we could not reach is not evidence that the stage is stale: the
      // failure is logged and folded into "unknown", and the renderer is not told,
      // as with every gate's own check failure (see the lane's `error` handler).
      checkSettled();
      if (refuseStageFromChangedChannel({ quiet })) return "superseded";
      log.error(`[update] freshness check failed — installing the staged ${staged}`, err);
      return "unknown";
    } finally {
      gate.verifyingStage = false;
    }
    return stageVerdict(staged, { quiet });
  }

  /**
   * Refuse a stage whose check describes a channel the user stopped following.
   * A manual install asks the selected lane after the old request releases the
   * updater. A quitting caller leaves that work to the next launch.
   */
  function refuseStageFromChangedChannel({ quiet = false } = {}) {
    const selectedChannel = currentChannel();
    const checkedChannel = feedChannel();
    if (checkedChannel === selectedChannel) return false;
    log.info(`[update] channel changed during the freshness check (${checkedChannel} -> ${selectedChannel}) — not installing on the old lane's verdict`);
    if (!quiet) {
      const pendingCheck = gate.checkInFlight;
      if (pendingCheck) {
        // `checking` now, while the new lane waits: the old check can hold its
        // socket for a minute, and the renderer must not sit on the refused
        // stage's `downloaded` card and install latch that long. The abandoned
        // check's handlers are inert, so nothing else replaces that state.
        emit("checking");
        void pendingCheck.then(() => safeCheck());
      } else {
        void safeCheck();
      }
    }
    return true;
  }

  /** Read a check's verdict back out of the stage its handlers left behind. */
  function stageVerdict(staged, { quiet = false } = {}) {
    if (refuseStageFromChangedChannel({ quiet })) return "superseded";
    const { ready, version } = stage();
    return ready && version === staged ? "latest" : "superseded";
  }

  return Object.assign(gate, {
    checkSettled,
    abandonCheck,
    liftAbandonment,
    claimCheck,
    ownsCheck,
    verifyStageIsLatest,
    refuseStageFromChangedChannel,
    stageVerdict,
  });
}

module.exports = { FRESHNESS_CHECK_TIMEOUT_MS, withinFreshnessBound, createFreshnessGate };
