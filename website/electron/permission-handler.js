// Renderer permission gating for the KiroCrew Electron app.
//
// Why this exists: without an explicit handler, Electron's default permission
// *check* can report `media` as denied for the renderer's getUserMedia(), so
// the chat input's mic button silently no-ops in the packaged app even though
// it works in a plain browser (Chromium prompts there). These handlers grant
// the microphone and deny everything else, per least privilege. Screen capture
// has its own seam (display-media.js) and is unaffected.
//
// ── The bug this module fixes ────────────────────────────────────────────────
//
// Voice input was denied in the packaged app while working in Chrome at the
// SAME origin, with the SAME macOS (TCC) grant, on the SAME device — proving by
// isolation that the denial came from this layer and not from the OS. The
// renderer saw `NotAllowedError` from getUserMedia even though
// navigator.permissions.query({name:'microphone'}) reported "granted" and
// device labels were populated (Chromium only exposes labels post-grant).
//
// The original CHECK handler had two independent defects:
//
//     setPermissionCheckHandler((wc, permission, _origin, details) => {
//       if (permission === "media") return isAppOrigin(wc) && details?.mediaType === "audio";
//       return false;
//     });
//
//   1. `wc` MAY BE NULL. Electron passes a null webContents for permission
//      checks that don't originate from a live frame. The old isAppOrigin then
//      ran `new URL("")`, threw, and returned false — a deny. Meanwhile
//      `_origin`, the origin STRING Electron supplies for exactly this case,
//      was discarded. That is the bug this module's two-source origin check
//      closes.
//   2. `mediaType === "audio"` is an exact match, so any other value — notably
//      'unknown', which Chromium uses on some paths — also denied.
//
// Either defect alone produces the observed asymmetry: permissions.query()
// arrives with a real frame and mediaType 'audio' (granted), while
// getUserMedia's internal check arrives without one (denied). The fix covers
// both, so it holds regardless of which fired.
//
// ── The rule ─────────────────────────────────────────────────────────────────
//
// Deny only when video is EXPLICITLY requested. Audio-only and unspecified both
// pass. That keeps the camera gated while making the mic robust to absent or
// renamed detail fields. Non-`media` permissions are still denied wholesale.
//
// A denial logs one line to the main-process console, because diagnosing this
// previously required attaching a debugger to the main process — the handlers
// were silent, so a deny was indistinguishable from an OS refusal.
//
// Pure logic with the origin lookup injected, so both handlers are unit
// testable without a live Electron session — mirroring display-media.js.
"use strict";

/**
 * True when the request belongs to the app's own dashboard.
 *
 * Checks TWO sources because neither is always present:
 *   - `wc.getURL()` — available for frame-originated requests.
 *   - `origin` — the string Electron passes to the CHECK handler, and the ONLY
 *     signal when `wc` is null. Ignoring it is what denied the microphone.
 *
 * Both are parsed as URLs and compared on hostname; never substring-matched, so
 * `http://localhost.evil.example/` cannot pass. The shell always loads
 * `http://localhost:<port>` (main.js BACKEND_URL) and remote hosts are SSH
 * forwards onto that same loopback port, so hostname is a stable identity.
 *
 * @param {{ getURL?: () => string } | null | undefined} wc
 * @param {string} [origin] - securityOrigin string, when Electron supplies one
 * @returns {boolean}
 */
function isAppOrigin(wc, origin) {
  let fromWc;
  try {
    fromWc = wc?.getURL?.();
  } catch {
    fromWc = undefined; // a destroyed webContents throws on getURL()
  }
  for (const candidate of [fromWc, origin]) {
    if (!candidate || typeof candidate !== "string") continue;
    try {
      if (new URL(candidate).hostname === "localhost") return true;
    } catch {
      // Unparseable — try the next source rather than denying outright.
    }
  }
  return false;
}

/**
 * Does this request explicitly ask for video (camera)?
 *
 * Only an explicit "video" entry counts. An absent or non-array `mediaTypes`
 * is NOT treated as a video request — see the fail-open rationale above.
 *
 * @param {{ mediaTypes?: Array<string> } | null | undefined} details
 * @returns {boolean}
 */
function requestsVideo(details) {
  const types = details?.mediaTypes;
  return Array.isArray(types) && types.includes("video");
}

/** One-line breadcrumb so a denial is visible without attaching a debugger. */
function logDeny(kind, permission, wc, origin, details) {
  // eslint-disable-next-line no-console -- see module header: silent denials
  // cost two debugging sessions.
  console.warn(
    `[permission] ${kind} DENY permission=${permission}`,
    `wcUrl=${(() => { try { return wc?.getURL?.() || "<none>"; } catch { return "<throws>"; } })()}`,
    `origin=${origin || "<none>"}`,
    `details=${JSON.stringify(details ?? null)}`,
  );
}

/**
 * Build the handler for session.setPermissionRequestHandler().
 *
 * Grants `media` for the app origin unless video is explicitly requested.
 * Denies every other permission type (geolocation, clipboard, notifications,
 * MIDI, …).
 *
 * @param {object} [deps]
 * @param {(wc: unknown, origin?: string) => boolean} [deps.isAppOrigin] - injectable for tests
 * @param {(...args: unknown[]) => void} [deps.onDeny] - injectable for tests
 * @returns {(wc: unknown, permission: string, callback: (granted: boolean) => void, details?: object) => void}
 */
function createPermissionRequestHandler(deps = {}) {
  const originOk = deps.isAppOrigin || isAppOrigin;
  const onDeny = deps.onDeny || logDeny;
  return function handlePermissionRequest(wc, permission, callback, details) {
    // The REQUEST handler has no origin string of its own; details may carry a
    // requesting URL on some Electron versions, so offer it as the fallback.
    const origin = details?.securityOrigin || details?.requestingUrl;
    const granted =
      permission === "media" && originOk(wc, origin) && !requestsVideo(details);
    if (!granted) onDeny("request", permission, wc, origin, details);
    return callback(granted);
  };
}

/**
 * Build the handler for session.setPermissionCheckHandler().
 *
 * Mirrors the request handler so a synchronous check cannot disagree with the
 * async grant — a disagreement is what made navigator.permissions.query()
 * report "granted" while getUserMedia failed. `details.mediaType` is the
 * singular sibling of `mediaTypes`; "video" is refused, anything else
 * (including an absent value) is allowed for the app origin.
 *
 * @param {object} [deps]
 * @param {(wc: unknown, origin?: string) => boolean} [deps.isAppOrigin] - injectable for tests
 * @param {(...args: unknown[]) => void} [deps.onDeny] - injectable for tests
 * @returns {(wc: unknown, permission: string, origin?: string, details?: object) => boolean}
 */
function createPermissionCheckHandler(deps = {}) {
  const originOk = deps.isAppOrigin || isAppOrigin;
  const onDeny = deps.onDeny || logDeny;
  return function handlePermissionCheck(wc, permission, origin, details) {
    const granted =
      permission === "media" &&
      originOk(wc, origin) &&
      details?.mediaType !== "video";
    if (!granted) onDeny("check", permission, wc, origin, details);
    return granted;
  };
}

module.exports = {
  isAppOrigin,
  requestsVideo,
  createPermissionRequestHandler,
  createPermissionCheckHandler,
};
