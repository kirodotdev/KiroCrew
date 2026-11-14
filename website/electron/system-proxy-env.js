"use strict";

const path = require("path");
const { windowsSystemRoot } = require("./find-bin");

// Recover the system HTTP(S) proxy on macOS for the gateway spawn.
//
// A GUI-launched .app inherits launchd's minimal environment rather than a
// login shell's (mac-env.js fixes the PATH twin of this problem). Shell-profile
// exports such as `export HTTPS_PROXY=...` therefore never reach the gateway,
// so a host that reaches the internet only through a proxy runs the gateway —
// and every kiro-cli child it spawns — with no `HTTP_PROXY`/`HTTPS_PROXY` at
// all. Those variables are the only proxy configuration the bundled binary's
// HTTP stack honours, so the child tries to reach the model backend directly
// and every turn fails as a "transient error (HTTP 5xx)" while the same
// machine's terminal kiro-cli works (issue #19821).
//
// macOS keeps the system-wide proxy in the Network configuration store, which
// `scutil --proxy` prints as a plain dictionary. GUI applications read that
// store directly, so reading it restores parity with them — and it is
// authoritative on managed (MDM) Macs whose policy re-evaluates there, where
// no shell profile ever mentions a proxy. Discovery is a gap-filler, never an
// override: gateway-env.js calls it only when the gateway environment carries
// none of the standard proxy variables, however they reached the app.
//
// Windows keeps its system proxy (Settings > Network > Proxy) in the per-user
// WinINet registry key, not in the environment, so a desktop launch reaches
// the gateway with no proxy variable either. Python's urllib falls back to
// that key on its own, but slack_sdk and the Telegram client read the
// environment only, so without discovery one channel's websocket and HTTP
// legs take different routes. The Windows reader below fills the same gap
// from `reg query` output. A Linux desktop session starts from the session
// environment the app already inherits, so Linux keeps passing it through.
//
// Sits beside mac-env.js so it can be unit-tested without Electron: the one
// system call is injected, and the parser is pure.

// Absolute path, never a bare name — the same rule mac-env.js applies to
// launchctl, and cheap insurance against a shuffled PATH.
const SCUTIL = "/usr/sbin/scutil";

// `scutil --proxy` is a local configd round-trip and returns effectively
// instantly. The timeout and the size bound exist so a wedged configd or a
// pathological dump cannot hang the spawn path or slurp memory — never because
// a slow read is expected. On any failure the answer is "nothing discovered":
// the spawn falls back to the inherited environment, the documented behaviour
// before this module existed.
const READ_TIMEOUT_MS = 2000;
const MAX_READ_BYTES = 64 * 1024;

// The loopback must never be proxied: loopback_http.py documents why
// (Kiro Crew's own requests carry the X-Internal-Secret, and urllib has no
// implicit loopback exemption). The macOS default ExceptionsList names
// `*.local` and `169.254/16` but NOT localhost, so discovery adds the bypass
// itself instead of trusting the store to have it.
const LOOPBACK_BYPASS = ["localhost", "127.0.0.1", "::1"];

