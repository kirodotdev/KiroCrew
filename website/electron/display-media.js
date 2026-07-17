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
"use strict";

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
 *        (reason: 'denied' = macOS Screen Recording off; 'no-sources' = nothing capturable)
 * @param {string} [deps.platform] - process.platform (defaults to the running platform)
 * @returns {(request: object, callback: (streams: object) => void) => Promise<void>}
 */
function createDisplayMediaHandler(deps) {
  if (!deps || typeof deps.getSources !== "function") {
    throw new Error("getSources is required");
  }
  const getSources = deps.getSources;
  const getScreenAccessStatus = deps.getScreenAccessStatus || (() => "granted");
  const onPermissionNeeded = deps.onPermissionNeeded || (() => {});
  const platform = deps.platform || process.platform;

  return async function handleDisplayMediaRequest(_request, callback) {
    try {
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
      callback({ video: source });
    } catch (err) {
      // Never throw out of the handler: a rejection here would crash the
      // request. Deny gracefully so the renderer's catch path runs.
      onPermissionNeeded("error");
      callback({});
    }
  };
}

module.exports = { chooseDisplaySource, createDisplayMediaHandler };
