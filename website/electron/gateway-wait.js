"use strict";
//
// Pure, injectable gateway-readiness logic, extracted from main.js so it can be
// unit-tested without Electron (mirrors gateway-stop.js). main.js binds the
// real checkBackend / window / status / failure-flag to these helpers.
//
// The headline behavior is FAIL-FAST: a bundled gateway that EXITS during
// startup (e.g. ModuleNotFoundError, or SIGKILL from Gatekeeper) will never
// become healthy, so polling its port for the full timeout is dead time behind
// a misleading "Waiting for gateway…" message. As soon as the spawn watcher
// reports a terminal exit we reject immediately (~1s) and surface the cause +
// the launch log. The maxWaitMs timeout stays purely as the backstop for a
// gateway that is alive but slow to bind its port.

const DEFAULT_GATEWAY_WAIT_MS = 30_000;
// A first launch imports a freshly installed bundled Python tree and probes its
// MCP servers before the gateway binds, which can exceed the ordinary connection
// deadline on any platform. Keep the splash responsive through that cold start
// without weakening the fail-fast child-exit path above.
const LOCAL_GATEWAY_WAIT_MS = 120_000;
// An LTS gateway checks the kiro login and loads the governance policy
// before it binds (kiro_crew/lts_boot.py), and exits if either fails.
// Their own limits: kiro-cli `--version` 10s and `whoami` 30s
// (kiro_prerequisite_probe.py), the policy fetch 10s (governance
// DEFAULT_FETCH_TIMEOUT_SECS). A signed-in user whose token refresh is slow
// must not see "can't reach the gateway" while those run, so an LTS
// local gateway gets them on top of its ordinary budget, plus headroom. A
// failed check still ends the wait at once: the child exits.
const LTS_PRE_BIND_CHECKS_MS = 50_000;
const LTS_PRE_BIND_HEADROOM_MS = 30_000;

/**
 * Pick the readiness deadline for the connection being opened.
 *
 * Only the primary local gateway gets the extended cold-start budget, and, in
 * an LTS build, the pre-bind check budget on top. Connection
 * tabs still fail on the ordinary deadline, and every spawned child exit still
 * wins immediately through `getFailure`.
 *
 * @param {{watchSpawn?: boolean, edition?: string}} [o]
 * @returns {number}
 */
function gatewayWaitTimeoutMs({
  watchSpawn = false,
  edition = "oss",
} = {}) {
  const base = watchSpawn ? LOCAL_GATEWAY_WAIT_MS : DEFAULT_GATEWAY_WAIT_MS;
  return watchSpawn && edition === "lts"
    ? base + LTS_PRE_BIND_CHECKS_MS + LTS_PRE_BIND_HEADROOM_MS
    : base;
}

/**
 * Poll `checkBackend` until the gateway answers, the spawned child reports a
 * terminal failure, the window closes, or we hit maxWaitMs.
 *
 * @param {object}   o
 * @param {() => Promise<void>} o.checkBackend  resolve = healthy, reject = not yet
 * @param {() => (object|null)} [o.getFailure]  returns a truthy failure record
 *                                              ({code,signal} or {error}) once the
 *                                              spawned gateway has exited; null while
 *                                              it is still alive / never spawned
 * @param {() => boolean} [o.isWindowAlive]     false => abort (window closed)
 * @param {(msg: string) => void} [o.onStatus]  progress callback
 * @param {() => number} [o.now]                injectable clock (tests)
 * @param {(fn: Function, ms: number) => any} [o.setTimeoutFn]  injectable timer (tests)
 * @param {number} [o.maxWaitMs=30000]
 * @param {number} [o.pollIntervalMs=500]
 * @returns {Promise<void>} resolves when healthy; rejects with a tagged Error
 *   where `err.kind` is one of 'failed' | 'timeout' | 'window-closed', and for
 *   'failed' `err.failure` carries the exit record.
 */
