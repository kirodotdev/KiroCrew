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
 * ── The macOS (TCC) leg ──────────────────────────────────────────────────────
 *
 * Granting at the Electron layer is necessary but NOT sufficient on macOS: the
 * OS gates the mic separately, and its prompt is ONE-SHOT — once a user is
 * 'denied', macOS never asks again, so a bare deny is a permanent dead end
 * ("permission denied" with no prompt and nothing obvious to click). This
 * handler therefore mirrors display-media.js's screen-capture treatment:
 *
 *   - 'granted'         -> grant.
 *   - 'not-determined'  -> ask macOS NOW (the user just clicked the mic, so the
 *                          prompt is in-context) and honor the answer.
 *   - 'denied'/'restricted' -> deny AND surface a recovery route to System
 *                          Settings; the OS will not re-prompt on its own.
 *
 * The TCC leg is OPT-IN via injected deps: with no `getMicAccessStatus` the
 * handler stays fully synchronous and behaves exactly as before (non-darwin
 * platforms and unit tests take that path).
 *
 * @param {object} [deps]
 * @param {(wc: unknown, origin?: string) => boolean} [deps.isAppOrigin] - injectable for tests
 * @param {(...args: unknown[]) => void} [deps.onDeny] - injectable for tests
 * @param {() => string} [deps.getMicAccessStatus] - systemPreferences.getMediaAccessStatus('microphone')
 * @param {() => Promise<boolean>} [deps.askForMicAccess] - systemPreferences.askForMediaAccess('microphone')
 * @param {(reason: string) => void} [deps.onMicBlocked] - surfaced when macOS will not re-prompt
 * @returns {(wc: unknown, permission: string, callback: (granted: boolean) => void, details?: object) => void}
 */
function createPermissionRequestHandler(deps = {}) {
  const originOk = deps.isAppOrigin || isAppOrigin;
  const onDeny = deps.onDeny || logDeny;
  const isUntrusted = deps.isUntrusted || (() => false);
  const getMicAccessStatus = deps.getMicAccessStatus;
  const askForMicAccess = deps.askForMicAccess;
  const onMicBlocked = deps.onMicBlocked || (() => {});
  // Audit sinks are OBSERVERS: they must never be able to change a verdict or
  // strand a request. Both are real hazards, not hypotheticals — the default
  // logDeny JSON.stringify()s an Electron-supplied `details`, which throws on a
  // circular structure or a throwing getter, and onMicBlocked opens a real
  // dialog. An escaping throw here would leave the renderer's getUserMedia
  // promise unsettled FOREVER, the exact silent dead end this module exists to
  // prevent. Wrapping once, at the source, is what keeps every call site safe.
  const audit = (...args) => {
    try {
      onDeny(...args);
    } catch {
      /* breadcrumb is best effort — never let logging decide a permission */
    }
  };
  return function handlePermissionRequest(wc, permission, callback, details) {
    // The REQUEST handler has no origin string of its own; details may carry a
    // requesting URL on some Electron versions, so offer it as the fallback.
    const origin = details?.securityOrigin || details?.requestingUrl;
    // Untrusted first, by IDENTITY, before any origin heuristic. The embedded
    // browser view shares this session, and `isAppOrigin` treats ANY localhost
    // origin as the app — so browsing to `http://localhost:<anything>` would
    // otherwise inherit the dashboard's microphone grant. A page never gets a
    // capability just for being served from loopback.
    const granted =
      !isUntrusted(wc) &&
      permission === "media" &&
      originOk(wc, origin) &&
      !requestsVideo(details);
    if (!granted) {
      audit("request", permission, wc, origin, details);
      return callback(false);
    }
    // Electron says yes; on macOS the OS still has to. Absent the TCC deps
    // (non-darwin, tests) this stays synchronous — the original behavior.
    if (typeof getMicAccessStatus !== "function") return callback(true);

    let status;
    try {
      status = getMicAccessStatus();
    } catch {
      // Probing the OS must never be what breaks the mic: fail OPEN and let
      // getUserMedia surface whatever the OS decides.
      return callback(true);
    }
    if (status === "granted") return callback(true);
    if (status === "denied" || status === "restricted") {
      audit("request", `${permission}:tcc-${status}`, wc, origin, details);
      try {
        onMicBlocked(status);
      } catch {
        /* recovery UI is best effort — see the `audit` note above */
      }
      return callback(false);
    }
    // 'not-determined' (or anything unrecognized): ask, in context.
    if (typeof askForMicAccess !== "function") return callback(true);
    Promise.resolve()
      .then(askForMicAccess)
      // Fail open ONLY on a rejection from askForMicAccess itself (older macOS,
      // missing API). This must be settled BEFORE the sinks below run: with a
      // single trailing .catch, anything thrown by onDeny or callback would be
      // caught downstream and answered with a SECOND callback(true) — inverting
      // a user's explicit refusal into a grant, and double-invoking a callback
      // Electron only permits once.
      .then(
        (ok) => Boolean(ok),
        () => true,
      )
      .then((ok) => {
        if (!ok) audit("request", `${permission}:tcc-refused`, wc, origin, details);
        callback(ok);
      });
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
  const isUntrusted = deps.isUntrusted || (() => false);
  return function handlePermissionCheck(wc, permission, origin, details) {
    // Mirrors the request handler's untrusted-first rule; see the note there.
    // Note this handler may receive a null `wc`, in which case identity is
    // unavailable — but a check alone grants no capability, and the REQUEST
    // handler (which does the actual granting) is always frame-originated and
    // therefore always has a webContents to match.
    const granted =
      !isUntrusted(wc) &&
      permission === "media" &&
      originOk(wc, origin) &&
      details?.mediaType !== "video";
    // Guarded for the same reason as the request handler's `audit`: this is a
    // synchronous Electron callback, so a throwing breadcrumb would propagate
    // into Chromium's permission check instead of just failing to log.
    if (!granted) {
      try {
        onDeny("check", permission, wc, origin, details);
      } catch {
        /* breadcrumb is best effort — never let logging decide a permission */
      }
    }
    return granted;
  };
}

module.exports = {
  isAppOrigin,
  requestsVideo,
  createPermissionRequestHandler,
  createPermissionCheckHandler,
};