/** Characters no legitimate scutil value carries; rejects mangled entries. */
const UNSAFE_ENTRY = /[\s,{}<>"'\\]/;

/**
 * Rewrite one macOS exception into the form environment-variable matchers read.
 *
 * Once a proxy variable is set, Python's urllib/requests and most other HTTP
 * stacks stop asking the system store and match `NO_PROXY` as a host suffix.
 * macOS writes wildcards as `*.local`; a suffix matcher reads `.local`. A CIDR
 * entry such as `169.254/16` matches no host there, so it is dropped rather
 * than passed on as a no-op that looks like a bypass.
 *
 * @param {string} entry
 * @returns {string | null}
 */
function toNoProxyEntry(entry) {
  if (entry.includes("/")) return null;
  const host = entry.startsWith("*.") ? entry.slice(1) : entry;
  return host === "" || host === "." || host.includes("*") ? null : host;
}

/**
 * True when a macOS exception is a wildcard a NO_PROXY suffix cannot express.
 *
 * Only a leading `*.` maps onto a suffix. Any other wildcard (`*`, `*.*`,
 * `10.*`, `host*.corp`) bypasses hosts that NO_PROXY would send through the
 * proxy, and once a proxy variable is set the system's own matcher is no
 * longer asked. Exporting a narrower bypass than the system's would route
 * direct-only traffic through the proxy, so discovery gives up instead and
 * the inherited environment stands.
 *
 * @param {string} entry
 * @returns {boolean}
 */
function isUnrepresentableWildcard(entry) {
  if (!entry.includes("*")) return false;
  const rest = entry.startsWith("*.") ? entry.slice(2) : entry;
  return rest === "" || rest.includes("*");
}

// macOS ships `169.254/16` in every default ExceptionsList. It is the one CIDR
// entry dropped instead of stopping discovery: link-local hosts sit on the
// local segment, the gateway has no link-local destination on a Mac, and
// refusing it would turn discovery off on nearly every Mac (issue #19821).
const DEFAULT_LINK_LOCAL_BYPASS = "169.254/16";

/**
 * True when a macOS exception is a CIDR range NO_PROXY cannot express.
 *
 * Python's urllib matches `NO_PROXY` by host name only, while the macOS
 * matcher it replaces resolves the host and checks the range. A `10/8`
 * bypass dropped from the export would send private-subnet traffic through
 * the proxy, so any CIDR entry other than the default link-local one stops
 * discovery.
 *
 * @param {string} entry
 * @returns {boolean}
 */
function isUnrepresentableCidr(entry) {
  return entry.includes("/") && entry !== DEFAULT_LINK_LOCAL_BYPASS;
}

/**
 * Parse the `ExceptionsList` array out of a `scutil --proxy` dump.
 *
 * Both print shapes are handled separately, because one lax regex over both
 * lets the inline form's own closing brace leak into the first shape's body:
 * the current `<array> { 0 : a \n 1 : b }` block (entries on numbered lines)
 * and the older single-line `{ a, b }` spelling. Entries are order-preserved
 * and mangled ones dropped, never silently reordered.
 *
 * @param {string} text
 * @returns {string[]}
 */
function parseExceptionsList(text) {
  const block = text.match(/^\s*ExceptionsList\s*:\s*<array>\s*\{([\s\S]*?)\n\s*\}\s*$/m);
  if (block) {
    return [...block[1].matchAll(/^\s*\d+\s*:\s*(.+?)\s*$/gm)]
      .map((match) => match[1])
      .filter((entry) => entry !== "" && !UNSAFE_ENTRY.test(entry));
  }
  const inline = text.match(/^\s*ExceptionsList\s*:\s*\{(.*?)\}\s*$/m);
  if (!inline) return [];
  return inline[1]
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry !== "" && !UNSAFE_ENTRY.test(entry));
}

/**
 * Keep only the top-level keys of a `scutil --proxy` dump.
 *
 * Nested dictionaries (`__SCOPED__` per interface, `__SUPPLEMENTAL__` per
 * match domain) hold proxies that apply only to that interface or domain.
 * Reading their keys would export a domain-only proxy for every host, so they
 * are cut out. The top-level `ExceptionsList` array is kept whole.
 *
 * @param {string} text
 * @returns {string}
 */
function topLevelKeys(text) {
  const kept = [];
  let depth = 0;
  let keptBlockDepth = null;
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (trimmed.startsWith("}")) {
      depth -= 1;
      if (keptBlockDepth !== null && depth === keptBlockDepth) {
        kept.push(line);
        keptBlockDepth = null;
        continue;
      }
    }
    const inKeptBlock = keptBlockDepth !== null;
    if (depth <= 1 || inKeptBlock) kept.push(line);
    if (trimmed.endsWith("{")) {
      if (!inKeptBlock && depth <= 1 && /^ExceptionsList\s*:/.test(trimmed)) keptBlockDepth = depth;
      depth += 1;
    }
  }
  return kept.join("\n");
}

