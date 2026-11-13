"use strict";

const { getRemoteHostConfig } = require("./host-config");
const { validateRemoteSettings } = require("./validation");

/** Wait this long before respawning a keeper process that died on its own. */
const RESPAWN_DELAY_MS = 5000;

/**
 * How long a pane waits for its forward before it loads anyway. ssh's own
 * connect budget is 15s (DEFAULT_CONNECT_TIMEOUT_SECS on the Python side); a
 * little over it lets one full attempt land.
 */
const PANE_FORWARD_WAIT_MS = 20000;

/** Pane ports one window may ask for; the keeper holds at most this many too. */
const MAX_PANE_PORTS = 32;

/**
 * The keeper's report about one pane forward (see tunnel_keeper.py):
 * `tunnel: pane <port> <state>`, where a `down` or `refused` state is followed
 * by a space and its reason.
 */
const PANE_LINE_PREFIX = "tunnel: pane ";
const PANE_STATES = new Set(["connected", "down", "refused"]);
const PANE_PORT_MAX_DIGITS = 5;

/** `{ port, state }` for a keeper's pane report, else null. */
function parsePaneReport(line) {
  if (!line.startsWith(PANE_LINE_PREFIX)) return null;
  const rest = line.slice(PANE_LINE_PREFIX.length);
  const portEnd = rest.indexOf(" ");
  if (portEnd < 1 || portEnd > PANE_PORT_MAX_DIGITS) return null;
  const digits = rest.slice(0, portEnd);
  for (const ch of digits) if (ch < "0" || ch > "9") return null;
  const tail = rest.slice(portEnd + 1);
  const stateEnd = tail.indexOf(" ");
  const state = stateEnd === -1 ? tail : tail.slice(0, stateEnd);
  if (!PANE_STATES.has(state)) return null;
  return { port: Number(digits), state };
}

/** The distinct valid pane ports in `ports`, never the dashboard's own `port`. */
function cleanPanePorts(ports, port) {
  if (!Array.isArray(ports)) return [];
  const clean = new Set();
  for (const value of ports) {
    if (Number.isInteger(value) && value >= 1 && value <= 65535 && value !== port) clean.add(value);
    if (clean.size >= MAX_PANE_PORTS) break;
  }
  return [...clean].sort((a, b) => a - b);
}

/**
 * The SSH forward a client-only launch reaches its remote crew through, kept
 * open by this app instead of by a terminal the user has to remember.
 *
 * The forward itself is supervised by `kirocrew desktop tunnel`, which runs the
 * same tunnel supervisor and backoff Remote Crew uses inside a gateway. This
 * module only owns that one process: start it when the crew opted in, respawn
 * it if it dies, bounce it on wake so the forward is rebuilt at once rather than
 * after ssh's keepalive notices the dead connection, and stop it on quit. Its
 * stdin is held open as a lifeline, so the forward cannot outlive this app
 * however the app exits.
 *
 * The same keeper also carries the Remote Crew panes that crew's dashboard
 * frames. Each pane loads `localhost:<port>`, and that port is a forward the
 * crew's gateway opened on ITS machine, so from here nothing answers on it
 * until the keeper forwards it too. Every window says which pane ports it
 * shows (`forwardPanePorts`), the keeper is asked for their union over its
 * stdin, and a port no window shows any more is dropped. A replaced keeper is
 * asked again for every port still shown.
 *
 * Not offered on Windows: the ssh transport the supervisor spawns is not yet
 * supported there.
 */
