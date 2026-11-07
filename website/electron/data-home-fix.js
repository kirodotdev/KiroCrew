"use strict";

// One-click repair for a data home another account owns (left by an earlier
// `sudo kirocrew ...`): give it back to the person running this app.
//
// The OS asks for the admin password itself -- macOS through `osascript ... with
// administrator privileges`, Linux through `pkexec` -- so no password ever
// reaches this process. The only command run with privilege is a `find` over
// the data home that runs `chown -h <this uid>` on entries another account owns,
// skipping other filesystems and any non-directory with a second hard link
// (see elevatedCommand). The target is the data home this process resolves
// itself (never a path from the renderer), it must be a real directory and not a
// link, and `find` never follows a link inside it. The owner-only modes are then
// restored as the person, with no privilege at all.
//
// Every binary is a fixed absolute path; nothing is looked up on PATH.

const nodeFs = require("fs");
const nodePath = require("path");
const childProcess = require("child_process");
const { resolveHome } = require("./home-dir");

const OSASCRIPT = "/usr/bin/osascript";
const PKEXEC_CANDIDATES = ["/usr/bin/pkexec", "/bin/pkexec"];
const CHOWN_CANDIDATES = {
  darwin: ["/usr/sbin/chown"],
  linux: ["/usr/bin/chown", "/bin/chown"],
};
const CHMOD_CANDIDATES = ["/bin/chmod", "/usr/bin/chmod"];
const FIND_CANDIDATES = ["/usr/bin/find", "/bin/find"];

// osascript's "User canceled." (-128) and pkexec's dismissed dialog.
const OSASCRIPT_CANCELLED = "-128";
const PKEXEC_CANCELLED_CODES = new Set([126]);

function firstExisting(candidates, fs) {
  for (const candidate of candidates) {
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch { /* not here */ }
  }
  return null;
}

/** The elevation route this host offers, or null where none is available. */
function elevationRoute({ platform = process.platform, fs = nodeFs } = {}) {
  const chown = firstExisting(CHOWN_CANDIDATES[platform] || [], fs);
  const find = firstExisting(FIND_CANDIDATES, fs);
  if (!chown || !find) return null;
  if (platform === "darwin") {
    return firstExisting([OSASCRIPT], fs) ? { kind: "osascript", bin: OSASCRIPT, chown, find } : null;
  }
  if (platform === "linux") {
    const pkexec = firstExisting(PKEXEC_CANDIDATES, fs);
    return pkexec ? { kind: "pkexec", bin: pkexec, chown, find } : null;
  }
  return null;
}

/**
 * The directory the repair may touch: the resolved data home, as a real path.
 * Throws when it is missing, a link, not a directory, or a filesystem root.
 */
function repairTarget(home, { fs = nodeFs, path = nodePath } = {}) {
  const st = fs.lstatSync(home);
  if (st.isSymbolicLink()) throw new Error("the data home is a link");
  if (!st.isDirectory()) throw new Error("the data home is not a directory");
  const real = fs.realpathSync(home);
  if (real === path.dirname(real)) throw new Error("the data home is a filesystem root");
  return real;
}

/**
 * Whether *gatewayHome* (the data home the connected gateway reports) is the
 * home this process resolves. A gateway started with another KIROCREW_HOME
 * would stay broken after a repair of this one, so the shell offers none.
 * The value only gates the repair; the target is always *home*.
 */
function sameHome(gatewayHome, home, { fs = nodeFs } = {}) {
  if (typeof gatewayHome !== "string" || !gatewayHome) return false;
  try {
    return fs.realpathSync(gatewayHome) === fs.realpathSync(home);
  } catch {
    return false;
  }
}

/** Whether a repair of *gatewayHome* can run here: a route, and the same home. */
function canFix({ gatewayHome, platform = process.platform, fs = nodeFs, home = resolveHome() } = {}) {
  return elevationRoute({ platform, fs }) !== null && sameHome(gatewayHome, home, { fs });
}

/** A POSIX shell word for *value*. */
function shellWord(value) {
  return `'${String(value).split("'").join("'\\''")}'`;
}

/** An AppleScript string literal body for *value*. */
function appleScriptString(value) {
  return String(value).split("\\").join("\\\\").split("\"").join("\\\"");
}

/**
 * The [file, args] that run the privileged chown through *route*. As root it
 * changes only entries another account owns, on the home's own filesystem,
 * never a symlink's target (`chown -h`), and never a non-directory with a
 * second hard link: a link planted in the home to a root-owned file would
 * otherwise hand that file to this account.
 */
function elevatedCommand(route, uid, target) {
  if (!Number.isInteger(uid) || uid <= 0) throw new Error("no ordinary account to give the files to");
  const id = String(uid);
  const chown = [
    route.find, target, "-xdev", "!", "-user", id,
    "(", "-type", "d", "-o", "-links", "1", ")",
    "-exec", route.chown, "-h", id, "{}", "+",
  ];
  if (route.kind === "osascript") {
    const line = chown.map(shellWord).join(" ");
    return [route.bin, ["-e", `do shell script "${appleScriptString(line)}" with administrator privileges`]];
  }
  return [route.bin, chown];
}

function run(execFile, file, args) {
  return new Promise((resolve) => {
    execFile(file, args, { timeout: 120_000 }, (error, _stdout, stderr) => {
      resolve({ error, stderr: String(stderr || "") });
    });
  });
}

/**
 * Ask the OS for admin rights and give the data home back to this account.
 * Resolves { ok: true } or { ok: false, reason: "unavailable" | "cancelled" | "failed" }.
 */
async function fixDataHome({
  gatewayHome,
  platform = process.platform,
  fs = nodeFs,
  path = nodePath,
  execFile = childProcess.execFile,
  getuid = () => (typeof process.getuid === "function" ? process.getuid() : -1),
  home = resolveHome(),
  log = () => {},
} = {}) {
  const route = elevationRoute({ platform, fs });
  const uid = getuid();
  if (!route || !Number.isInteger(uid) || uid <= 0) return { ok: false, reason: "unavailable" };
  if (!sameHome(gatewayHome, home, { fs })) {
    log("data-home:fix refused: the gateway runs on another data home");
    return { ok: false, reason: "unavailable" };
  }
  let target;
  try {
    target = repairTarget(home, { fs, path });
  } catch (error) {
    log(`data-home:fix refused: ${error.message}`);
    return { ok: false, reason: "failed" };
  }
  const [file, args] = elevatedCommand(route, uid, target);
  const elevated = await run(execFile, file, args);
  if (elevated.error) {
    const cancelled = route.kind === "osascript"
      ? elevated.stderr.includes(OSASCRIPT_CANCELLED)
      : PKEXEC_CANCELLED_CODES.has(elevated.error.code);
    log(`data-home:fix ${cancelled ? "cancelled" : "failed"}: ${elevated.error.message}`);
    return { ok: false, reason: cancelled ? "cancelled" : "failed" };
  }
  // The owner is fixed now, but a mode left unreadable still blocks the app, so
  // only a finished chmod counts as fixed.
  const chmod = firstExisting(CHMOD_CANDIDATES, fs);
  if (!chmod) {
    log("data-home:fix failed: no chmod found");
    return { ok: false, reason: "failed" };
  }
  const restored = await run(execFile, chmod, ["-R", "u+rwX", target]);
  if (restored.error) {
    log(`data-home:fix chmod failed: ${restored.error.message}`);
    return { ok: false, reason: "failed" };
  }
  return { ok: true };
}

module.exports = {
  elevationRoute,
  repairTarget,
  elevatedCommand,
  sameHome,
  canFix,
  fixDataHome,
};