/**
 * Turn a `scutil --proxy` dump into the standard proxy environment variables.
 *
 * Only protocols the system actually enables are emitted, and only with a
 * usable host:port pair. A .pac auto-config is a program this shell must not
 * interpret, so an auto-config-only store honestly discovers nothing and the
 * caller falls through. When nothing is enabled the answer is `null` —
 * "nothing to merge" — rather than an object of absent keys.
 *
 * Only the uppercase names are set. `agent.env` overrides a proxy by the
 * spelling the operator writes, normally uppercase; a discovered lowercase
 * twin would survive that override, and urllib, requests and curl prefer the
 * lowercase spelling, so the system proxy would win over the operator's.
 * `NO_PROXY` merges the system's bypass list with the loopback.
 *
 * @param {string | null | undefined} text
 * @returns {NodeJS.ProcessEnv | null}
 */
function proxyEnvFromScutilOutput(dump) {
  if (typeof dump !== "string" || dump.trim() === "") return null;
  const text = topLevelKeys(dump);

  const scalar = (key) => {
    const match = text.match(new RegExp(`^\\s*${key}\\s*:\\s*(.*?)\\s*$`, "m"));
    return match && match[1] !== "" ? match[1] : null;
  };
  const enabled = (key) => scalar(key) === "1";
  const proxyHost = (key) => {
    const value = scalar(key);
    if (!value || UNSAFE_ENTRY.test(value)) return null;
    // An IPv6 literal needs brackets inside a URL (`http://[::1]:3128`);
    // scutil prints it bare.
    return value.includes(":") && !value.startsWith("[") ? `[${value}]` : value;
  };
  const proxyPort = (key) => {
    const value = scalar(key);
    const port = value ? Number.parseInt(value, 10) : Number.NaN;
    return Number.isInteger(port) && port > 0 && port <= 65535 ? port : null;
  };

  const env = {};
  if (enabled("HTTPEnable")) {
    const host = proxyHost("HTTPProxy");
    const port = proxyPort("HTTPPort");
    if (host && port) env.HTTP_PROXY = `http://${host}:${port}`;
  }
  if (enabled("HTTPSEnable")) {
    const host = proxyHost("HTTPSProxy");
    const port = proxyPort("HTTPSPort");
    if (host && port) env.HTTPS_PROXY = `http://${host}:${port}`;
  }
  if (Object.keys(env).length === 0) return null;

  const listed = parseExceptionsList(text);
  // A bare `*` bypasses the proxy for every host: the system says "connect
  // directly", so there is nothing to discover. Any other wildcard NO_PROXY
  // cannot express (`*.*`, `10.*`) would leave a narrower bypass than the
  // system's, so it falls through the same way.
  if (listed.some(isUnrepresentableWildcard)) return null;
  // The same holds for a private CIDR range and for "Bypass proxy settings
  // for these Hosts & Domains: simple hostnames" (ExcludeSimpleHostnames),
  // which NO_PROXY has no spelling for.
  if (listed.some(isUnrepresentableCidr)) return null;
  if (enabled("ExcludeSimpleHostnames")) return null;
  const exceptions = listed.map(toNoProxyEntry).filter((entry) => entry !== null);
  env.NO_PROXY = [...new Set([...exceptions, ...LOOPBACK_BYPASS])].join(",");
  return env;
}

// The per-user key the Windows Internet Options proxy page writes.
const WININET_KEY = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";

/**
 * Pick the named values out of `reg query` output.
 *
 * Each value prints as `    <name>    <REG_TYPE>    <data>` on its own line.
 * Subkey lines carry no type column and never match.
 *
 * @param {string} text
 * @returns {Map<string, string>}
 */
function parseRegQuery(text) {
  const values = new Map();
  for (const raw of text.split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    // Columns are split on spaces and tabs only, never across a line break,
    // so an empty value cannot swallow the next record.
    if (line[0] !== " " && line[0] !== "\t") continue;
    const [name, type, rest] = splitColumns(line);
    if (!name || !isRegType(type)) continue;
    values.set(name.toLowerCase(), trimBlanks(rest));
  }
  return values;
}

const BLANK = new Set([" ", "\t"]);

function trimBlanks(text) {
  let start = 0;
  let end = text.length;
  while (start < end && BLANK.has(text[start])) start += 1;
  while (end > start && BLANK.has(text[end - 1])) end -= 1;
  return text.slice(start, end);
}