function createTunnelKeeper({
  store,
  port,
  spawn,
  resolveBin,
  getEnv = () => ({}),
  isWindows = false,
  log = () => {},
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}) {
  let child = null;
  let activeKey = "";
  let respawnTimer = null;
  let wanted = false;
  // Set when this launch found nothing carrying the port of a crew that did not
  // opt in (see startForUnansweredPort); cleared by stop().
  let unanswered = false;
  // Set once the user saves this port's crew in this launch: the saved choice
  // then stands, so a later probe in the same launch (Retry) never reopens a
  // forward the save left unticked. stop() does not clear it.
  let saved = false;
  // Pane ports per asking window, the ports the current keeper was asked to
  // hold, what it last reported for each, and the requests waiting on one.
  const paneOwners = new Map();
  let paneAsked = new Set();
  let paneState = new Map();
  const paneWaiters = new Map();

  /**
   * The crew's coordinates when this port opted into a managed tunnel, or
   * nothing else carries it this launch, else null.
   */
  function managedTarget() {
    if (isWindows) return null;
    const config = getRemoteHostConfig(store, port);
    if (!config || !config.host) return null;
    if (config.manageTunnel !== true && !unanswered) return null;
    const error = validateRemoteSettings(
      config.host,
      config.binPath,
      config.remotePort,
      config.remotePath,
    );
    if (error) {
      log(`tunnel: not keeping a forward to ${config.host}: ${error}`);
      return null;
    }
    return { host: config.host, remotePort: String(config.remotePort || port) };
  }

  function spawnKeeper(target) {
    const bin = resolveBin();
    const args = [
      "desktop", "tunnel",
      "--host", target.host,
      "--local-port", String(port),
      "--remote-port", target.remotePort,
    ];
    log(`tunnel: keeping 127.0.0.1:${port} -> ${target.host}:${target.remotePort} open (${bin})`);
    let proc;
    try {
      proc = spawn(bin, args, { stdio: ["pipe", "pipe", "pipe"], env: getEnv(), windowsHide: true });
    } catch (error) {
      log(`tunnel: could not start the keeper: ${error && error.message}`);
      scheduleRespawn();
      return;
    }
    child = proc;
    forgetPaneReports();
    // Whole lines only: a pane report split across two chunks must still be read.
    const lineRelay = () => {
      let partial = "";
      return (chunk) => {
        const lines = (partial + String(chunk)).split("\n");
        partial = lines.pop();
        for (const raw of lines) {
          const line = raw.trim();
          if (!line) continue;
          log(line);
          if (child === proc) notePaneReport(line);
        }
      };
    };
    if (proc.stdout) proc.stdout.on("data", lineRelay());
    if (proc.stderr) proc.stderr.on("data", lineRelay());
    // A write to a closed lifeline is how an exited keeper looks from here; it
    // is reported by the exit handler, not as an uncaught stream error.
    if (proc.stdin) proc.stdin.on("error", () => {});
    // A keeper replaced by restart() or stop() is no longer this module's
    // concern; only the current one's death calls for a respawn. A spawn that
    // fails asynchronously (ENOENT, EAGAIN) emits 'error' and never 'exit', so
    // both events end the same way.
    const lost = (reason) => {
      if (child !== proc) return;
      child = null;
      forgetPaneReports();
      if (!wanted) return;
      log(`tunnel: keeper ${reason}; restarting`);
      scheduleRespawn();
    };
    proc.on("error", (error) => lost(`error: ${error && error.message}`));
    proc.on("exit", (code, signal) => lost(`exited (code=${code} signal=${signal})`));
    syncPanePorts();
  }

  function settlePane(panePort, state) {
    const waiters = paneWaiters.get(panePort);
    if (!waiters) return;
    paneWaiters.delete(panePort);
    for (const waiter of waiters) waiter(state);
  }

  /**
   * What a keeper reported dies with it: a replacement is asked for every
   * port afresh, and a request waits for that answer, not the old one.
   */
  function forgetPaneReports() {
    paneAsked = new Set();
    paneState = new Map();
  }

  function notePaneReport(line) {
    const report = parsePaneReport(line);
    if (!report) return;
    const panePort = report.port;
    if (!paneAsked.has(panePort)) return;
    const state = report.state === "connected" ? "up" : "down";
    paneState.set(panePort, state);
    settlePane(panePort, state);
  }

  function writeToKeeper(line) {
    try {
      if (child && child.stdin) child.stdin.write(`${line}\n`);
    } catch { /* a dead keeper is reported by its exit handler */ }
  }

  /** Ask the current keeper for every pane port some window shows, and drop the rest. */
  function syncPanePorts() {
    if (!child) return;
    const union = new Set();
    for (const ports of paneOwners.values()) for (const p of ports) union.add(p);
    for (const p of paneAsked) {
      if (union.has(p)) continue;
      paneAsked.delete(p);
      paneState.delete(p);
      writeToKeeper(`drop ${p}`);
      settlePane(p, "down");
    }
    for (const p of union) {
      if (paneAsked.has(p)) continue;
      paneAsked.add(p);
      writeToKeeper(`forward ${p}`);
    }
  }

  /** "up" or "down" once the keeper has reported on `panePort`, or after the wait. */
  function waitForPane(panePort) {
    const known = paneState.get(panePort);
    if (known) return Promise.resolve(known);
    return new Promise((resolve) => {
      let timer = null;
      const done = (state) => {
        if (timer) clearTimeoutFn(timer);
        timer = null;
        const waiters = paneWaiters.get(panePort);
        if (waiters) {
          waiters.delete(done);
          if (waiters.size === 0) paneWaiters.delete(panePort);
        }
        resolve(state);
      };
      if (!paneWaiters.has(panePort)) paneWaiters.set(panePort, new Set());
      paneWaiters.get(panePort).add(done);
      timer = setTimeoutFn(() => {
        timer = null;
        done("down");
      }, PANE_FORWARD_WAIT_MS);
    });
  }

  /**
   * Record the pane ports window `owner` shows now and have the keeper carry
   * them. Resolves `{port: "up" | "down"}` for each once the keeper has
   * reported on it (or the wait ran out), so the window loads a pane only once
   * its port answers here. Resolves null when this app keeps no forward for
   * the launch port, where there is nothing to ask.
   */
  function forwardPanePorts(owner, ports) {
    const clean = cleanPanePorts(ports, port);
    // Recorded with no forward kept too: a window asks once and is answered
    // null, so a keeper started later (the crew opting in without a reload)
    // must already know what that window shows.
    if (clean.length) paneOwners.set(owner, new Set(clean));
    else paneOwners.delete(owner);
    if (!wanted) return Promise.resolve(null);
    syncPanePorts();
    return Promise.all(clean.map(waitForPane)).then((states) =>
      Object.fromEntries(clean.map((p, i) => [p, states[i]])));
  }

  /** Forget window `owner`'s pane ports (it closed); ports no window shows are dropped. */
  function releasePaneForwards(owner) {
    if (!paneOwners.delete(owner)) return;
    syncPanePorts();
  }

  function scheduleRespawn() {
    if (respawnTimer || !wanted) return;
    respawnTimer = setTimeoutFn(() => {
      respawnTimer = null;
      if (wanted && !child) start();
    }, RESPAWN_DELAY_MS);
  }

  /**
   * Bring the keeper in line with what this port's crew asks for now: start it
   * when the crew opted in, replace it when the host or remote port changed
   * since it was spawned, and stop it when the crew has opted out, so an edit
   * takes effect at once rather than at the next relaunch.
   */
  function start() {
    const target = managedTarget();
    if (!target) {
      if (wanted || child) stop();
      return false;
    }
    wanted = true;
    const key = `${target.host}\n${target.remotePort}`;
    if (child && key !== activeKey) {
      log(`tunnel: crew moved to ${target.host}:${target.remotePort}; replacing the forward`);
      const proc = child;
      child = null;
      terminate(proc);
    }
    if (!child) {
      activeKey = key;
      spawnKeeper(target);
    }
    return true;
  }

  function terminate(proc) {
    try { if (proc.stdin) proc.stdin.end(); } catch { /* already closed */ }
    try { proc.kill("SIGTERM"); } catch { /* already gone */ }
  }

  /**
   * Keep the forward for this launch although the crew did not opt in. Called
   * only once a probe found nothing answering the port, so no VPN, kubectl or
   * ssh forward of the user's own is carrying it and none is taken over. A
   * client-only crew saved with the option unticked would otherwise reach
   * nothing on every launch; this keeps it reachable with no change to what is
   * stored. Lasts until stop() or applySavedChoice(), so a wake keeps it.
   *
   * @returns {boolean} whether a forward is now kept.
   */
  function startForUnansweredPort() {
    if (saved) return false;
    unanswered = true;
    if (start()) return true;
    unanswered = false;
    return false;
  }

  /**
   * Re-apply what the user just saved for this port. A save states the choice
   * again, so a forward kept only because the port was unanswered at launch
   * ends when the save leaves the option unticked, and no later probe in this
   * launch opens one again.
   *
   * @returns {boolean} whether a forward is now kept.
   */
  function applySavedChoice() {
    saved = true;
    unanswered = false;
    return start();
  }

  /** Stop the keeper and its forwards; nothing respawns until start() again. */
  function stop() {
    wanted = false;
    unanswered = false;
    if (respawnTimer) {
      clearTimeoutFn(respawnTimer);
      respawnTimer = null;
    }
    const proc = child;
    child = null;
    forgetPaneReports();
    if (proc) terminate(proc);
    // No keeper will answer them now: a waiting pane loads at once.
    for (const panePort of [...paneWaiters.keys()]) settlePane(panePort, "down");
  }

  /**
   * Rebuild the forward now. After sleep the old ssh can look alive for up to
   * its keepalive window while carrying nothing, so waiting for it to notice
   * is exactly the dead dialog this exists to avoid.
   */
  function restart() {
    if (!wanted) return;
    const proc = child;
    child = null;
    forgetPaneReports();
    if (proc) terminate(proc);
    start();
  }

  return { start, startForUnansweredPort, applySavedChoice, stop, restart, forwardPanePorts, releasePaneForwards };
}

module.exports = { createTunnelKeeper, RESPAWN_DELAY_MS, PANE_FORWARD_WAIT_MS, MAX_PANE_PORTS };
