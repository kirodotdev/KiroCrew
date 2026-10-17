"use strict";

// Open the user's own terminal emulator in a project directory.
//
// THE LAUNCH BOUNDARY. The desktop shell starts the terminal's OWN executable,
// with an argv this file pins per app, and hands the project directory to it
// only as the child's working directory (and, where the app needs it, as the
// value of the app's own working-directory flag). The directory is never a
// DOCUMENT given to an OS opener: no `open`, no `open -a`, no `shell.openPath`,
// no `xdg-open`, no `start`, no AppleScript, no shell string. An opener
// re-resolves the pathname later and runs whatever handler the thing at that
// path selects, so a rename between the check and the open can swap a directory
// for content a handler executes. Here, the same swap can at worst change the
// directory a shell starts in; a directory replaced by a file makes the child's
// chdir fail, and the launch reports failure.
//
// WHAT IS LEFT OUT. An app that cannot be started this way is not on the list:
// Terminal.app and iTerm2 take no working-directory argument and need
// AppleScript, Ghostty on macOS can only be started through `open -na`, and
// Warp opens a directory through a URL scheme. Windows has no entry yet:
// Windows Terminal is reached through an alias in a user-writable directory,
// and the console-host launch shape is not verified.
//
// WHERE THE BINARY COMES FROM. Only the fixed absolute paths below are probed,
// never PATH and never a path the renderer or the user supplies. A user- or
// agent-writable PATH entry therefore cannot decide which program runs, and a
// setting naming an app this file does not list is ignored.
//
// WHO DECIDES. The gateway, not this process and not the renderer. A launch
// starts from a single-use grant the gateway mints for the dashboard owner on a
// direct-local request, when the integrated terminal is enabled and the user has
// picked an app (`dashboard.terminal.external_app`, unset by default). The grant
// names the app and the resolved directory; this process redeems it over the
// loopback internal-secret channel and starts nothing without it. The renderer
// hands over only the opaque grant.

const fs = require("fs");
const nodePath = require("path");
const { spawn } = require("child_process");

// The gateway route that redeems a launch grant. Machine-only: loopback plus
// the internal secret, the same channel the native browser panel drains on.
const GRANT_REDEEM_PATH = "/api/terminal/external-grant";

// A grant is the gateway's `secrets.token_urlsafe(32)`: 43 URL-safe characters.
// Anything else is refused before a request is made.
const GRANT_RE = /^[A-Za-z0-9_-]{32,128}$/;

// Root-owned system directories a Linux terminal emulator is installed into by
// the distribution's package manager, Snap, or NixOS.
const LINUX_BIN_DIRS = Object.freeze([
  "/usr/bin",
  "/bin",
  "/usr/local/bin",
  "/run/current-system/sw/bin",
  "/snap/bin",
]);

function linuxCandidates(name) {
  return LINUX_BIN_DIRS.map((dir) => `${dir}/${name}`);
}

function macCandidate(bundle, exe) {
  return [`/Applications/${bundle}.app/Contents/MacOS/${exe}`];
}

// The allowlist. `args(dir)` is the complete argv after the executable; `dir`
// is always an absolute realpath, so it can never read as a flag. Every entry
// starts a new window in that directory.
const TERMINALS = Object.freeze([
  Object.freeze({
    id: "alacritty",
    label: "Alacritty",
    candidates: Object.freeze({
      darwin: macCandidate("Alacritty", "alacritty"),
      linux: linuxCandidates("alacritty"),
    }),
    args: (dir) => ["--working-directory", dir],
  }),
  Object.freeze({
    id: "ghostty",
    label: "Ghostty",
    candidates: Object.freeze({
      linux: linuxCandidates("ghostty"),
    }),
    args: (dir) => [`--working-directory=${dir}`],
  }),
  Object.freeze({
    id: "gnome-terminal",
    label: "GNOME Terminal",
    candidates: Object.freeze({
      linux: linuxCandidates("gnome-terminal"),
    }),
    args: (dir) => [`--working-directory=${dir}`],
  }),
  Object.freeze({
    id: "kitty",
    label: "kitty",
    candidates: Object.freeze({
      darwin: macCandidate("kitty", "kitty"),
      linux: linuxCandidates("kitty"),
    }),
    args: (dir) => ["--directory", dir],
  }),
  Object.freeze({
    id: "konsole",
    label: "Konsole",
    candidates: Object.freeze({
      linux: linuxCandidates("konsole"),
    }),
    args: (dir) => ["--workdir", dir],
  }),
  Object.freeze({
    id: "wezterm",
    label: "WezTerm",
    candidates: Object.freeze({
      darwin: macCandidate("WezTerm", "wezterm"),
      linux: linuxCandidates("wezterm"),
    }),
    args: (dir) => ["start", "--cwd", dir],
  }),
  Object.freeze({
    id: "xfce4-terminal",
    label: "Xfce Terminal",
    candidates: Object.freeze({
      linux: linuxCandidates("xfce4-terminal"),
    }),
    args: (dir) => [`--working-directory=${dir}`],
  }),
]);