function waitForGateway({
  checkBackend,
  getFailure = () => null,
  isWindowAlive = () => true,
  onStatus = () => {},
  now = Date.now,
  setTimeoutFn = setTimeout,
  maxWaitMs = DEFAULT_GATEWAY_WAIT_MS,
  pollIntervalMs = 500,
}) {
  const start = now();
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (!isWindowAlive()) {
        const e = new Error("Window closed");
        e.kind = "window-closed";
        return reject(e);
      }
      // Check the spawned child's terminal state BEFORE the timeout: a process
      // that already exited is never coming back, so fail now rather than
      // waiting out the clock.
      const failure = getFailure();
      if (failure) {
        const e = new Error(describeGatewayFailure(failure));
        e.kind = "failed";
        e.failure = failure;
        return reject(e);
      }
      if (now() - start > maxWaitMs) {
        const e = new Error("Backend timeout");
        e.kind = "timeout";
        return reject(e);
      }
      onStatus(`Waiting for gateway… ${Math.round((now() - start) / 1000)}s`);
      checkBackend()
        .then(() => { onStatus("Connected ✓"); resolve(); })
        .catch(() => setTimeoutFn(poll, pollIntervalMs));
    };
    poll();
  });
}

// A port holder's command is shown on one line, cut to this many characters:
// room for an `ssh -L` forward and its host, short of a wall of arguments.
const HOLDER_COMMAND_MAX_CHARS = 120;

function holderCommandText(command) {
  const line = String(command).replace(/\s+/g, " ").trim();
  return line.length > HOLDER_COMMAND_MAX_CHARS
    ? `${line.slice(0, HOLDER_COMMAND_MAX_CHARS - 3)}...`
    : line;
}

/**
 * One-line, human-readable reason for a gateway start failure. The SIGKILL case
 * carries the Gatekeeper hint because an unsigned/quarantined nested executable
 * being killed on launch is the most common "works for me, not my friend" mode.
 *
 * @param {{code?: number|null, signal?: string|null, error?: string, disabled?: boolean, port?: number, remoteHost?: string, remotePort?: string, canStartHere?: boolean, localStartFailed?: boolean, localStartPortBusy?: number, holders?: Array<{pid: number, command: string|null}>, isWindows?: boolean}|null} failure
 * @returns {string}
 */
