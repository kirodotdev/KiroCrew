/**
 * What the updater tells the renderer: the channel this install follows, the
 * lane pair the About panel renders (what the followed feed publishes, and
 * whether these bytes are ahead of it), every lifecycle push, and the info
 * payload that replays the last push to a freshly mounted renderer.
 *
 * Both lanes (the electron-updater feed lane and the marker-driven managed
 * lane) report through this one owner, so the payload shapes cannot diverge.
 * The channel and version policy it reads is the facade's (auto-update.js).
 */
function createUpdateReporter({
  app,
  getFlavor,
  getChannelPreference,
  getAutoDownloadPreference,
  onUpdateState,
  uiDriven,
  log,
  osPlatform,
  osArch,
  platform,
  managed,
  linux,
  channelForFlavor,
  channelForVersion,
  resolveChannel,
  isNewerVersion,
  manualDownloadUrl,
}) {
  // Last lifecycle payload handed to the UI. Pushed state dies with the
  // renderer: the post-install-failure recovery path reloads the window, and a
  // fresh mount that only ever LISTENS would render as if nothing happened --
  // the failure card (and its Retry) silently vanish. getInfo() carries this
  // back out so the renderer can replay it on mount, which keeps the boot path
  // untouched (the renderer already requests the info payload).
  let lastEmittedState = null;
  // The version the FOLLOWED channel's feed last reported, and the channel it was
  // reported FOR. Recorded because promotion never re-stamps: the stable feed's
  // current release is literally `0.4.1-insider.1`, so `channelForVersion` cannot
  // tell a promoted-stable install from an insider one, and every surface that
  // asked the version string "which lane am I on" answered `insider` for the whole
  // promoted-stable population. The feed's own answer is the only honest input, so
  // it is kept for the display layer.
  let laneVersion = null;
  let laneChannel = null;
  /**
   * The lane pair, or UNKNOWN. Reported as unknown unless the recorded version
   * was read for the channel this install follows RIGHT NOW: a switch makes the
   * old lane's answer describe a lane nobody is on, and `update:set-channel`
   * returns `getInfo()` synchronously while its re-check is still in flight, so a
   * read-time comparison is what closes that window rather than clearing state on
   * an ordering assumption. Concretely, without it: flip insider -> stable on an
   * up-to-date insider build while the follow-up check cannot reach the feed
   * (offline), and a retained `runningAheadOfLane: false` tells the panel these
   * bytes ARE the stable release — folding the chip to a version that does not
   * exist and suppressing the prerelease ask, i.e. the very bug this pair exists
   * to fix. `null` means no usable answer and must never read as "ahead".
   */
  function laneSnapshot() {
    if (!laneVersion || laneChannel !== currentChannel()) {
      return { laneVersion: "", runningAheadOfLane: null };
    }
    return { laneVersion, runningAheadOfLane: isNewerVersion(app.getVersion(), laneVersion) };
  }
  /**
   * Record what the lane just answered, attributed to the lane that answered.
   * `feedChannel` is the channel the feed lane's last configureFeed() pointed at.
   */
  function recordLaneVersion(version, feedChannel) {
    if (!version) return;
    laneVersion = version;
    // The channel the FETCH was configured for, not a live read: the preference
    // can flip while a check is in flight, and attributing that answer to the new
    // lane is the same mis-pairing `shouldAutoOffer` avoids with `feedChannel`.
    laneChannel = feedChannel || currentChannel();
  }
  // Single channel resolver used for the feed AND everything reported to
  // the UI. Read the preference FRESH on every call: configureFeed() runs
  // per check, so a Settings channel switch takes effect on the next check
  // with no re-init. Flavor stays the unstamped-dev display fallback.
  function currentChannel() {
    const stamped = channelForVersion(app.getVersion());
    return resolveChannel(stamped, getChannelPreference()) || channelForFlavor(getFlavor());
  }
  function emit(state, extra = {}) {
    if (!uiDriven) return;
    const payload = {
      state,
      channel: currentChannel(),
      version: app.getVersion(),
      // The renderer cannot infer the real updater handoff from getInfo().platform:
      // packaged Linux variants and older bundles make that display field an
      // unreliable capability signal. Carry the handoff contract with every
      // lifecycle event so both ready surfaces can set the right expectation.
      // Externally managed Windows installs run the marker's update command
      // and relaunch directly; they never hand off to our NSIS installer.
      installHandoff: osPlatform === "win32" && !managed
        ? "windows-installer"
        : "automatic-relaunch",
      // Display inputs for the version chip and the prerelease note (see
      // laneSnapshot). Carried on every lifecycle payload as well as getInfo()
      // so a renderer driven by pushes alone never falls back to the
      // stamp-based guess this pair replaces -- and so a renderer that mounted
      // before the latest check does not keep rendering that older answer.
      ...laneSnapshot(),
      ...extra,
    };
    // Remembered even when the push below throws: a renderer that missed the
    // push is exactly the one the getInfo() replay exists to catch up.
    lastEmittedState = payload;
    try {
      onUpdateState(payload);
    } catch (err) {
      log.error("[update] onUpdateState threw", err);
    }
  }
  function getInfo() {
    const stamped = channelForVersion(app.getVersion());
    // Observability for the replay path: without this line a replayed state is
    // indistinguishable from a live emit in the log, so a report of "the
    // failure card came back / didn't come back" has no evidence to read.
    if (lastEmittedState) {
      log.info(`[update] getInfo carrying replay seed: ${lastEmittedState.state}`
        + (lastEmittedState.phase ? ` (phase ${lastEmittedState.phase})` : ""));
    }
    return {
      version: app.getVersion(),
      channel: currentChannel(),
      // Switcher inputs: the build's own lane, whether this build may switch
      // (nightly is pinned; dev has no lane; an externally-managed install has
      // no lane the marker's owner reads), and the stored preference.
      stampedChannel: stamped,
      channelSwitchable: !managed && (stamped === "insider" || stamped === "stable"),
      channelPreference: getChannelPreference() || "",
      // What the FOLLOWED channel publishes, and whether these bytes are ahead
      // of it — i.e. that lane never shipped this build, so the install is not
      // on it. Both come from the feed rather than from `stampedChannel`, which
      // a promoted stable release makes unusable for the question (its bytes
      // keep the soaked candidate's insider stamp). "" / null until a check has
      // completed FOR THE CHANNEL THIS INSTALL FOLLOWS (see laneSnapshot).
      ...laneSnapshot(),
      // Current auto-download policy, so About renders the toggle from the
      // value the updater will actually act on rather than from its own copy
      // of the store. Read through the same guard as the event path: a
      // throwing reader reports "off", matching what would happen on discovery.
      autoDownload: (() => {
        try { return !!getAutoDownloadPreference(); } catch { return false; }
      })(),
      // Externally-managed metadata, both empty on a self-updating install.
      managedBy: managed ? managed.managedBy || "" : "",
      updateCommand: managed ? managed.updateCommand || "" : "",
      platform,
      packaged: !!app.isPackaged,
      // Escape hatch for a failed install (see manualDownloadUrl).
      downloadUrl: manualDownloadUrl(currentChannel(), osPlatform, osArch, linux.format),
      // Replay seed for a freshly mounted renderer (see lastEmittedState).
      lastState: lastEmittedState,
    };
  }

  return { currentChannel, emit, getInfo, recordLaneVersion };
}

module.exports = { createUpdateReporter };
