"use strict";

/**
 * Relay the trackpad's real finger-down / finger-up to the dashboard.
 *
 * A two-finger trackpad swipe reaches a page only as `wheel` events, which say
 * nothing about when the fingers touched or left: the page's swipe reader
 * (website/src/hooks/useTrackpadSwipe) has to guess the lift-off from a gap in
 * the stream or from the shape of the momentum tail. Chromium itself knows: on
 * macOS a phased wheel opens a `gestureScrollBegin` when the fingers land and
 * closes with `gestureScrollEnd`, and Electron reports both through the
 * documented WebContents `input-event` (the replacement for the removed
 * BrowserWindow `scroll-touch-begin` / `scroll-touch-end` events):
 * https://www.electronjs.org/docs/latest/breaking-changes#removed-browserwindow-scroll-touch--events
 *
 * This forwards each change as one word, `begin` or `end`, on
 * {@link SCROLL_TOUCH_CHANNEL}; the preload hands it to the page as
 * `electronAPI.onScrollTouch`. Nothing else crosses: no position, no delta, no
 * timing. The page treats the signal as a hint and keeps its own gap and
 * momentum detection, so a lost or late phase costs nothing but precision.
 *
 * macOS only. Other platforms have no reliable touchpad phase, and Chromium can
 * open and close a gesture per plain wheel tick there, which would read as a
 * lift-off after every event.
 */

const SCROLL_TOUCH_CHANNEL = "scroll-touch";

/** The phase an Electron input event announces, or null for any other event. */
function scrollTouchPhase(input) {
  if (!input || typeof input !== "object") return null;
  if (input.type === "gestureScrollBegin") return "begin";
  if (input.type === "gestureScrollEnd") return "end";
  return null;
}

/**
 * Arm the relay on the dashboard's WebContents. Call ONCE per WebContents. Only
 * a CHANGE of phase is sent, so a repeated begin (a second scroll starting
 * before Chromium closed the first) does not reach the page twice.
 *
 * Returns the input-event handler (for tests), or null when nothing was armed.
 */
function armScrollTouch(wc, { platform = process.platform, isAlive = () => true } = {}) {
  if (platform !== "darwin" || !wc || typeof wc.on !== "function") return null;
  let last = "end";
  const onInput = (_event, input) => {
    const phase = scrollTouchPhase(input);
    if (phase === null || phase === last) return;
    last = phase;
    try {
      if (!isAlive()) return;
      wc.send(SCROLL_TOUCH_CHANNEL, phase);
    } catch {
      /* mid-teardown: same tolerance as the other window helpers */
    }
  };
  wc.on("input-event", onInput);
  return onInput;
}

module.exports = { armScrollTouch, scrollTouchPhase, SCROLL_TOUCH_CHANNEL };
