"use strict";

/** Chromium reports this when a newer navigation supersedes the current load. */
const ERR_ABORTED = -3;

/** A completed 4xx/5xx main-frame navigation is an error document. */
function isAppWindowErrorResponse(httpResponseCode) {
  return typeof httpResponseCode === "number" && httpResponseCode >= 400;
}

/**
 * Track app windows whose main document is not safe to reveal or initialize.
 * The host owns the visible consequence, credentials, retry cadence, and reload.
 */
function createAppWindowErrorLatch({ onBlank = () => {} } = {}) {
  const blanked = new WeakSet();

  function isLive(win) {
    return !!win && !win.isDestroyed();
  }

  function markBlanked(win) {
    if (!isLive(win)) return;
    blanked.add(win);
    onBlank(win);
  }

  function handleNavigation(win, httpResponseCode) {
    if (!isLive(win)) return;
    if (isAppWindowErrorResponse(httpResponseCode)) {
      markBlanked(win);
    } else {
      blanked.delete(win);
    }
  }

  function handleLoadFailure(win, errorCode, isMainFrame) {
    if (!isLive(win)) return;
    if (isMainFrame === false || errorCode === ERR_ABORTED) return;
    markBlanked(win);
  }

  function isBlanked(win) {
    return isLive(win) && blanked.has(win);
  }

  function hasBlanked(windows) {
    for (const win of windows) {
      if (isBlanked(win)) return true;
    }
    return false;
  }

  function rearm(windows, reload) {
    let count = 0;
    for (const win of windows) {
      if (!isBlanked(win)) continue;
      reload(win);
      count += 1;
    }
    return count;
  }

  return {
    handleNavigation,
    handleLoadFailure,
    isBlanked,
    hasBlanked,
    rearm,
  };
}

module.exports = {
  createAppWindowErrorLatch,
  isAppWindowErrorResponse,
};
