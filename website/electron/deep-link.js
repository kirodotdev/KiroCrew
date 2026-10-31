// kirocrew:// deep links for the Kiro Crew Electron app.
//
// Other programs on the machine (a companion app, a Slack button, a Raycast
// script, `kirocrew open`) use this scheme to open the desktop app on one
// session, or on one app's detail page, rather than only bring its window
// forward. A link carries the same target as the dashboard's own browser URL
// (`/chat?sid=<slot key>`, `/apps/detail/<name>`) and is delivered through the
// same `navigate` IPC the Crew Companion overlay's "Open session" uses inside
// the process.
//
// This module owns the one decision that matters for a scheme handler: which
// URLs are allowed to drive navigation. The string comes from the OS, which
// hands the app whatever any other program asked LaunchServices (or the
// Windows/Linux registration) to open, so it is untrusted input. The rule is an
// allowlist of three shapes -- raise the window, open one session, or show one
// app's detail page -- mirroring `external-scheme.js`'s stance on outbound
// schemes: a deny-by-default shape, never a pass-through.
//
// Electron-free on purpose (like display-media.js and external-scheme.js) so
// the parsing and the allowlist are unit-tested without a live app;
// deep-link-handler.js and main.js own the Electron wiring and the window raise.
"use strict";

/** The scheme this app registers. Also the value in package.json `build.protocols`. */
const SCHEME = "kirocrew";

/**
 * Pick the kirocrew:// URL out of a process argv, if one is there.
 *
 * On Windows and Linux the OS launches a SECOND instance with the URL as an
 * argument; Electron's `second-instance` event hands the primary instance that
 * argv. On macOS the URL arrives through `open-url` instead and argv carries
 * none, so this answers "" there. Only the LAST matching argument is taken: a
 * second URL on one command line is not a thing the OS produces, and taking
 * the first would let a crafted launch smuggle an earlier argument past a
 * caller that only looked at the end.
 *
 * @param {ReadonlyArray<string>} argv the raw argv (first entry is the binary).
 * @returns {string} the URL, or "" when none is present.
 */
function deepLinkFromArgv(argv) {
  if (!Array.isArray(argv)) return "";
  let found = "";
  for (const arg of argv) {
    if (typeof arg === "string" && arg.toLowerCase().startsWith(SCHEME + ":")) {
      found = arg;
    }
  }
  return found;
}

/**
 * Resolve a kirocrew:// URL to the dashboard route it may open.
 *
 * Exactly three forms are accepted:
 *
 *   kirocrew://                       -> { route: "" }           (raise the window)
 *   kirocrew://chat?sid=<slot key>    -> { route: "/chat?sid=<encoded key>", slotKey }
 *   kirocrew://apps/detail/<name>     -> { route: "/apps/detail/<name>" }   (one app's detail page)
 *
 * Anything else -- another scheme, an unknown host, an extra path segment, a
 * missing or malformed sid or app name, any other query parameter -- is refused
 * with a reason. The route is REBUILT from the parsed pieces, never copied from
 * the input, so the only characters that can reach `navigate` are the ones the
 * sid or name check admits. A token never rides a deep link: the desktop window
 * is already authenticated, and a credential in a URL that any program on the
 * machine can hand to LaunchServices would be a credential in every program's
 * reach.
 *
 * `sid` is passed through `encodeURIComponent` after a length and character
 * check: slot keys are the gateway's own identifiers (`cos-quick-chat`,
 * `chat-3-1785870239`, `slack_1712345678.123456`), so a conservative class is
 * enough and anything outside it is refused rather than escaped.
 *
 * The app shape names the detail page only. `/apps/<name>` is the app's own
 * page (the app's code runs there) and the store pages act on apps, so a program
 * outside the dashboard gets to point at the one page that merely describes an
 * app. That page takes its install, update and enable trigger from router
 * state, never from its URL (`AppDetailPage`'s `autoAction`), so the most a link
 * can do is show it: the click stays the user's. The name is checked against the
 * manifest's own app-name grammar (`manifest_names.KEBAB_RE`), which needs no
 * URL encoding, so a percent-encoded segment is refused rather than decoded.
 *
 * @param {string} raw the URL as the OS delivered it.
 * @returns {{ok: true, route: string, slotKey: string} | {ok: false, reason: string}}
 */
function resolveDeepLink(raw) {
  if (typeof raw !== "string" || !raw) return refuse("empty");
  let url;
  try {
    url = new URL(raw);
  } catch {
    return refuse("unparseable");
  }
  if (url.protocol !== SCHEME + ":") return refuse("wrong scheme");
  if (url.username || url.password || url.port) return refuse("credentials or port in URL");
  if (url.hash) return refuse("fragment not allowed");

  // `new URL("kirocrew://chat?sid=x")` puts "chat" in hostname and "" in
  // pathname. A bare `kirocrew://` (or `kirocrew:`) has an empty hostname.
  const host = url.hostname.toLowerCase();
  const segments = url.pathname.split("/").filter(Boolean);
  const params = [...url.searchParams.keys()];

  if (!host) {
    if (segments.length || params.length) return refuse("unknown route");
    return { ok: true, route: "", slotKey: "" };
  }

  if (host === "chat") {
    if (segments.length) return refuse("unknown route");
    if (params.length === 0) return refuse("missing sid");
    if (params.length !== 1 || params[0] !== "sid") return refuse("unknown query parameter");
    const sid = url.searchParams.get("sid") || "";
    if (!isSlotKey(sid)) return refuse("invalid sid");
    return { ok: true, route: `/chat?sid=${encodeURIComponent(sid)}`, slotKey: sid };
  }

  if (host === "apps") {
    if (segments.length !== 2 || segments[0] !== "detail") return refuse("unknown route");
    if (params.length) return refuse("unknown query parameter");
    const name = segments[1];
    if (!isAppName(name)) return refuse("invalid app name");
    return { ok: true, route: `/apps/detail/${encodeURIComponent(name)}`, slotKey: "" };
  }

  return refuse("unknown route");
}

function refuse(reason) {
  return { ok: false, reason };
}

// Gateway slot keys: letters, digits, and the separators the gateway itself
// uses. 200 is well above any real key and far below anything that could
// matter to a URL or an IPC payload. `cli_open.py` carries the same pattern.
const SLOT_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;

function isSlotKey(s) {
  return typeof s === "string" && SLOT_KEY_RE.test(s);
}

// App names: the manifest's kebab-case grammar, `KEBAB_RE` in
// src/kiro_crew/apps/manifest_names.py, which every door that admits an app
// enforces. The manifest sets no length cap, so one is applied here: 200 bounds
// what reaches the IPC payload, as the slot-key class does, and no real app
// name comes near it.
const APP_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const APP_NAME_MAX_LENGTH = 200;

function isAppName(s) {
  return typeof s === "string" && s.length <= APP_NAME_MAX_LENGTH && APP_NAME_RE.test(s);
}

module.exports = {
  SCHEME,
  deepLinkFromArgv,
  resolveDeepLink,
};