/** The first two blank-separated fields and the rest of the line (maybe ""). */
function splitColumns(line) {
  const fields = [];
  let i = 0;
  while (fields.length < 2) {
    while (i < line.length && BLANK.has(line[i])) i += 1;
    const start = i;
    while (i < line.length && !BLANK.has(line[i])) i += 1;
    if (start === i) return [null, null, ""];
    fields.push(line.slice(start, i));
  }
  return [fields[0], fields[1], line.slice(i)];
}

const REG_TYPE_CHARS = new Set("ABCDEFGHIJKLMNOPQRSTUVWXYZ_");

function isRegType(type) {
  return (
    typeof type === "string" &&
    type.startsWith("REG_") &&
    type.length > 4 &&
    [...type.slice(4)].every((c) => REG_TYPE_CHARS.has(c))
  );
}

/**
 * One WinINet `host:port` server entry as a proxy URL, or null when unusable.
 *
 * WinINet speaks plain HTTP to a server named here (an `http://` prefix is
 * accepted and dropped). Any other scheme is not an HTTP proxy.
 *
 * @param {string} entry
 * @returns {string | null}
 */
const DIGITS = new Set("0123456789");
const HEX_HOST_CHARS = new Set("0123456789abcdefABCDEF:.");
const NAME_HOST_FORBIDDEN = new Set([" ", "\t", "\r", "\n", ":", "/", "[", "]"]);

function winProxyUrl(entry) {
  let server = entry.trim();
  if (server.toLowerCase().startsWith("http://")) server = server.slice(7);
  if (server.endsWith("/")) server = server.slice(0, -1);
  if (UNSAFE_ENTRY.test(server)) return null;
  // WinINet reads a server with no port as port 80. A bracketed IPv6 literal
  // carries its own colons, so only a colon after the closing `]` is a port.
  const colon = server.lastIndexOf(":");
  const hasPort = colon > 0 && colon > server.lastIndexOf("]");
  const host = hasPort ? server.slice(0, colon) : server;
  const portText = hasPort ? server.slice(colon + 1) : "80";
  if (host === "") return null;
  if (portText.length < 1 || portText.length > 5 || ![...portText].every((c) => DIGITS.has(c))) {
    return null;
  }
  const bracketed =
    host.length > 2 &&
    host.startsWith("[") &&
    host.endsWith("]") &&
    [...host.slice(1, -1)].every((c) => HEX_HOST_CHARS.has(c));
  const named = [...host].every((c) => !NAME_HOST_FORBIDDEN.has(c));
  if (!bracketed && !named) return null;
  const port = Number.parseInt(portText, 10);
  return port > 0 && port <= 65535 ? `http://${host}:${port}` : null;
}

/**
 * Turn the WinINet registry values into the standard proxy variables.
 *
 * `ProxyServer` is either one `host:port` for every protocol or a
 * `http=...;https=...` list, where a protocol left out connects directly.
 * The bypass rules match `proxyEnvFromScutilOutput`: `<local>` (bypass
 * simple host names) and any wildcard other than a leading `*.` have no
 * NO_PROXY spelling, so either one discovers nothing rather than export a
 * narrower bypass than the system's. An auto-config (PAC) setting alone is
 * not read, and a setup script set beside a manual server discovers nothing.
 *
 * @param {string | null | undefined} text - `reg query` output
 * @returns {NodeJS.ProcessEnv | null}
 */