function describeGatewayFailure(failure) {
  if (!failure) return "The gateway failed to start.";
  // Nothing was launched, so there is no exit code and no log to read: the port
  // was silent and this app is set not to start a gateway here. Naming both
  // halves matters because either one alone is a normal, working state.
  //
  // Deliberately does not send the user to Settings for the fix: the page
  // holding that switch is served by a gateway, which is the thing not running.
  // The error dialog carries a button instead. Settings is named only as the way
  // back afterwards, which is reachable precisely because a gateway is serving
  // by then. On a remote crew's port that button cannot start a gateway in
  // place, so it restarts the app and lets port selection pick a local port;
  // this message names the host to check first, because reaching the crew is
  // what the user asked for.
  // `localStartBlocked` joins this family rather than overloading `disabled`:
  // the setting is ON in that state and nothing was launched anyway, because
  // the port this launch targets is not one a new gateway may bind.
  if (failure.disabled || failure.localStartBlocked) {
    if (failure.remoteHost) {
      // The crew binds its own port on its own machine; this app only holds the
      // local end of the link. Naming the local port here would send the user to
      // check a port nothing over there was ever expected to serve.
      const target = failure.remotePort
        ? `${failure.remoteHost}:${failure.remotePort}, reached through local port ${failure.port},`
        : `${failure.remoteHost} on port ${failure.port},`;
      // One exit per message, not a conditional the reader has to resolve. The
      // composer knows whether it is rendering the button and says so on the
      // record, so this names the route that is actually open. The two ways the
      // button closes get their own wording: one never offered the restart, the
      // other ran it and it did not finish, and a user who just watched that
      // restart must not be told restarting is unavailable.
      //
      // The clause about the setting has to follow it. Using the button turns the
      // setting on, so after that "set not to start a gateway" is false, and a
      // paragraph asserting both that and "the setting stays on" cannot be
      // reconciled by the reader. Both post-click states describe the launch
      // instead, which stays true either way.
      const startedHere = failure.localStartPortBusy || failure.localStartFailed
        || failure.localStartBlocked || failure.localStartUnverifiable
        ? "this launch did not start one here"
        : "Kiro Crew is set not to start a gateway on this machine";
      const reachTheCrew = `Nothing is answering at ${target} and ${startedHere}. `
        + `Start the gateway on `
        + `${failure.remoteHost}, or re-establish the tunnel or port-forward that `
        + `reaches it, then retry to reach ${failure.remoteHost} again. If that `
        + "address is wrong, choose Edit Remote Crew to correct it.";
      if (failure.localStartUnverifiable) {
        // Ahead of every other post-click branch, because it is the only one that
        // must not offer the button. The restart ran and its gateway answered;
        // what this app could not do is establish which process holds the port,
        // and that outcome is a property of the host, so the same click reaches
        // the same place. Saying "never served one" would deny what the user
        // watched.
        //
        // The route named is the explicit one, not "quit and reopen". This branch
        // is only reachable with a crew configured on the port this launch
        // targets, and the launch targets that port on purpose, so a plain reopen
        // lands on the crew-configured refusal and starts nothing -- the same
        // route that branch names is the one that works here too.
        return `Starting a local gateway here got as far as answering on port `
          + `${failure.localStartUnverifiable}, but this app could not check which `
          + "program holds that port, so it stopped the one it had started rather "
          + "than hand over to something it could not identify. Choosing Start "
          + "Local Gateway again reaches the same check."
          + "\n\nTo run one here, quit Kiro Crew and start it from a terminal with "
          + "the environment variable KIROCREW_PORT set to a port number that has "
          + `no remote host configured.\n\n${reachTheCrew}`;
      }
      if (failure.localStartFailed) {
        // A reopened dialog whose opening sentences are identical reads as
        // nothing having happened, so the branch that exists to acknowledge the
        // attempt leads with it and the unchanged crew advice follows.
        //
        // Ahead of the busy-port branch deliberately: a record survives every
        // attempt, so a run that reached a restart and lost it is the newer fact
        // and must win over a port that was busy on an earlier click. The
        // supervisor clears the earlier field as well, and this ordering means a
        // path that forgets to still cannot describe a withdrawn button.
        return "Starting a local gateway here did not finish: the restarted app "
          + "never served one. The \"Run a local gateway\" setting stays on, so "
          + "choose Start Local Gateway to try again. It will pick a free port."
          + `\n\n${reachTheCrew}`;
      }
      if (failure.localStartPortBusy) {
        // Nothing was restarted: the port the restart would have used is already
        // served by something else, so this names that port instead of reporting
        // an attempt. It leads with what happened for the same reason the failed
        // attempt does. The button is still rendered, because freeing that port
        // is outside this app and makes the same click work.
        //
        // "on this computer" because the title names the crew's port and these two
        // numbers are easy to read as one.
        return `Starting a local gateway here did not begin: port `
          + `${failure.localStartPortBusy} on this computer is already served by `
          + "something else, so restarting would not have produced a gateway this "
          + `app can tell apart from it. That is usually another Kiro Crew `
          + `started from a terminal, so quit that one. Then choose Start Local `
          + `Gateway again. `
          + `The "Run a local gateway" setting stays on.\n\n${reachTheCrew}`;
      }
      if (failure.localStartBlocked === "crew-configured") {
        // The port is not busy and nothing failed: it belongs to the crew, so a
        // gateway bound there could not be told apart from it. Only a fresh
        // process can take a different port, which is what the button does, so
        // the remedy is the button rather than an instruction carried out by hand.
        //
        // Worded as a reservation rather than as the mechanism. A cold reader of
        // the mechanism ("would be taken for that crew") reported not following
        // it, and what they need is which port is unavailable and what to press;
        // the mechanism belongs in the code and the description.
        // Two states share this title and only one renders the button, so the one
        // that withholds it says why -- the sibling branch below already does, and
        // without the clause the screen gives a reader no way to tell the two
        // apart. It leads the remedy, because "why not" has to come before "so
        // instead".
        // Each remedy is a whole sentence, because the two do not share a
        // lead-in: one names a button, the other has to say first that the button
        // is unavailable. "To run one here instead, restarting the app is not
        // available" was the dangling result of sharing one.
        const remedy = failure.canStartHere
          ? "To run one here instead, choose Start Local Gateway, which starts "
            + "one on a free port."
          : "Restarting the app to pick a free port is not available here, so to "
            + "run one here instead, quit Kiro Crew and start it from a terminal "
            + "with the environment variable KIROCREW_PORT set to a port number "
            + "that has no remote host configured.";
        return `${reachTheCrew}\n\nPort ${failure.port} is reserved for reaching `
          + `${failure.remoteHost}, so Kiro Crew did not start a local gateway on `
          + `it. ${remedy} Your sessions on ${failure.remoteHost} stay there. `
          + `The "Run a local gateway" setting stays on.`;
      }
      if (failure.canStartHere) {
        return `${reachTheCrew}\n\nTo run one on this machine instead, choose Start `
          + "Local Gateway: it turns the setting back on and restarts the app so it "
          + `picks a free port on this computer. Your sessions on ${failure.remoteHost} `
          + "stay there; the local gateway is a separate one. Once it is running you "
          + "can turn that setting back off in Settings.";
      }
      return `${reachTheCrew}\n\nRestarting the app to pick a local port is not `
        + "available here, so to run one on this machine instead, quit Kiro Crew "
        + "and start it from a terminal with the environment variable "
        + "KIROCREW_PORT set to a port number that has no remote host configured.";
    }
    if (failure.localStartBlocked === "foreign-holder") {
      // Something answers there and this app can attribute it neither to itself
      // nor to a crew recorded on that port. Adopting it would post this
      // machine's local secret to it, because the mint only requires a literal
      // loopback origin and a tunnel's local end is one.
      //
      // Worded as what this app knows rather than as what the holder is: it may
      // well BE a Kiro Crew gateway, reached over a tunnel from another machine.
      // That is why recording it as a crew is the first remedy -- doing so makes
      // the same port adoptable on the next launch -- and quitting the program is
      // the other. Both controls are in this dialog.
      //
      // Each holder the owner probe found is named, with the command that ends
      // it, because nothing else on screen says which program to quit. A holder
      // whose command could not be read is still named by its pid.
      const holders = Array.isArray(failure.holders) ? failure.holders : [];
      const named = holders.map(({ pid, command }) => (command
        ? `process ${pid} (${holderCommandText(command)})`
        : `process ${pid}, whose command could not be read`));
      const pids = holders.map(({ pid }) => pid);
      const endCommand = failure.isWindows
        ? `taskkill ${pids.map((pid) => `/PID ${pid}`).join(" ")} /F`
        : `kill -TERM ${pids.join(" ")}`;
      const endIt = pids.length
        ? `\n\nTo end ${pids.length > 1 ? "them" : "it"} from a terminal, run this, `
          + `then retry:\n${endCommand}`
        : "";
      return `Port ${failure.port} on this computer is served by a program this `
        + "app did not start, and Kiro Crew has no remote crew saved for that "
        + "port, so this launch did not use it."
        + (named.length ? ` It is held by ${named.join("; ")}.` : "")
        + "\n\nIf that program is how you "
        + "reach one of your crews, choose Add Remote Crew and fill in its "
        + "address: that saves the address so this port can be used next time, "
        + "and you can change or clear it later. If it is unrelated software, "
        + "quit it and then retry. An SSH tunnel left running from an earlier "
        + "connection, for example across a VPN reconnect, can hold the port "
        + `this way.${endIt}`;
    }
    return `No gateway is answering on port ${failure.port}, and Kiro Crew is set `
      + "not to start one on this machine. Start the gateway you connect to (or "
      + "the connection that reaches it) and retry, or start one here. If your "
      + "crew runs on another machine, choose Add Remote Crew to name it.";
  }
  // An incomplete bundle is not a launch failure — the installer is still writing
  // the backend. That message already explains the state and names Retry, so pass
  // it through bare; prefixing "could not be launched" would open with failure
  // vocabulary under a title saying the install is still finishing.
  if (failure.incompleteBundle && failure.error) return failure.error;
  if (failure.error) return `The gateway could not be launched: ${failure.error}`;
  if (failure.signal === "SIGKILL") {
    return "The gateway was killed on launch (SIGKILL). On macOS this usually "
      + "means Gatekeeper blocked an unsigned or quarantined executable — try: "
      + "xattr -cr <path to KiroCrew.app>";
  }
  if (failure.signal) {
    return `The gateway exited on launch (signal ${failure.signal}).`;
  }
  return `The gateway exited on launch (code ${failure.code}). `
    + "The cause is in the launch log below.";
}

