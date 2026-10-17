// Who may be granted a screen capture in the Kiro Crew Electron app.
//
// `display-media.js` answers `setDisplayMediaRequestHandler` by picking a
// whole-screen source and granting it — no per-request confirmation UI on the
// `desktopCapturer` fallback path. So the question "who is asking" has to be
// answered before that, and it has to be answered by IDENTITY.
//
// ── Why shape is not enough ──────────────────────────────────────────────────
//
// The obvious test is `request.frame.parent === null`: only a top-level frame
// passes, so an embedded pane's iframe is refused. That test is not sound,
// because `parent === null` means "top-level frame of this webContents", not
// "is a document this app loaded". A page inside a non-sandboxed iframe can
// navigate the top frame (`target="_top"` on a user click), and the promoted
// document is then the main frame of the dashboard's own webContents. It would
// pass a shape test, and a loopback-hostname origin test as well, since the
// pane is served from the same host as the dashboard.
//
// ── What this module tests instead ───────────────────────────────────────────
//
// A request is authorized only when all three hold:
//
//   1. Its webContents is REGISTERED here — one this app created to host its
//      own documents. `registerCaptureSurface` is called at each such creation
//      site, so anything else (a future window, an embedded view) is refused by
//      default rather than by remembering to deny it.
//   2. Its frame IS that webContents' main frame. This is what refuses an
//      embedded pane, a preview iframe, or any other subframe, none of which
//      can be the main frame of a registered surface.
//   3. Its document's origin still EXACTLY matches the origin registered for
//      that webContents — scheme, host AND port. This is what refuses a
//      promoted document: a pane lives on a different loopback port than the
//      dashboard, so replacing the top document changes the origin even though
//      the host is identical.
//
// The port matters here and deliberately differs from `isAppOrigin`, which
// compares hostname only because a remote host reaches a dashboard through an
// SSH forward onto a loopback port. That reasoning is about which port the
// DASHBOARD is served on; it is not licence to accept every loopback port for
// the same webContents, which is exactly what the promoted-pane path abuses.
//
// A WeakMap, so an entry vanishes with the window and this never keeps a
// destroyed webContents alive — the same reason `browser-view.js` keeps its
// untrusted set as a WeakSet. Registration stores the ORIGIN rather than the
// full URL because the app navigates within its own origin freely (SPA routes,
// query tokens) and must keep capture across that.
"use strict";

/** webContents this app created to host its own documents -> that document's origin. */
const captureSurfaces = new WeakMap();

/**
 * Note a webContents as one of the app's own capture surfaces.
 *
 * Call this at every creation site whose renderer may call getDisplayMedia:
 * the dashboard view and the Mochi panel/crop windows. An unregistered
 * webContents is refused, so a NEW surface that forgets this call loses screen
 * capture — a visible, testable failure, rather than a silent grant to
 * something nobody authorized.
 *
 * A URL that will not parse is NOT registered: without a trustworthy origin
 * there is nothing to compare a later request against.
 *
 * @param {object} wc - the webContents being created
 * @param {string} appUrl - the app URL loaded into it (only its origin is kept)
 * @returns {boolean} true when the surface was registered
 */
function registerCaptureSurface(wc, appUrl) {
  if (!wc || typeof wc !== "object") return false;
  if (typeof appUrl !== "string" || !appUrl) return false;
  let origin;
  try {
    origin = new URL(appUrl).origin;
  } catch {
    return false;
  }
  // "null" is what URL reports for an opaque origin (data:, some file: forms).
  // Registering it would make two unrelated opaque documents compare equal.
  if (!origin || origin === "null") return false;
  captureSurfaces.set(wc, origin);
  return true;
}

/** Test seam: is this webContents registered, and as which origin? */
function registeredOrigin(wc) {
  if (!wc || typeof wc !== "object") return undefined;
  return captureSurfaces.get(wc);
}

/**
 * Build the `isTrustedRequest` predicate `display-media.js` takes.
 *
 * `fromFrame` is injected (it is `webContents.fromFrame` in the app) so the
 * whole decision is unit-testable without a live Electron process, matching how
 * `display-media.js` and `permission-handler.js` take their runtime glue.
 *
 * Every read is guarded: a destroyed WebFrameMain or webContents throws on
 * property access, and a throw here would otherwise escape into Chromium's
 * request handling. Anything unreadable is refused — a request that cannot be
 * attributed is not one to grant.
 *
 * @param {object} deps
 * @param {(frame: object) => object|undefined} deps.fromFrame - webContents.fromFrame
 * @returns {(request: object) => boolean}
 */
function createCaptureTrust(deps) {
  if (!deps || typeof deps.fromFrame !== "function") {
    throw new Error("fromFrame is required");
  }
  const fromFrame = deps.fromFrame;

  return function isAppCaptureRequest(request) {
    if (!request || typeof request !== "object") return false;

    let frame;
    try {
      frame = request.frame;
    } catch {
      return false;
    }
    if (!frame || typeof frame !== "object") return false;

    let wc;
    try {
      wc = fromFrame(frame);
    } catch {
      return false;
    }
    const expected = registeredOrigin(wc);
    if (!expected) return false; // leg 1: not a surface this app registered

    let main;
    try {
      main = wc.mainFrame;
    } catch {
      return false;
    }
    if (!main || main !== frame) return false; // leg 2: a subframe, e.g. a pane

    let url;
    try {
      url = frame.url;
    } catch {
      return false;
    }
    if (typeof url !== "string" || !url) return false;
    let actual;
    try {
      actual = new URL(url).origin;
    } catch {
      return false;
    }
    // leg 3: still the document this surface was registered for, port included
    return actual === expected;
  };
}

module.exports = {
  registerCaptureSurface,
  registeredOrigin,
  createCaptureTrust,
};
