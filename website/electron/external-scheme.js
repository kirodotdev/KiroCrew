// External-scheme hand-off for the KiroCrew Electron app.
//
// Why this exists: Settings -> Computer Use offers "Open System Settings"
// shortcuts that target macOS System Settings deep links
// (`x-apple.systempreferences:…`). In the packaged desktop app those buttons
// were dead clicks, for two compounding reasons:
//
//  1. The dashboard renders as a FRAME (InstancesViewport mounts each instance's
//     dashboard in an <iframe>). A frame navigation is governed by the CSP
//     `frame-src` directive, which the dashboard declares explicitly as
//     `frame-src 'self' blob: https://*.cloudfront.net …` (server.py `_BASE_CSP`)
//     — a loopback/cloudfront allowlist that names no custom scheme. So
//     assigning `window.location.href = 'x-apple…'` inside the pane is refused
//     with ERR_BLOCKED_BY_CSP before it ever reaches the OS. (The same
//     assignment works from a TOP-LEVEL page, which is why this survived
//     browser-tab testing.)
//  2. Routing it through `window.open()` instead makes it a new top-level
//     request, which DOES reach `setWindowOpenHandler` — but that handler allowed
//     any SAME-ORIGIN URL in-app, forwarded cross-origin `http:`/`https:` to the
//     browser, and silently denied every other scheme, so the deep link died
//     there too.
//
// This module owns the scheme decision so it is unit-testable without a live
// Electron process, mirroring display-media.js. The renderer half of the fix
// (window.open instead of location.href) lives in ComputerUsePanel.tsx.
"use strict";

// Non-web URLs handed to the OS. Deliberately an EXACT-MATCH allowlist of whole
// URLs, not a scheme allowlist: the string comes from the renderer and reaches
// shell.openExternal, which launches an external application.
//
// Whole-URL matching, because the product needs exactly these two targets and a
// scheme-granular rule would admit unbounded attacker-chosen payloads into
// LaunchServices. That is reachable, not theoretical: LLM-authored widget and
// artifact content renders in iframes carrying
// `sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"`
// (WidgetFrame, ArtifactBody, ArtifactsPage), no CSP directive constrains a
// `window.open` target's scheme, and remote-instance dashboard frames share this
// same handler. A scheme-only rule would let model-generated JS pop ANY System
// Settings pane — e.g. Sharing -> Remote Login, or Configuration Profiles — next
// to agent-authored text telling the user to enable it.
//
// Matching the raw string also removes a parser-differential class: the verdict
// is computed from `new URL(...)` (WHATWG: lowercases the scheme, strips tabs and
// newlines inside it, resolves `..`) while the RAW string is what gets forwarded
// and re-parsed by NSURL/CFURL. Requiring raw equality means validated and
// forwarded values cannot diverge.
//
// `file:` is absent by construction — handing an arbitrary local path to the OS
// is a local-file disclosure/execution vector, not a navigation. Web schemes are
// handled separately (see classifyNavigation) because they may also be
// same-origin, which stays in-app.
//
// MUST stay byte-identical to the backend's SETTINGS_URL_* constants
// (`computer_use/permissions.py`) and their frontend mirrors
// (`PANE_*` in ComputerUsePanel.tsx). A drift here is a dead button, not a
// security hole — the panel's own test pins the frontend pair.
const EXTERNAL_URLS = Object.freeze([
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
]);

// Web schemes the dashboard itself may navigate to.
const WEB_SCHEMES = Object.freeze(["http:", "https:"]);

// Schemes that INHERIT the creating page's origin, so a same-origin check is
// meaningful for them. `blob:` is the load-bearing case: WidgetFrame's "Open in
// new tab" builds a wrapper document with URL.createObjectURL and opens it, and
// `new URL('blob:http://host/uuid').origin` is `http://host` — the inner origin.
// Such a URL must stay IN-APP (it is the dashboard's own content and needs a real
// window object); it must never be handed to the OS, which is why these are
// checked for same-origin only and never appear in EXTERNAL_SCHEMES.
const ORIGIN_INHERITING_SCHEMES = Object.freeze(["blob:"]);

/**
 * Classify a URL the renderer asked to open.
 *
 * Returns one of:
 *  - "allow"    — same-origin: let the WebContents handle it in-app.
 *  - "external" — hand to the OS / default browser.
 *  - "block"    — deny, and do nothing (unknown or unsafe scheme).
 *
 * SAME-ORIGIN IS CHECKED FIRST, deliberately. The handler this replaced tested
 * origin before protocol, and `blob:` inherits the creating page's origin — so a
 * protocol-first ordering would demote the dashboard's own blob popouts
 * (WidgetFrame "Open in new tab") from `allow` to `block` and make that button a
 * dead click in the packaged app. Only schemes that genuinely carry an inner
 * origin participate; a same-origin verdict can only ever be `allow`, so this
 * never widens what reaches `shell.openExternal`.
 *
 * Fails closed: an unparseable URL is "block".
 *
 * @param {string} url the requested target
 * @param {string} appOrigin the dashboard's own origin (e.g. http://127.0.0.1:6777)
 * @returns {"allow"|"external"|"block"}
 */
