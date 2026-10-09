"use strict";

// Integrated authentication (Kerberos / Negotiate, NTLM) for the app's web
// contents, opt-in per EDITION at build time.
//
// Chromium answers a server's `WWW-Authenticate: Negotiate` challenge only for
// hosts on an allowlist; with none set, an embedded page on a Kerberos-gated
// intranet gets a 401 even when the OS holds a valid ticket. Electron takes that
// list as a command-line switch (ELECTRON_SWITCH below, the name Electron
// documents and the one verified to fix a Browser panel 401), which must be set
// before app ready, so a Dock or Start-menu launch -- which passes
// no arguments -- can only get it from code inside the app.
//
// The public build bakes nothing and appends nothing. An edition that ships to a
// managed intranet sets KIROCREW_AUTH_SERVER_ALLOWLIST when it runs
// packaging/build-desktop.sh, which validates it with parseAuthServerAllowlist
// below and stages it as AUTH-SERVER-ALLOWLIST next to this module; package.json
// `files` packs it into app.asar, so it is trusted as code is trusted (the same
// shape as the baked EXTERNALLY-MANAGED marker in auto-update.js).
//
// Credential DELEGATION (Electron's separate negotiate-delegate switch,
// forwarding the user's ticket to the server) is deliberately never set here: answering the
// challenge is all an intranet page needs, and delegation would let every
// allowlisted server act as the user elsewhere.
//
// The switch is process-wide: every web contents answers for these hosts,
// including a Browser panel page the AGENT drives. An agent (or untrusted
// content steering it) that navigates the panel to an allowlisted host gets a
// page authenticated as the user, with no cookie or prior sign-in. That is the
// point for an intranet edition, and it is why the list belongs to the edition
// that knows which hosts are acceptable, never to core.

const fs = require("fs");
const path = require("path");

const BAKED_ALLOWLIST_NAME = "AUTH-SERVER-ALLOWLIST";
const ALLOWLIST_MAX_BYTES = 2048;
const ALLOWLIST_MAX_ENTRIES = 64;
const ELECTRON_SWITCH = "auth-server-whitelist"; // wokeignore:rule=whitelist

// One entry: an exact host (`intranet.example.org`) or a suffix pattern
// (`*.example.com`). Labels are DNS-shaped; a lone `*`, a scheme, a path, a port
// or an inner wildcard is refused, so a typo can never widen the list to every
// host on the internet.
const LABEL = "[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?";
const ENTRY_RE = new RegExp(`^(?:\\*\\.)?${LABEL}(?:\\.${LABEL})+$`);

/**
 * Validate and normalize a comma-separated allowlist.
 * @param {unknown} text
 * @returns {string|null} the entries joined by "," or null when any entry is invalid
 */
function parseAuthServerAllowlist(text) {
  if (typeof text !== "string") return null;
  const entries = text.trim().split(",").map((e) => e.trim());
  if (entries.length === 0 || entries.length > ALLOWLIST_MAX_ENTRIES) return null;
  if (!entries.every((e) => ENTRY_RE.test(e))) return null;
  return entries.join(",");
}

/**
 * Read the allowlist baked beside this module, if any. Fails soft: an absent,
 * oversized, non-regular or malformed file yields null and the app launches
 * without integrated auth rather than not at all.
 * @param {object} [o]
 * @param {string} [o.bakedPath]
 * @param {(msg: string) => void} [o.log]
 * @returns {string|null}
 */
function readBakedAuthServerAllowlist({
  bakedPath = path.join(__dirname, BAKED_ALLOWLIST_NAME),
  log = () => {},
} = {}) {
  let st;
  try {
    st = fs.lstatSync(bakedPath);
  } catch {
    return null;
  }
  if (!st.isFile()) {
    log(`[auth-allowlist] ignoring ${bakedPath}: not a regular file`);
    return null;
  }
  if (st.size > ALLOWLIST_MAX_BYTES) {
    log(`[auth-allowlist] ignoring ${bakedPath}: ${st.size} bytes exceeds ${ALLOWLIST_MAX_BYTES}`);
    return null;
  }
  let text;
  try {
    text = fs.readFileSync(bakedPath, "utf8");
  } catch (e) {
    log(`[auth-allowlist] ignoring ${bakedPath}: ${e.message}`);
    return null;
  }
  const value = parseAuthServerAllowlist(text);
  if (value === null) log(`[auth-allowlist] ignoring ${bakedPath}: malformed allowlist`);
  return value;
}

/**
 * Append Electron's integrated-auth switch when the edition baked an allowlist.
 * Must run before app ready.
 * @param {object} deps
 * @param {(name: string, value: string) => void} deps.appendSwitch
 * @param {string} [deps.bakedPath]
 * @param {(msg: string) => void} [deps.log]
 * @returns {string|null} the applied allowlist, or null when none was applied
 */
function applyAuthServerAllowlist({ appendSwitch, bakedPath, log = () => {} }) {
  const value = readBakedAuthServerAllowlist({ bakedPath, log });
  if (value === null) return null;
  appendSwitch(ELECTRON_SWITCH, value);
  log(`[auth-allowlist] integrated auth enabled for ${value}`);
  return value;
}

module.exports = {
  BAKED_ALLOWLIST_NAME,
  ALLOWLIST_MAX_BYTES,
  parseAuthServerAllowlist,
  readBakedAuthServerAllowlist,
  applyAuthServerAllowlist,
};
