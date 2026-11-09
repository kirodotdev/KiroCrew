// kirocrew:// deep links for the Kiro Crew Electron app.
//
// Other programs on the machine (a companion app, a Slack button, a Raycast
// script, `kirocrew open`, a web page listing the user's machines) use this
// scheme to open the desktop app on one session, on one app's detail page, or
// on Settings -> Remote Crew's add form filled in with one machine, rather than
// only bring its window forward. A link carries the same target as the
// dashboard's own browser URL (`/chat?sid=<slot key>`, `/apps/detail/<name>`,
// `/settings/instances`) and is delivered through the same `navigate` IPC the
// Crew Companion overlay's "Open session" uses inside the process.
//
// This module owns the one decision that matters for a scheme handler: which
// URLs are allowed to drive navigation. The string comes from the OS, which
// hands the app whatever any other program asked LaunchServices (or the
// Windows/Linux registration) to open, so it is untrusted input. The rule is an
// allowlist of five shapes -- raise the window, open one session, open one
// session on a remote crew, show one app's detail page, or fill in the Remote
// Crew add form -- mirroring
// `external-scheme.js`'s stance on outbound schemes: a deny-by-default shape,
// never a pass-through.
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
 * Exactly five forms are accepted:
 *
 *   kirocrew://                              -> { route: "" }           (raise the window)
 *   kirocrew://chat?sid=<slot key>           -> { route: "/chat?sid=<encoded key>", slotKey }
 *   kirocrew://chat?crew=<crew id>&sid=<key> -> { route: "/chat?crew=<id>&crewSid=<encoded key>", slotKey }
 *   kirocrew://apps/detail/<name>            -> { route: "/apps/detail/<name>" }   (one app's detail page)
 *   kirocrew://remote-crew/add?host=<ssh host>[&user=<u>][&port=<n>][&name=<label>]
 *                                            -> { route: "/settings/instances?host=<[u@]ssh host>[&port=<n>][&name=<label>]" }
 *                                               (the Remote Crew add form, filled in; see resolveRemoteCrewAdd)
 *
 * A crew link's route carries no `sid`, so an older dashboard never opens a
 * same-named LOCAL session. A `crew` that is not a crew id routes to the
 * value-free `/chat?crewError=1`, so the dashboard can say so.
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
    const crewLink = params.length === 2 && params.includes("crew") && params.includes("sid");
    if (!crewLink && (params.length !== 1 || params[0] !== "sid")) return refuse("unknown query parameter");
    const sid = url.searchParams.get("sid") || "";
    if (!crewLink) {
      if (!isSlotKey(sid)) return refuse("invalid sid");
      return { ok: true, route: `/chat?sid=${encodeURIComponent(sid)}`, slotKey: sid };
    }
    if (!CREW_SLOT_KEY_RE.test(sid)) return refuse("invalid sid");
    const crew = url.searchParams.get("crew") || "";
    if (!CREW_ID_RE.test(crew)) return { ok: true, route: "/chat?crewError=1", slotKey: "" };
    return { ok: true, route: `/chat?crew=${crew}&crewSid=${encodeURIComponent(sid)}`, slotKey: sid };
  }

  if (host === "apps") {
    if (segments.length !== 2 || segments[0] !== "detail") return refuse("unknown route");
    if (params.length) return refuse("unknown query parameter");
    const name = segments[1];
    if (!isAppName(name)) return refuse("invalid app name");
    return { ok: true, route: `/apps/detail/${encodeURIComponent(name)}`, slotKey: "" };
  }

  if (host === "remote-crew") return resolveRemoteCrewAdd(raw, segments, url.searchParams);

  return refuse("unknown route");
}

// A Remote Crew add link FILLS IN Settings -> Remote Crew's add form and does
// nothing else: nothing is saved, no tunnel opens and no command runs until the
// user reads the fields and presses Add. From that click on, the crew is one the
// user added by hand, so with Auto-connect crews on it connects once added. So
// whatever can make the OS open this URL can put in that form only text the
// user could have typed, under a line saying where it came from.
//
// The whole URL is capped before anything is read from it, each key may appear
// once, and an unknown key refuses the link instead of being dropped, so a link
// is taken whole or not at all.
const REMOTE_CREW_LINK_MAX_LENGTH = 8192;
const REMOTE_CREW_ADD_KEYS = ["host", "user", "port", "name"];

/**
 * `host` (required) and `user` (optional) become the form's SSH host, `user@host`
 * when both are given, and must pass `isSshHost`. `port` is the REMOTE GATEWAY's
 * port, the form's Remote port field; the port ssh dials comes from the user's
 * `~/.ssh/config`, as it does for a crew typed in by hand. `name` is the crew's
 * label. A link without `port` or `name` leaves that field as the form has it.
 *
 * @param {string} raw the URL as the OS delivered it
 * @param {string[]} segments its path segments
 * @param {URLSearchParams} query its query
 */