/**
 * Last `n` lines of `text`, with trailing blank lines trimmed — for inlining a
 * launch-log tail into an error dialog. Pure (no fs) so it is trivially tested.
 *
 * @param {string} text
 * @param {number} [n=20]
 * @returns {string}
 */
function tailLines(text, n = 20) {
  if (!text) return "";
  const lines = String(text).replace(/\s+$/, "").split("\n");
  return lines.slice(Math.max(0, lines.length - n)).join("\n");
}

// How much of the end of the launch log a dialog reads. 60 lines of launch log
// is far under this, so the tail is never short-changed; and it is a hard cap
// on the read regardless of how large the file has grown, which is the point.
const DEFAULT_TAIL_BYTES = 256 * 1024;

/**
 * Last `n` lines of a log file, reading only the final `maxBytes` of it rather
 * than the whole file. The two failure dialogs call this in Electron's MAIN
 * process the moment the gateway fails, where the launch log can be hundreds of
 * MB on a long-lived install because it only ever appends. Reading at most
 * `maxBytes` from the end keeps the cost flat whatever the size, and keeps the
 * synchronous read off V8's string limit, past which a whole-file `readFileSync`
 * throws and the dialog shows a blank log pane.
 *
 * A read that starts mid-file can slice a multi-byte UTF-8 character; `tailLines`
 * drops that first partial line as a side effect of keeping only the last `n`.
 *
 * Returns "" on any error (no file yet, unreadable), matching the swallow-and-
 * show-empty behaviour the callers already had.
 *
 * @param {string} filePath
 * @param {object} deps
 * @param {typeof import("fs")} deps.fs  needs statSync/openSync/readSync/closeSync/readFileSync
 * @param {number} [deps.lines=60]
 * @param {number} [deps.maxBytes=DEFAULT_TAIL_BYTES]
 * @returns {string}
 */
