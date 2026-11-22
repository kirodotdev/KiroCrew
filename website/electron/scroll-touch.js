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
/** Main -> dashboard: macOS Natural scrolling, `true` on or `false` off. */
const SCROLL_DIRECTION_CHANNEL = "scroll-direction";

/**
 * macOS Natural scrolling (System Settings > Trackpad), or null when unknown.
 * A wheel's `deltaX` is the scroll, not the fingers: with Natural scrolling off
 * its sign is flipped, so the page needs this to tell which way the fingers
 * moved. Read as a string: the key is absent until the user changes the
 * setting, and absent means the macOS default, on. A `boolean` read would
 * report that as false.
 */
function readNaturalScrolling(systemPreferences) {
  let raw;
  try {
    raw = systemPreferences.getUserDefault("com.apple.swipescrolldirection", "string");
  } catch {
    return null;
  }
  if (raw === "" || raw === "1") return true;
  if (raw === "0") return false;
  return null;
}

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
 * With `readNatural`, the Natural scrolling setting also goes out on
 * {@link SCROLL_DIRECTION_CHANNEL}: on every page load, and again when a
 * gesture begins and the setting has changed since, so a change in System
 * Settings applies from the next swipe.
 *
 * Returns the input-event handler (for tests), or null when nothing was armed.
 */
function armScrollTouch(wc, { platform = process.platform, isAlive = () => true, readNatural = null } = {}) {
  if (platform !== "darwin" || !wc || typeof wc.on !== "function") return null;
  let last = "end";
  let natural = null;
  const send = (channel, value) => {
    try {
      if (!isAlive()) return;
      wc.send(channel, value);
    } catch {
      /* mid-teardown: same tolerance as the other window helpers */
    }
  };
  const sendNatural = (always) => {
    if (typeof readNatural !== "function") return;
    const now = readNatural();
    if (now === null || (!always && now === natural)) return;
    natural = now;
    send(SCROLL_DIRECTION_CHANNEL, now);
  };
  const onInput = (_event, input) => {
    const phase = scrollTouchPhase(input);
    if (phase === null || phase === last) return;
    last = phase;
    if (phase === "begin") sendNatural(false);
    send(SCROLL_TOUCH_CHANNEL, phase);
  };
  wc.on("input-event", onInput);
  // A new page has a new preload with nothing cached: tell it.
  wc.on("did-finish-load", () => sendNatural(true));
  return onInput;
}

module.exports = {
  armScrollTouch,
  readNaturalScrolling,
  scrollTouchPhase,
  SCROLL_DIRECTION_CHANNEL,
  SCROLL_TOUCH_CHANNEL,
};