const BY_ID = new Map(TERMINALS.map((t) => [t.id, t]));

/** True when `file` is an executable regular file (symlinks followed). */
function isExecutableFile(fsMod, file) {
  try {
    if (!fsMod.statSync(file).isFile()) return false;
    fsMod.accessSync(file, fsMod.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolve one allowlisted app to the executable installed on this machine.
 *
 * @param {string} id - an allowlist id; any other value resolves to null
 * @param {object} [deps]
 * @param {string} [deps.platform] - `process.platform`
 * @param {typeof import("fs")} [deps.fs]
 * @returns {{id: string, label: string, bin: string, args: (dir: string) => string[]}|null}
 */
function resolveTerminal(id, { platform = process.platform, fs: fsMod = fs } = {}) {
  if (typeof id !== "string" || !BY_ID.has(id)) return null;
  const entry = BY_ID.get(id);
  const candidates = entry.candidates[platform] || [];
  for (const bin of candidates) {
    if (isExecutableFile(fsMod, bin)) {
      return { id: entry.id, label: entry.label, bin, args: entry.args };
    }
  }
  return null;
}

/**
 * The allowlisted apps installed on this machine, in allowlist order.
 *
 * @param {object} [deps] - see {@link resolveTerminal}
 * @returns {Array<{id: string, label: string}>}
 */
function detectTerminals(deps = {}) {
  const found = [];
  for (const entry of TERMINALS) {
    const hit = resolveTerminal(entry.id, deps);
    if (hit) found.push({ id: hit.id, label: hit.label });
  }
  return found;
}

/**
 * Redeem a gateway launch grant for the app and directory it names.
 *
 * POSTs the grant to {@link GRANT_REDEEM_PATH} with the internal secret. The
 * gateway hands a grant out once, so a replayed or forged one comes back as a
 * refusal. Resolves `{app, cwd}` on HTTP 200 with a well-formed body, else
 * null; never rejects.
 *
 * @param {unknown} grant
 * @param {object} deps
 * @param {string} deps.backendUrl - this shell's own local gateway URL
 * @param {() => string} deps.getSecret - the internal secret, re-read per call
 * @param {(url: string, init: object) => Promise<{status: number, json: () => Promise<any>}>} deps.fetchFn
 * @returns {Promise<{app: string, cwd: string}|null>}
 */
async function redeemGrant(grant, { backendUrl, getSecret, fetchFn }) {
  if (typeof grant !== "string" || !GRANT_RE.test(grant)) return null;
  const secret = typeof getSecret === "function" ? getSecret() : "";
  if (!secret) return null;
  let res;
  try {
    res = await fetchFn(`${backendUrl}${GRANT_REDEEM_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Secret": secret },
      body: JSON.stringify({ grant }),
    });
  } catch {
    return null;
  }
  if (!res || res.status !== 200) return null;
  let body;
  try {
    body = await res.json();
  } catch {
    return null;
  }
  if (!body || typeof body.app !== "string" || typeof body.cwd !== "string") return null;
  return { app: body.app, cwd: body.cwd };
}

/**
 * The directory a launch may start in: the realpath of `dir` when that is an
 * existing directory, else null. A relative path is refused rather than
 * resolved against this process's own working directory.
 *
 * @param {unknown} dir
 * @param {typeof import("fs")} [fsMod]
 * @returns {string|null}
 */
function launchDirectory(dir, fsMod = fs) {
  if (typeof dir !== "string" || dir === "" || !nodePath.isAbsolute(dir)) return null;
  try {
    const real = fsMod.realpathSync(dir);
    return fsMod.statSync(real).isDirectory() ? real : null;
  } catch {
    return null;
  }
}

/**
 * Start an allowlisted terminal app in `dir`.
 *
 * Resolves `{ ok: true, app }` once the child process has started, or
 * `{ ok: false, error }` with one of `"no-app"` (the id is not an installed
 * allowlisted app), `"bad-directory"` (not an existing directory) or
 * `"launch-failed"` (the process did not start, including a working directory
 * that stopped being a directory before the child could enter it). Never
 * rejects. Logs name the app only, never the directory or the argv.
 *
 * @param {string} id
 * @param {string} dir
 * @param {object} [deps]
 * @param {string} [deps.platform]
 * @param {typeof import("fs")} [deps.fs]
 * @param {typeof spawn} [deps.spawnFn]
 * @param {(line: string) => void} [deps.log]
 * @returns {Promise<{ok: boolean, app?: string, error?: string}>}
 */
function launchExternalTerminal(id, dir, {
  platform = process.platform,
  fs: fsMod = fs,
  spawnFn = spawn,
  log = () => {},
} = {}) {
  const app = resolveTerminal(id, { platform, fs: fsMod });
  if (!app) {
    log("external-terminal: no installed terminal app is chosen");
    return Promise.resolve({ ok: false, error: "no-app" });
  }
  const cwd = launchDirectory(dir, fsMod);
  if (!cwd) {
    log(`external-terminal: ${app.id} refused, the project is not a directory`);
    return Promise.resolve({ ok: false, error: "bad-directory" });
  }
  return new Promise((resolve) => {
    let settled = false;
    const settle = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    let child;
    try {
      child = spawnFn(app.bin, app.args(cwd), {
        cwd,
        detached: true,
        stdio: "ignore",
        shell: false,
        windowsHide: false,
      });
    } catch (err) {
      log(`external-terminal: ${app.id} failed to start (${(err && err.code) || "error"})`);
      settle({ ok: false, error: "launch-failed" });
      return;
    }
    child.once("error", (err) => {
      log(`external-terminal: ${app.id} failed to start (${(err && err.code) || "error"})`);
      settle({ ok: false, error: "launch-failed" });
    });
    child.once("spawn", () => {
      // The terminal outlives this app: detach it from our event loop.
      if (typeof child.unref === "function") child.unref();
      log(`external-terminal: started ${app.id}`);
      settle({ ok: true, app: app.id });
    });
  });
}

/**
 * Launch only on the gateway's approval: redeem `grant`, then start the app it
 * names in the directory it names. Resolves `{ ok: false, error: "no-grant" }`
 * when the gateway does not honour the grant, otherwise the result of
 * {@link launchExternalTerminal}. Never rejects.
 *
 * @param {unknown} grant
 * @param {object} deps - {@link redeemGrant}'s plus {@link launchExternalTerminal}'s
 * @returns {Promise<{ok: boolean, app?: string, error?: string}>}
 */
async function launchFromGrant(grant, deps) {
  const log = typeof deps.log === "function" ? deps.log : () => {};
  const approved = await redeemGrant(grant, deps);
  if (!approved) {
    log("external-terminal: the gateway did not approve this launch");
    return { ok: false, error: "no-grant" };
  }
  return launchExternalTerminal(approved.app, approved.cwd, deps);
}

module.exports = {
  GRANT_REDEEM_PATH,
  LINUX_BIN_DIRS,
  TERMINALS,
  resolveTerminal,
  detectTerminals,
  redeemGrant,
  launchDirectory,
  launchExternalTerminal,
  launchFromGrant,
};