function readLogTail(filePath, { fs, lines = 60, maxBytes = DEFAULT_TAIL_BYTES } = {}) {
  try {
    const size = fs.statSync(filePath).size;
    if (size <= maxBytes) {
      // Small log: read it whole, same bytes (and so the same 60-line tail) as
      // before. No partial-character slice is possible on this path.
      return tailLines(fs.readFileSync(filePath, "utf8"), lines);
    }
    const start = size - maxBytes;
    const buffer = Buffer.allocUnsafe(maxBytes);
    const fd = fs.openSync(filePath, "r");
    try {
      let offset = 0;
      while (offset < maxBytes) {
        const read = fs.readSync(fd, buffer, offset, maxBytes - offset, start + offset);
        if (read <= 0) break;
        offset += read;
      }
      return tailLines(buffer.toString("utf8", 0, offset), lines);
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return "";
  }
}

// Rotation cap for gateway-launch.log: many weeks of normal launch entries
// (the field grows ~1.66 MB/day). One previous generation is kept, so at most
// two files worth of this cap stay on disk.
const DEFAULT_LAUNCH_LOG_CAP_BYTES = 10 * 1024 * 1024;
// Matches chromium.previous.log's one-generation naming.
const LAUNCH_LOG_PREVIOUS_SUFFIX = ".previous.log";

/**
 * One-generation rotation of the launch log, in the same shape `rotateNativeLog`
 * uses for chromium.log: rename the current file aside so a fresh one starts
 * clean, keeping exactly one previous generation. The launch log has no cap and
 * only ever appends, so without this a long-lived install grows it without limit.
 *
 * Must run only when a FRESH gateway is about to be spawned, just before the new
 * child opens its descriptor on the log, and never when a surviving gateway is
 * reused. A reused gateway is still writing to this file — off Windows through a
 * descriptor it inherited at spawn, on Windows through the path it was handed —
 * so renaming under it would leave it writing to the renamed file while the
 * dialogs read the fresh one. Rotating only on a fresh spawn means no live writer
 * holds the file at the moment of the rename.
 *
 * Size-gated: a log under the cap is left untouched so a short log keeps
 * accumulating across launches. The previous generation is renamed, not deleted,
 * so the content users are asked to attach to bug reports is still on disk.
 *
 * Never throws — losing a rotation is a log line, not a failed launch.
 *
 * @param {string} logPath
 * @param {object} deps
 * @param {typeof import("fs")} deps.fs  needs statSync/renameSync
 * @param {number} [deps.capBytes=DEFAULT_LAUNCH_LOG_CAP_BYTES]
 * @param {(msg: string) => void} [deps.log]
 * @returns {{rotated: boolean, previousPath: string|null}}
 */
function rotateLaunchLog(logPath, { fs, capBytes = DEFAULT_LAUNCH_LOG_CAP_BYTES, log = () => {} } = {}) {
  try {
    const size = fs.statSync(logPath).size;
    if (size <= capBytes) return { rotated: false, previousPath: null };
    // Strip a trailing ".log" so the previous generation is
    // gateway-launch.previous.log, not gateway-launch.log.previous.log.
    const previousPath = String(logPath).replace(/\.log$/i, "") + LAUNCH_LOG_PREVIOUS_SUFFIX;
    // renameSync replaces an existing destination on Windows too (libuv passes
    // MOVEFILE_REPLACE_EXISTING), so a prior generation is overwritten: two
    // files, not N.
    fs.renameSync(logPath, previousPath);
    return { rotated: true, previousPath };
  } catch (e) {
    // No file yet (first launch) lands here via statSync ENOENT; a read-only
    // dir or a Windows sharing violation lands here on rename. Either way the
    // launch continues and the current file, if any, stays on disk.
    if (!e || e.code !== "ENOENT") {
      log(`launch log rotate skipped at ${logPath}: ${e && e.message}`);
    }
    return { rotated: false, previousPath: null };
  }
}

/**
 * True if the log text indicates the gateway could not bind its port because
 * something already holds it (a wedged or other KiroCrew gateway). This is a
 * distinct, recoverable failure from a crash: a plain retry can't help while
 * the holder is still there, but force-stopping it can. Pure (no fs/network).
 *
 * @param {string} text  launch-log tail
 * @returns {boolean}
 */
function isPortInUse(text) {
  if (!text) return false;
  return /address already in use|already in use|EADDRINUSE/i.test(String(text));
}

module.exports = {
  DEFAULT_GATEWAY_WAIT_MS,
  LOCAL_GATEWAY_WAIT_MS,
  LTS_PRE_BIND_CHECKS_MS,
  LTS_PRE_BIND_HEADROOM_MS,
  gatewayWaitTimeoutMs,
  waitForGateway,
  describeGatewayFailure,
  tailLines,
  readLogTail,
  rotateLaunchLog,
  isPortInUse,
};