function classifyNavigation(url, appOrigin) {
  let target;
  try {
    target = new URL(url);
  } catch {
    return "block";
  }
  const isWeb = WEB_SCHEMES.includes(target.protocol);
  if (isWeb || ORIGIN_INHERITING_SCHEMES.includes(target.protocol)) {
    let sameOrigin = false;
    try {
      const appOriginValue = new URL(appOrigin).origin;
      // Reject OPAQUE origins before comparing. `new URL()` succeeds for any
      // non-special scheme (`file:`, `data:`, `blob:null/…`) and yields the
      // literal string "null", which compares EQUAL to an opaque target origin —
      // so the catch below never fires and a foreign document would be treated
      // as same-origin. Not reachable today (getAppOrigin is always
      // `http://localhost:<port>`), but the invariant must hold for any caller.
      sameOrigin = appOriginValue !== "null" && target.origin === appOriginValue;
    } catch {
      // An unusable appOrigin must never PROMOTE a cross-origin URL to
      // same-origin (that would open it in-app); treat it as external instead.
      sameOrigin = false;
    }
    if (sameOrigin) return "allow";
    // Cross-origin web URLs open in the real browser. A cross-origin
    // origin-inheriting URL (a blob from some other origin) is NOT ours and is
    // not an OS hand-off either — block it.
    return isWeb ? "external" : "block";
  }
  // Non-web: exact whole-URL match only. Compared against the RAW string, so the
  // value validated here is byte-identical to the one forwarded to the OS.
  return EXTERNAL_URLS.includes(url) ? "external" : "block";
}

/**
 * Hand a URL to the OS, swallowing both failure shapes.
 *
 * `shell.openExternal` returns a Promise that REJECTS when the OS has no
 * handler for the scheme, so a bare call would surface as an unhandledRejection
 * rather than being caught by a synchronous try/catch. A failed hand-off is a
 * cosmetic problem — never a reason to disturb the main process.
 *
 * @param {(url: string) => Promise<void>|void} openExternal shell.openExternal
 * @param {string} url the target
 * @param {(msg: string) => void} log diagnostic sink
 */
function openExternalSafely(openExternal, url, log) {
  // A throwing sink must not become the failure. In the async branch below it
  // would otherwise surface as an unhandledRejection — the very thing this
  // helper exists to prevent.
  const note = (msg) => {
    try {
      if (log) log(msg);
    } catch {
      /* a diagnostic must never be load-bearing */
    }
  };
  try {
    const pending = openExternal(url);
    if (pending && typeof pending.catch === "function") {
      pending.catch((err) =>
        note(`openExternal rejected: ${err && err.message ? err.message : err}`),
      );
    }
  } catch (err) {
    note(`openExternal threw: ${err && err.message ? err.message : err}`);
  }
}

/**
 * Build the `setWindowOpenHandler` callback.
 *
 * Same-origin targets open in-app; allowlisted external schemes and
 * cross-origin web URLs are handed to the OS and the in-app window is denied
 * (so no blank popup is left behind); everything else is denied outright.
 *
 * @param {object} deps
 * @param {(url: string) => Promise<void>|void} deps.openExternal shell.openExternal (REQUIRED)
 * @param {() => string} deps.getAppOrigin resolves the dashboard origin (REQUIRED)
 * @param {(msg: string) => void} [deps.log] diagnostic sink
 * @returns {(details: {url: string}) => {action: "allow"|"deny"}}
 */
function createWindowOpenHandler(deps) {
  if (!deps || typeof deps.openExternal !== "function") {
    throw new Error("openExternal is required");
  }
  if (typeof deps.getAppOrigin !== "function") {
    throw new Error("getAppOrigin is required");
  }
  const log = deps.log || (() => {});

  // The WHOLE body is guarded, not just the classification: a throwing `log`
  // sink (a closed stdout / EPIPE) or a hostile `url` getter would otherwise
  // escape into Electron's setWindowOpenHandler. Fail closed to `deny`.
  return function handleWindowOpen(details) {
    try {
      const url = details && details.url;
      const verdict = classifyNavigation(url, deps.getAppOrigin());
      if (verdict === "allow") return { action: "allow" };
      if (verdict === "external") {
        openExternalSafely(deps.openExternal, url, log);
      } else {
        log(`denied window.open for unsupported target: ${String(url).slice(0, 60)}`);
      }
    } catch {
      /* fall through to deny */
    }
    return { action: "deny" };
  };
}

module.exports = {
  EXTERNAL_URLS,
  ORIGIN_INHERITING_SCHEMES,
  WEB_SCHEMES,
  classifyNavigation,
  createWindowOpenHandler,
  openExternalSafely,
};
