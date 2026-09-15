"use strict";
//
// Opt-in relaxation of Chromium's child-process sandbox, for hosts where the
// sandbox cannot start a child at all.
//
// The problem this solves: on some managed Windows devices EVERY sandboxed
// Chromium child dies the instant it launches. The window flashes gray and the
// app exits. The launcher log shows the GPU process failing over and over:
//
//     GPU process exited unexpectedly: exit_code=-2147483645
//     GPU process isn't usable. Goodbye.
//
// -2147483645 is 0x80000003, STATUS_BREAKPOINT — a CHECK failure during
// sandbox setup, not a graphics fault. The Gateway is healthy and the dashboard
// loads fine in a normal browser on the same machine; only Electron's children
// are dead, so the whole app looks broken on a working host. The usual cause is
// a third-party DLL forced into every child process (endpoint security, or a
// DisplayLink-style display driver) which Chromium's renderer code-integrity
// and AppContainer hardening then refuse to admit.
//
// Why this module exists rather than documenting `--no-sandbox`:
//
//  1. `--no-sandbox` is far too broad. It drops renderer AND GPU AND utility
//     isolation for the whole app. On a managed device — the only place this
//     bug appears — that is the worst trade available. Chromium can relax ONE
//     hardening layer at a time, which is usually enough, so the narrow levers
//     have to be reachable or nobody will use them.
//  2. A command-line flag is unreachable in practice. The single-instance
//     handoff drops argv from a second launch (see disable-gpu.js), so
//     `KiroCrew.exe --no-sandbox` silently does nothing once an instance holds
//     the lock — and the shortcut that carries the flag is rewritten by the
//     next app update. An env var survives both, and is the only form a fleet
//     administrator can push with Intune or Group Policy.
//
// Deliberately NOT an arbitrary-switch passthrough. A `KIROCREW_ELECTRON_FLAGS`
// that forwarded whatever it was given would be a privilege-escalation seam,
// not a compatibility one: `--remote-debugging-port` alone exposes a full CDP
// surface (every cookie and token in the profile) to any local process, and
// `--host-rules` silently redirects the app's traffic. Environment variables
// are writable by anything running as the user, agent shell commands included.
// So the accepted values are a CLOSED allowlist of sandbox-hardening layers,
// each mapping to switches reviewed here. An unrecognised token is ignored and
// logged; it never widens into a broader opt-out.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so the decision has to be testable without a live `app`
// (same pattern as disable-gpu.js / sandbox-profile.js / renderer-recovery.js).
//

/**
 * Tokens that disable ONE Chromium feature each, narrowest first.
 *
 * Iteration order is the applied order, so the emitted `--disable-features`
 * value is stable no matter how the operator ordered their input — a test can
 * assert an exact string, and two hosts with the same intent produce the same
 * command line.
 *
 * `RendererCodeIntegrity` is first because it is the one to try first: it is
 * the layer that blocks an unsigned DLL from loading into a renderer, and
 * Chrome ships an enterprise policy (`RendererCodeIntegrityEnabled`) for
 * exactly this situation, so it is well-trodden ground rather than a guess.
 */
const FEATURE_TOKENS = new Map([
  ["renderer-code-integrity", "RendererCodeIntegrity"],
  ["renderer-app-container", "RendererAppContainer"],
  ["network-service-sandbox", "NetworkServiceSandbox"],
]);

/**
 * Tokens that map to a plain switch rather than a feature flag.
 *
 * `off` is last and is the blunt instrument: it disables the sandbox for every
 * child process. It is offered because a host that survives none of the narrow
 * levers still needs a way to run the app at all, but it is reported at WARN so
 * it cannot become a permanent setting nobody remembers making.
 */
const SWITCH_TOKENS = new Map([
  ["gpu-sandbox", "disable-gpu-sandbox"],
  ["off", "no-sandbox"],
]);

/** The token that gives up the whole sandbox, called out for its own warning. */
const FULL_OPT_OUT = "off";

/**
 * Every accepted token, for error messages and documentation.
 *
 * @returns {string[]}
 */
function knownTokens() {
  return [...FEATURE_TOKENS.keys(), ...SWITCH_TOKENS.keys()];
}

/**
 * Split the requested tokens out of the environment and argv.
 *
 * Accepts comma- or space-separated values, any case, with surrounding
 * whitespace — this is typed into a Group Policy field by a human under time
 * pressure, so "Off", " gpu-sandbox , off " and "gpu-sandbox off" all mean what
 * they look like.
 *
 * Inputs, both honoured (env wins nothing; the union is used):
 *   1. `KIROCREW_SANDBOX_COMPAT=<tokens>` — the durable, deployable form.
 *   2. `--sandbox-compat=<tokens>` in argv — for a one-off diagnostic launch,
 *      with the single-instance argv caveat above.
 *
 * @param {object} deps
 * @param {NodeJS.ProcessEnv} [deps.env]
 * @param {string[]} [deps.argv]
 * @returns {{requested: string[], unknown: string[]}} `requested` holds the
 *   recognised tokens in canonical (applied) order; `unknown` holds the rest,
 *   in the order given, for reporting.
 */
function parseSandboxCompat({ env = process.env, argv = process.argv } = {}) {
  const raw = [];

  const fromEnv = env && env.KIROCREW_SANDBOX_COMPAT;
  if (typeof fromEnv === "string") raw.push(fromEnv);

  if (Array.isArray(argv)) {
    for (const arg of argv) {
      if (typeof arg !== "string") continue;
      if (arg.startsWith("--sandbox-compat=")) {
        raw.push(arg.slice("--sandbox-compat=".length));
      }
    }
  }

  const seen = new Set();
  const unknown = [];
  for (const chunk of raw) {
    for (const piece of String(chunk).split(/[,\s]+/)) {
      const token = piece.trim().toLowerCase();
      if (!token) continue;
      if (FEATURE_TOKENS.has(token) || SWITCH_TOKENS.has(token)) {
        seen.add(token);
      } else if (!unknown.includes(token)) {
        unknown.push(token);
      }
    }
  }

  // Canonical order, not input order: see FEATURE_TOKENS.
  const requested = knownTokens().filter((token) => seen.has(token));
  return { requested, unknown };
}