function resolveRemoteCrewAdd(raw, segments, query) {
  if (raw.length > REMOTE_CREW_LINK_MAX_LENGTH) return refuse("link too long");
  if (segments.length !== 1 || segments[0] !== "add") return refuse("unknown route");
  const seen = new Set();
  for (const key of query.keys()) {
    if (!REMOTE_CREW_ADD_KEYS.includes(key)) return refuse("unknown query parameter");
    if (seen.has(key)) return refuse("repeated query parameter");
    seen.add(key);
  }
  if (!seen.has("host")) return refuse("missing host");
  const user = query.get("user");
  const sshHost = user === null ? query.get("host") : `${user}@${query.get("host")}`;
  if (!isSshHost(sshHost)) return refuse("invalid host");
  const port = query.get("port");
  if (port !== null && !isGatewayPort(port)) return refuse("invalid port");
  const name = query.get("name");
  if (name !== null && !isCrewName(name)) return refuse("invalid name");
  let route = `/settings/instances?host=${encodeURIComponent(sshHost)}`;
  if (port !== null) route += `&port=${port}`;
  if (name !== null) route += `&name=${encodeURIComponent(name)}`;
  return { ok: true, route, slotKey: "" };
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

// A crew link's key is also stored as that crew's last-chat hint, whose store
// takes at most 128 characters (`isPaneSlotKey`, website/src/lib/paneLastSlot.ts),
// so the slot-key rule above is capped there. A test pins both.
const CREW_SLOT_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
// A crew id as the instances registry mints it (`_ID_RE`,
// src/kiro_crew/instances/registry.py; a test pins the two).
const CREW_ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

// App names: the manifest's kebab-case grammar, `KEBAB_RE` in
// src/kiro_crew/apps/manifest_names.py, which every door that admits an app
// enforces. It is checked here with plain logic rather than a regex, because
// the name arrives from outside the process: one or more parts joined by
// single dashes, each part made only of `a`-`z` and `0`-`9`. The length cap
// is the manifest's own `APP_NAME_MAX_LENGTH` (manifest_names.py), so a name
// no manifest can carry is refused here too; test/test_cli_open.py keeps the
// two numbers equal.
// test/fixtures/app-name-samples.json holds the names both sides are tested on.
const APP_NAME_MAX_LENGTH = 128;
const APP_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789";

function isAppName(s) {
  if (typeof s !== "string" || s.length === 0 || s.length > APP_NAME_MAX_LENGTH) return false;
  for (const part of s.split("-")) {
    if (part.length === 0) return false;
    for (const ch of part) {
      if (!APP_NAME_CHARS.includes(ch)) return false;
    }
  }
  return true;
}

// SSH hosts: what `validate_ssh_host` (src/kiro_crew/instances/validation.py)
// accepts, the check the gateway runs before it puts a host in an ssh command
// line. A host, an FQDN, an ssh-config alias or `user@host`: at most one `@`,
// each part starting with a letter, digit or underscore and holding only those,
// `.` and `-`, so no part can be read as an ssh option. The gateway strips
// surrounding whitespace first; a link carrying any is refused instead.
// test/fixtures/ssh-host-samples.json holds the hosts both sides are tested on,
// and test/test_cli_open.py keeps SSH_HOST_MAX_LENGTH equal to the gateway's.
const SSH_HOST_MAX_LENGTH = 255;
const SSH_HOST_PART_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]*$/;

function isSshHost(s) {
  if (typeof s !== "string" || s.length === 0 || s.length > SSH_HOST_MAX_LENGTH) return false;
  const parts = s.split("@");
  return parts.length <= 2 && parts.every((part) => SSH_HOST_PART_RE.test(part));
}

// A TCP port, written without a sign or leading zero.
function isGatewayPort(s) {
  return /^[1-9][0-9]{0,4}$/.test(s) && Number(s) <= 65535;
}

// Crew labels: not blank, at most the registry's `INSTANCE_NAME_MAX` characters
// (src/kiro_crew/instances/registry.py, counted in code points as Python does;
// test/test_cli_open.py keeps the two equal), and no control or invisible
// formatting character, so the label the user reads in the form is the text it
// holds.
const CREW_NAME_MAX_LENGTH = 200;
const CONTROL_OR_FORMAT_RE = /[\p{Cc}\p{Cf}]/u;

function isCrewName(s) {
  return (
    typeof s === "string" &&
    s.trim().length > 0 &&
    Array.from(s).length <= CREW_NAME_MAX_LENGTH &&
    !CONTROL_OR_FORMAT_RE.test(s)
  );
}

module.exports = {
  SCHEME,
  deepLinkFromArgv,
  isAppName,
  isCrewName,
  isSshHost,
  resolveDeepLink,
};
