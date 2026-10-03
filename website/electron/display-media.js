// Screen-share request handling for the KiroCrew Electron app.
//
// Why this exists: in a browser, navigator.mediaDevices.getDisplayMedia() shows
// the OS picker natively. In Electron (>= 20) the renderer's call is REJECTED
// unless the main process registers session.setDisplayMediaRequestHandler().
// Without it, the chat input's screen-snip tool silently does nothing in the
// packaged app (the renderer's capture promise rejects and the snip code treats
// it as "user cancelled"). This module supplies that handler.
//
// The Electron runtime glue (session, desktopCapturer, systemPreferences) is
// injected so the selection + permission logic is unit-testable without a live
// Electron process — mirroring how the renderer keeps getDisplayMedia at an
// untested I/O boundary.
//
// ── Who may ask ───────────────────────────────────────────────────────────
//
// This handler is the app's OWN picker: `chooseDisplaySource` selects a
// whole-screen source and grants it, so a grant here hands over the entire local
// screen with no per-request confirmation UI. `{ useSystemPicker: true }` at the
// call site prefers the OS picker where one exists, but this fallback runs
// everywhere it does not, so the grant cannot be scoped by trusting the picker
// to appear.
//
// It is registered on the DEFAULT session, which the dashboard shares with every
// frame it embeds — an iframe uses its parent's session, and the instances
// viewport delegates `display-capture` to panes that host another machine's
// dashboard. So a pane's request arrives here, and answering "who is asking" is
// what separates it from the app's own window.
//
// That question is NOT answered in this module, and deliberately not by the
// request's shape. `capture-trust.js` answers it by identity — a registered
// surface, its own main frame, still on its registered origin — because a
// top-level frame is not the same thing as a document this app loaded: a page in
// a non-sandboxed iframe can navigate the top frame and inherit its position.
// Read that module's header for why each leg is there.
//
// `deps.isTrustedRequest` is therefore REQUIRED in practice: with no dep the
// default refuses everything, so a call site that forgets to wire it loses
// capture visibly instead of granting screens to whoever asks.
//
// The embedded BROWSER PANEL never reaches this handler at all, by construction
// rather than by any gate: it runs on the `persist:kirocrew-browser` partition,
// which registers no display-media handler, and per this header's opening note a
// session with no handler REJECTS the renderer's call. That partition's
// permission handlers are additionally deny-all.
"use strict";

const os = require("node:os");

/**
 * Pick the best capture source from desktopCapturer.getSources().
 * Prefers a whole-screen source ("screen:*") over a window, since the snip
 * tool crops a region out of a full frame. Falls back to the first source.
 *
 * @param {Array<{id: string, name: string}>} sources
 * @returns {object|null} the chosen source, or null if there are none
 */
function chooseDisplaySource(sources) {
  if (!Array.isArray(sources) || sources.length === 0) return null;
  const screen = sources.find((s) => typeof s.id === "string" && s.id.startsWith("screen:"));
  return screen || sources[0];
}

/**
 * Build the handler passed to session.setDisplayMediaRequestHandler().
 *
 * @param {object} deps
 * @param {() => Promise<Array>} deps.getSources - desktopCapturer.getSources wrapper (REQUIRED)
 * @param {() => string} [deps.getScreenAccessStatus] - systemPreferences.getMediaAccessStatus('screen')
 * @param {(reason: string) => void} [deps.onPermissionNeeded] - surfaced when capture is blocked
 *        (reason: 'denied' = macOS Screen Recording off; 'no-sources' = nothing capturable;
 *        'untrusted-frame' = a subframe or foreign origin asked; 'error' = the probe threw)
 * @param {(request: object) => boolean} [deps.isTrustedRequest] - may this requester be
 *        granted a screen? Supply capture-trust.js's predicate. Omitted, it denies
 *        every request: a capability gate must fail closed when nobody wired it.
 * @param {string} [deps.platform] - process.platform (defaults to the running platform)
 * @returns {(request: object, callback: (streams: object) => void) => Promise<void>}
 */
/**
 * Platforms where Electron's `Streams.audio: 'loopback'` actually captures system
 * audio.
 *
 * Windows only, and that is Electron's own statement, not a guess — see the
 * `Streams.audio` doc comment in `electron.d.ts` for the version in
 * `node_modules` (43.2.0): a loopback device "is currently only supported on
 * Windows". Granting it elsewhere would be asking for a device the platform
 * cannot provide, on a handler the chat screen-snip tool also depends on.
 *
 * macOS 15+ does not need this path: `setDisplayMediaRequestHandler` is
 * installed with `{ useSystemPicker: true }`, and Electron uses the native
 * picker without invoking this handler. Older macOS releases still run the
 * handler, which deliberately grants video only.
 *
 * Deliberately NOT `electron-audio-loopback`. That package documents itself as
 * required for Electron >= 31 and < 39, and states that from Electron 39 on it is
 * unnecessary; this app is on 43.2.0, so adding it would be a dependency its own
 * author says is not needed here.
 */
const LOOPBACK_AUDIO_PLATFORMS = new Set(["win32"]);

/** Darwin 24 is macOS 15, the first release Electron supports here. */
const MACOS_SYSTEM_PICKER_MIN_DARWIN_MAJOR = 24;