function proxyEnvFromRegQueryOutput(text) {
  if (typeof text !== "string" || text.trim() === "") return null;
  const values = parseRegQuery(text);
  const enable = values.get("proxyenable");
  if (!enable || Number.parseInt(enable, 16) !== 1) return null;
  const server = (values.get("proxyserver") || "").trim();
  if (server === "") return null;
  // WinINet asks a setup script before the manual server, so a manual proxy
  // exported for every host would override the script's DIRECT answers.
  if ((values.get("autoconfigurl") || "").trim() !== "") return null;

  const env = {};
  if (!server.includes("=")) {
    const url = winProxyUrl(server);
    if (url) {
      env.HTTP_PROXY = url;
      env.HTTPS_PROXY = url;
    }
  } else {
    for (const part of server.split(";")) {
      const [scheme, ...rest] = part.split("=");
      const url = winProxyUrl(rest.join("="));
      if (!url) continue;
      const name = scheme.trim().toLowerCase();
      if (name === "http") env.HTTP_PROXY = url;
      if (name === "https") env.HTTPS_PROXY = url;
    }
    // A list without an https entry sends HTTPS direct, and clients that fall
    // back from HTTPS_PROXY to HTTP_PROXY would proxy it, so nothing is set.
    if (!env.HTTPS_PROXY) return null;
  }
  if (Object.keys(env).length === 0) return null;

  const listed = (values.get("proxyoverride") || "")
    .split(";")
    .map((entry) => entry.trim().toLowerCase())
    .filter((entry) => entry !== "");
  if (listed.includes("<local>")) return null;
  if (listed.some(isUnrepresentableWildcard)) return null;
  // WinINet matches `?` and `[...]` as wildcards too, while NO_PROXY readers
  // take them literally, so an entry with either has no NO_PROXY spelling.
  if (listed.some((entry) => ["?", "[", "]"].some((c) => entry.includes(c)))) return null;
  // `<-loopback>` removes WinINet's implicit loopback bypass; the loopback is
  // never proxied here, so it is dropped.
  const bypass = listed.filter((entry) => entry !== "<-loopback>");
  const exceptions = bypass.map((entry) => (UNSAFE_ENTRY.test(entry) ? null : toNoProxyEntry(entry)));
  // An entry that cannot be copied would leave a narrower bypass than the
  // system's, so it discovers nothing, like `<local>` above.
  if (exceptions.includes(null)) return null;
  env.NO_PROXY = [...new Set([...exceptions, ...LOOPBACK_BYPASS])].join(",");
  return env;
}

// The in-box `reg.exe` under the Windows directory the kernel names, never
// `%SystemRoot%`, which a user can point at a planted binary.
function readWindowsProxyEnvironment(execFileSync, fs) {
  const root = windowsSystemRoot(fs);
  if (!root) return null;
  const reg = path.win32.join(root, "System32", "reg.exe");
  const out = execFileSync(reg, ["query", WININET_KEY], {
    encoding: "utf8",
    timeout: READ_TIMEOUT_MS,
    maxBuffer: MAX_READ_BYTES,
    stdio: ["ignore", "pipe", "ignore"],
    windowsHide: true,
  });
  return proxyEnvFromRegQueryOutput(typeof out === "string" ? out : "");
}

/**
 * Read the system proxy: macOS's Network configuration store, or the Windows
 * WinINet registry key.
 *
 * Returns `null` on any other platform, without an executor, on a
 * missing/wedged reader, or when the system enables no manual proxy. Every one
 * of those means "nothing to merge", and the inherited environment stands.
 *
 * @param {object} deps
 * @param {Function} deps.execFileSync - `child_process.execFileSync`
 * @param {string} [deps.platform] - `process.platform` (defaults to it)
 * @param {typeof import("fs")} [deps.fs] - Node fs (Windows reader only)
 * @returns {NodeJS.ProcessEnv | null}
 */
function readSystemProxyEnvironment({
  execFileSync,
  platform = process.platform,
  fs = require("fs"),
} = {}) {
  if (typeof execFileSync !== "function") return null;
  if (platform === "win32") {
    try {
      return readWindowsProxyEnvironment(execFileSync, fs);
    } catch {
      return null;
    }
  }
  if (platform !== "darwin") return null;
  try {
    const out = execFileSync(SCUTIL, ["--proxy"], {
      encoding: "utf8",
      timeout: READ_TIMEOUT_MS,
      maxBuffer: MAX_READ_BYTES,
      // scutil writes nothing useful to stderr here; a silent pipe keeps the
      // spawn path's log file free of subprocess noise.
      stdio: ["ignore", "pipe", "ignore"],
    });
    return proxyEnvFromScutilOutput(typeof out === "string" ? out : "");
  } catch {
    return null;
  }
}

module.exports = {
  parseRegQuery,
  SCUTIL,
  WININET_KEY,
  proxyEnvFromRegQueryOutput,
  proxyEnvFromScutilOutput,
  readSystemProxyEnvironment,
};
