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
 * Page permissions granted to the trusted dashboard with no OS (TCC) leg.
 *
 * Both members share one shape: the dashboard's own code requests them, they
 * claim no OS resource Electron must broker (so they are answered before the
 * media path rather than folded into its macOS machinery), and denying them is
 * SILENT in the renderer — the exact failure mode this module's header
 * documents for the microphone. The trust gate applies to both: an untrusted
 * page in the embedded browser view is refused regardless of origin.
 *
 * - `fullscreen`: Electron routes a DOM `element.requestFullscreen()` through
 *   these handlers as `permission === "fullscreen"`. Before it was granted,
 *   the fullscreen button on an inline `<video>` (a file card, the media
 *   viewer) did nothing at all when clicked. Denied to untrusted views because
 *   a third-party page owning the whole screen can repaint it as a convincing
 *   fake of this app.
 *
 * - `notifications`: the dashboard fires page-context `new Notification()` for
 *   new unacked notifications (useNativeNotification.ts) and approval requests
 *   (useWebSocket.ts). In a plain browser Chromium prompts and grants, so
 *   Chrome-tab users got native OS toasts; under this handler's blanket deny,
 *   `Notification.permission` was pinned to 'denied' and the SAME code no-oped
 *   silently in the packaged app — the one surface where OS notifications are
 *   most expected. Granting here makes Electron render those constructor calls
 *   as real OS notifications with zero renderer changes. Denied to untrusted
 *   views so a browsed page cannot post OS toasts wearing this app's identity.
 *
 * This set exists because it now has TWO consumers with identical treatment.
 * Capabilities routed through the same handler with NO consumer in this
 * product (`pointerLock`, `keyboardLock`) stay out: widening a permission set
 * without a consumer is how a security default quietly erodes.
 */
const TRUSTED_PAGE_PERMISSIONS = new Set(["fullscreen", "notifications"]);

/**
 * Trusted-page permissions that are additionally restricted to the MAIN frame.
 *
 * The trust/origin gate above identifies the dashboard's webContents, but a
 * subframe shares its parent's webContents — and `isAppOrigin` treats any
 * localhost origin as the app. Without a frame check, an iframe the dashboard
 * embeds (a scripted localhost preview, a widget) would inherit the grant and
 * could post OS notifications wearing this app's identity. `notifications` is
 * therefore granted only when `details.isMainFrame` is exactly true —
 * fail-closed when the field is absent, since only Electron's real permission
 * callbacks (which always carry it) should ever be granted.
 *
 * `fullscreen` stays frame-agnostic deliberately: an inline <video> inside an
 * embedded player frame legitimately requests fullscreen, that was the
 * pre-existing granted behavior this change must not regress, and a fullscreen
 * takeover is visible and user-initiated where a forged notification is not.
 */
const MAIN_FRAME_ONLY_PERMISSIONS = new Set(["notifications"]);

/** Frame gate for MAIN_FRAME_ONLY_PERMISSIONS; true for everything else. */
function frameOk(permission, details) {
  return (
    !MAIN_FRAME_ONLY_PERMISSIONS.has(permission) ||
    details?.isMainFrame === true
  );
}

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

/**
 * Is this denial worth a breadcrumb, or is it a by-design permanent refusal?
 *
 * Chromium fires the CHECK handler on every navigation for permissions this app
 * denies wholesale — `geolocation`, `web-app-installation`, `background-sync`,
 * and `media` with `mediaType:"video"` (the camera, which is deliberately
 * absent). Logging each one floods the main-process console with dozens of
 * lines per session that carry ZERO diagnostic value: they are always denied and
 * always will be.
 *
 * The ONE denial worth seeing is the class this module exists to catch — a
 * `media` request that should have been GRANTED (audio or unspecified) but was
 * refused, i.e. the silent-mic regression documented above. In healthy
 * operation this never fires for the app origin (such a request is granted), so
 * a line here is a real signal, not noise. Video is by-design and is checked on
 * every route, so it is not noteworthy either.
 *
 * @param {string} permission
 * @param {{ mediaType?: string, mediaTypes?: Array<string> }} [details]
 * @param {boolean} [isVideo] - precomputed video verdict (request-handler side)
 * @returns {boolean}
 */
function isNoteworthyDenial(permission, details, isVideo) {
  if (permission !== "media") return false;
  const video = isVideo ?? (details?.mediaType === "video" || requestsVideo(details));
  return !video;
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
 * Grants `media` for the app origin unless video is explicitly requested, plus
 * the TRUSTED_PAGE_PERMISSIONS set (fullscreen, notifications). Denies every
 * other permission type (geolocation, clipboard, MIDI, …).
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
    //
    // Non-OS page capabilities (fullscreen, notifications) are answered HERE,
    // before the media rule: they need the same trust gate but none of the
    // macOS TCC machinery below, and letting one fall through would put a
    // fullscreen click or a notification behind a microphone prompt. Logged
    // unconditionally on refusal rather than via isNoteworthyDenial: this
    // branch only runs on a real request (a user gesture or an explicit
    // Notification.requestPermission()), never on Chromium's per-navigation
    // re-checks, so it cannot become console noise.
    if (TRUSTED_PAGE_PERMISSIONS.has(permission)) {
      const allowed =
        frameOk(permission, details) && !isUntrusted(wc) && originOk(wc, origin);
      if (!allowed) audit("request", permission, wc, origin, details);
      return callback(allowed);
    }
    const granted =
      !isUntrusted(wc) &&
      permission === "media" &&
      originOk(wc, origin) &&
      !requestsVideo(details);
    if (!granted) {
      if (isNoteworthyDenial(permission, details)) {
        audit("request", permission, wc, origin, details);
      }
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
    //
    // This branch mirrors the request handler's for the reason in this
    // module's header: a check that disagrees with the async grant is exactly
    // what made permissions.query() report one verdict while the real request
    // took another. For notifications the check IS the user-visible surface:
    // the renderer reads `Notification.permission` (this handler's verdict)
    // and never constructs a toast unless it says 'granted'.
    if (TRUSTED_PAGE_PERMISSIONS.has(permission)) {
      return (
        frameOk(permission, details) && !isUntrusted(wc) && originOk(wc, origin)
      );
    }
    const granted =
      !isUntrusted(wc) &&
      permission === "media" &&
      originOk(wc, origin) &&
      details?.mediaType !== "video";
    // Guarded for the same reason as the request handler's `audit`: this is a
    // synchronous Electron callback, so a throwing breadcrumb would propagate
    // into Chromium's permission check instead of just failing to log. Only a
    // noteworthy denial is logged — the by-design refusals Chromium re-checks on
    // every navigation are silenced (see isNoteworthyDenial).
    if (!granted && isNoteworthyDenial(permission, details)) {
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
  isNoteworthyDenial,
  createPermissionRequestHandler,
  createPermissionCheckHandler,
};