/**
 * Whether Electron's native macOS screen picker exists on this host.
 *
 * Electron 43.2.0 documents `useSystemPicker` as macOS 15+ only. Darwin's
 * kernel major is stable for that mapping (24.x = macOS 15), so malformed or
 * unavailable releases fail closed to the handler path.
 *
 * @param {{platform?: string, release?: string}} opts
 * @returns {boolean}
 */
function supportsSystemPicker(opts) {
  const {
    platform = process.platform,
    release = os.release(),
  } = opts || {};
  if (platform !== "darwin") return false;
  const match = /^(\d+)(?:\.|$)/.exec(String(release));
  return match !== null && Number(match[1]) >= MACOS_SYSTEM_PICKER_MIN_DARWIN_MAJOR;
}

/**
 * Decide what to put in `Streams.audio`, or `undefined` for "grant no audio".
 *
 * Two gates, both load-bearing:
 *
 * 1. `audioRequested` — this handler is SHARED with the chat input's screen-snip
 *    tool, which asks for video only. Attaching a loopback audio device to a snip
 *    would start capturing the user's system audio for a screenshot.
 * 2. platform — see LOOPBACK_AUDIO_PLATFORMS.
 *
 * @param {{audioRequested?: boolean, platform?: string}} opts
 * @returns {"loopback"|undefined}
 */
function chooseAudioGrant(opts) {
  const { audioRequested, platform } = opts || {};
  if (!audioRequested) return undefined;
  if (!LOOPBACK_AUDIO_PLATFORMS.has(platform)) return undefined;
  return "loopback";
}

function createDisplayMediaHandler(deps) {
  if (!deps || typeof deps.getSources !== "function") {
    throw new Error("getSources is required");
  }
  const getSources = deps.getSources;
  const getScreenAccessStatus = deps.getScreenAccessStatus || (() => "granted");
  const onPermissionNeeded = deps.onPermissionNeeded || (() => {});
  const isTrustedRequest = deps.isTrustedRequest || (() => false);
  const platform = deps.platform || process.platform;

  return async function handleDisplayMediaRequest(request, callback) {
    try {
      // Identity first, before any OS or source probe: a refused requester must
      // not reach getSources() at all, since that call is what triggers macOS's
      // Screen Recording prompt and enumerates every open window's title.
      if (!isTrustedRequest(request)) {
        onPermissionNeeded("untrusted-frame");
        callback({}); // deny -> the caller's getDisplayMedia rejects, as it does with no handler
        return;
      }

      // macOS gates screen capture behind the Screen Recording TCC permission.
      // 'not-determined' is allowed through — getSources() triggers the OS
      // prompt. An explicit 'denied'/'restricted' will never yield frames, so
      // short-circuit and guide the user to System Settings instead of failing
      // opaquely.
      if (platform === "darwin") {
        const status = getScreenAccessStatus();
        if (status === "denied" || status === "restricted") {
          onPermissionNeeded("denied");
          callback({}); // deny -> renderer getDisplayMedia rejects -> snip no-ops cleanly
          return;
        }
      }

      const sources = await getSources();
      const source = chooseDisplaySource(sources);
      if (!source) {
        onPermissionNeeded("no-sources");
        callback({});
        return;
      }
      // Meeting capture asks for audio as well as video; the snip tool does not.
      // Where Electron can supply a loopback device, granting it here means the
      // meeting gets the other participants' voices with NO picker at all, since
      // this handler already auto-selects the source.
      const streams = { video: source };
      const audio = chooseAudioGrant({
        audioRequested: request && request.audioRequested,
        platform,
      });
      if (audio) streams.audio = audio;
      callback(streams);
    } catch (err) {
      // Never throw out of the handler: a rejection here would crash the
      // request. Deny gracefully so the renderer's catch path runs.
      onPermissionNeeded("error");
      callback({});
    }
  };
}

/**
 * Which audio-capture tier this platform gets, as a string the renderer can
 * branch its guidance on.
 *
 * Derived from the same two facts the handler uses, so the message the user reads
 * and the grant they actually get cannot disagree:
 *
 * - `loopback`      — this handler grants a loopback device, no picker, system
 *                     audio just arrives (Windows).
 * - `system-picker` — Electron uses the native macOS 15+ picker instead of this
 *                     handler, so whether audio arrives is the user's pick.
 * - `video-only`    — this handler runs but cannot grant audio, and it
 *                     auto-selects a source, so there is no picker in which the
 *                     user could offer audio either. Microphone only (macOS 14
 *                     and older, and Linux).
 *
 * @param {{platform?: string, release?: string, useSystemPicker?: boolean}} opts
 * @returns {"loopback"|"system-picker"|"video-only"}
 */
function describeAudioTier(opts) {
  const { platform, release, useSystemPicker } = opts || {};
  if (LOOPBACK_AUDIO_PLATFORMS.has(platform)) return "loopback";
  if (useSystemPicker && supportsSystemPicker({ platform, release })) {
    return "system-picker";
  }
  return "video-only";
}

module.exports = {
  chooseDisplaySource,
  chooseAudioGrant,
  createDisplayMediaHandler,
  describeAudioTier,
  supportsSystemPicker,
  LOOPBACK_AUDIO_PLATFORMS,
  MACOS_SYSTEM_PICKER_MIN_DARWIN_MAJOR,
};
