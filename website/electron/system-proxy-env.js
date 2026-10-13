"use strict";

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
// Windows and Linux need no discovery here. A Windows GUI process inherits the
// user's environment variables, the ordinary place proxy variables live; a
// Linux desktop session starts from the session environment the app already
// inherits. On both, `buildGatewayEnvironment` keeps passing the environment
// through untouched.
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
 * Turn a `scutil --proxy` dump into the standard proxy environment variables.
 *
 * Only protocols the system actually enables are emitted, and only with a
 * usable host:port pair. A .pac auto-config is a program this shell must not
 * interpret, so an auto-config-only store honestly discovers nothing and the
 * caller falls through. When nothing is enabled the answer is `null` —
 * "nothing to merge" — rather than an object of absent keys.
 *
 * Both spellings of every name are set (many HTTP stacks check exactly one
 * case), and `NO_PROXY` merges the system's bypass list with the loopback.
 *
 * @param {string | null | undefined} text
 * @returns {NodeJS.ProcessEnv | null}
 */
function proxyEnvFromScutilOutput(text) {
  if (typeof text !== "string" || text.trim() === "") return null;

  const scalar = (key) => {
    const match = text.match(new RegExp(`^\\s*${key}\\s*:\\s*(.*?)\\s*$`, "m"));
    return match && match[1] !== "" ? match[1] : null;
  };
  const enabled = (key) => scalar(key) === "1";
  const proxyHost = (key) => {
    const value = scalar(key);
    return value && !UNSAFE_ENTRY.test(value) ? value : null;
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
    if (host && port) {
      env.HTTP_PROXY = `http://${host}:${port}`;
      env.http_proxy = env.HTTP_PROXY;
    }
  }
  if (enabled("HTTPSEnable")) {
    const host = proxyHost("HTTPSProxy");
    const port = proxyPort("HTTPSPort");
    if (host && port) {
      env.HTTPS_PROXY = `http://${host}:${port}`;
      env.https_proxy = env.HTTPS_PROXY;
    }
  }
  if (enabled("SOCKSEnable")) {
    const host = proxyHost("SOCKSProxy");
    const port = proxyPort("SOCKSPort");
    if (host && port) {
      env.ALL_PROXY = `socks5://${host}:${port}`;
      env.all_proxy = env.ALL_PROXY;
    }
  }
  if (Object.keys(env).length === 0) return null;

  const noProxy = [
    ...new Set([...parseExceptionsList(text), ...LOOPBACK_BYPASS]),
  ].join(",");
  env.NO_PROXY = noProxy;
  env.no_proxy = noProxy;
  return env;
}

/**
 * Read the system proxy from macOS's Network configuration store.
 *
 * Mirrors `mac-env.js`'s `readLaunchdPath`: returns `null` on any non-darwin
 * platform, without an executor, on a missing/wedged `scutil`, or when the
 * store enables no manual proxy — every one of those means "nothing to
 * merge", and the inherited environment stands.
 *
 * @param {object} deps
 * @param {Function} deps.execFileSync - `child_process.execFileSync`
 * @param {string} [deps.platform] - `process.platform` (defaults to it)
 * @returns {NodeJS.ProcessEnv | null}
 */
function readSystemProxyEnvironment({ execFileSync, platform = process.platform } = {}) {
  if (platform !== "darwin" || typeof execFileSync !== "function") return null;
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
  SCUTIL,
  READ_TIMEOUT_MS,
  MAX_READ_BYTES,
  LOOPBACK_BYPASS,
  parseExceptionsList,
  proxyEnvFromScutilOutput,
  readSystemProxyEnvironment,
};