/**
 * The exact Chromium switches a set of tokens becomes.
 *
 * Returned as data (not applied inline) so a test can pin the switch spelling:
 * these are Chromium's names, and an unknown switch is ignored SILENTLY, so a
 * typo here is a fix that appears to work and changes nothing.
 *
 * The single combined `--disable-features` is load-bearing. Chromium stores one
 * value per switch name, so appending the switch twice does not union the two —
 * the second overwrites the first, and the layer the operator listed earlier
 * stays enabled. Every feature token therefore has to arrive in ONE
 * comma-separated value.
 *
 * @param {string[]} tokens Recognised tokens, as returned by parseSandboxCompat.
 * @returns {Array<[string, string|undefined]>} `[switchName, value]` pairs.
 */
function sandboxCompatSwitches(tokens) {
  const wanted = new Set(Array.isArray(tokens) ? tokens : []);
  const pairs = [];

  const features = [...FEATURE_TOKENS.entries()]
    .filter(([token]) => wanted.has(token))
    .map(([, feature]) => feature);
  if (features.length) pairs.push(["disable-features", features.join(",")]);

  for (const [token, name] of SWITCH_TOKENS) {
    if (wanted.has(token)) pairs.push([name, undefined]);
  }

  return pairs;
}

/**
 * Apply the requested sandbox relaxations. Never throws.
 *
 * Must run BEFORE the app is ready: Chromium reads these during
 * initialization, so appending them later is accepted and then ignored — the
 * same timing constraint as the GPU and native-logging switches.
 *
 * @param {object} deps
 * @param {(name: string, value?: string) => void} deps.appendSwitch
 * @param {NodeJS.ProcessEnv} [deps.env]
 * @param {string[]} [deps.argv]
 * @param {(msg: string) => void} [deps.log]
 * @param {(msg: string) => void} [deps.warn] User-facing channel for the full
 *   opt-out. Defaults to `log` so a caller that has no warning channel still
 *   records it.
 * @returns {{requested: string[], applied: string[], unknown: string[], fullOptOut: boolean}}
 */
function initSandboxCompat({
  appendSwitch,
  env,
  argv,
  log = () => {},
  warn,
} = {}) {
  const { requested, unknown } = parseSandboxCompat({ env, argv });
  const userWarn = typeof warn === "function" ? warn : log;
  const applied = [];

  if (unknown.length) {
    // Named individually: the whole point of a closed allowlist is that a
    // misremembered token fails visibly instead of being read as "off".
    log(
      `sandbox-compat: ignoring unknown token(s) ${unknown.join(",")} — ` +
        `accepted values are ${knownTokens().join(", ")}`,
    );
  }

  if (!requested.length) {
    return { requested, applied, unknown, fullOptOut: false };
  }

  for (const [name, value] of sandboxCompatSwitches(requested)) {
    try {
      if (value === undefined) appendSwitch(name);
      else appendSwitch(name, value);
      applied.push(value === undefined ? name : `${name}=${value}`);
    } catch (e) {
      // One rejected switch must not cost us the others, nor the boot.
      log(`sandbox-compat switch --${name} failed: ${e && e.message}`);
    }
  }

  const fullOptOut = requested.includes(FULL_OPT_OUT);
  log(
    `sandbox-compat: tokens=${requested.join(",")} ` +
      `switches=${applied.join(" ") || "none"}`,
  );
  if (fullOptOut) {
    // Deliberately on the user-facing channel. This state weakens renderer,
    // GPU and utility isolation for every launch, and the operator who set it
    // is usually not the person reading the log months later.
    userWarn(
      "WARN sandbox DISABLED for every child process (KIROCREW_SANDBOX_COMPAT=off). " +
        "This is a last-resort compatibility setting, not a supported steady state — " +
        "prefer a narrower token, or an endpoint-security exclusion for this app.",
    );
  }

  return { requested, applied, unknown, fullOptOut };
}

/**
 * Whether this launch is running with Chromium's sandbox disabled, by ANY route.
 *
 * Two different routes reach the same state and both have to count:
 *
 *  1. This module's own `off` token.
 *  2. A raw `--no-sandbox` passed straight to Chromium, which needs no
 *     cooperation from this module to take effect.
 *
 * Missing the second is what makes the diagnostic advice actively wrong rather
 * than merely redundant: a renderer that keeps dying when the sandbox is
 * ALREADY off is not failing *because* of the sandbox, so pointing the reader
 * at the sandbox ladder sends them down a dead end while the real cause goes
 * unexamined.
 *
 * @param {object} deps
 * @param {NodeJS.ProcessEnv} [deps.env]
 * @param {string[]} [deps.argv]
 * @returns {boolean}
 */
function sandboxIsDisabled({ env = process.env, argv = process.argv } = {}) {
  if (parseSandboxCompat({ env, argv }).requested.includes(FULL_OPT_OUT)) return true;
  return Array.isArray(argv) && argv.includes("--no-sandbox");
}

module.exports = {
  parseSandboxCompat,
  sandboxCompatSwitches,
  initSandboxCompat,
  sandboxIsDisabled,
  knownTokens,
  FEATURE_TOKENS,
  SWITCH_TOKENS,
  FULL_OPT_OUT,
};
