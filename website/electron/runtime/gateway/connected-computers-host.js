"use strict";

/**
 * Supervise this computer's local controller for connected computers when the
 * desktop app points at a Remote Crew and no local gateway runs (issue 29949,
 * G1/G2).
 *
 * The child is `kirocrew connected-computer desktop-host`. It speaks one JSON
 * object per line on its own stdin/stdout and opens no socket, so nothing but
 * this main process can ask it anything. It holds the grant store, the OS
 * keyring adapter, the Off generation and the controller lock the local
 * gateway would hold; this module only starts it, routes requests to it, and
 * stops it.
 *
 * Lifetime:
 * - stdin is the lifeline: ending it is the graceful stop (the host stops every
 *   connector and keeps every grant); SIGTERM follows after STOP_GRACE_MS;
 * - exit EXIT_DISABLED (the local switch is off) and EXIT_ELSEWHERE (a local
 *   gateway runs this computer's connectors) are final for this launch;
 * - any other exit respawns after RESPAWN_DELAY_MS, at most MAX_RESPAWNS times
 *   in a row before the state reads "failed".
 *
 * Every dependency is injected: no electron, fs, child_process or network
 * import, so the whole lifecycle is testable with fakes.
 */

const HOST_ARGS = Object.freeze(["connected-computer", "desktop-host"]);
const EXIT_PROTOCOL = 2;
const EXIT_DISABLED = 3;
const EXIT_ELSEWHERE = 4;
const RESPAWN_DELAY_MS = 5000;
const STOP_GRACE_MS = 8000;
const MAX_RESPAWNS = 5;
const MAX_PENDING = 16;
// A reply is a few kilobytes at most; a line this long is a broken child.
const MAX_STDOUT_BUFFER = 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 30000;
// The host refuses a hello naming more, so one more is never sent.
const MAX_INSTANCES = 16;
// The same shapes desktop_host.parse_instances accepts: an SSH destination is
// never an option (leading "-") or shell text, a remote path is plain.
const SSH_HOST_RE = /^[A-Za-z0-9_.@:[\]][A-Za-z0-9_.@:[\]-]{0,252}$/;
const REMOTE_BIN_RE = /^[A-Za-z0-9_./~+-]{0,512}$/;

/**
 * The Remote Crew connections this app configured (`remoteHosts`, keyed by the
 * local port its forward listens on), in the shape the host's hello takes.
 * Only this app's own store is read: a page never names a connection. An
 * entry the host would refuse is left out here, so one bad entry does not
 * keep every other crew from being shared with.
 *
 * @param {{get: (key: string) => unknown}} store
 * @returns {Array<{id: string, name: string, ssh_host: string, remote_bin: string, remote_port: number, local_port: number}>}
 */
function instancesFromStore(store) {
  const hosts = (store && store.get("remoteHosts")) || {};
  const out = [];
  for (const [key, config] of Object.entries(hosts)) {
    if (out.length >= MAX_INSTANCES) break;
    if (!config || typeof config.host !== "string" || !SSH_HOST_RE.test(config.host)) continue;
    const localPort = Number.parseInt(key, 10);
    if (String(localPort) !== key || localPort < 1 || localPort > 65535) continue;
    const remoteBin = typeof config.binPath === "string" ? config.binPath : "";
    if (!REMOTE_BIN_RE.test(remoteBin)) continue;
    const rawRemote = String(config.remotePort || "").trim();
    const remotePort = rawRemote === "" ? 0 : Number.parseInt(rawRemote, 10);
    if (!Number.isInteger(remotePort) || remotePort < 0 || remotePort > 65535) continue;
    if (rawRemote !== "" && String(remotePort) !== rawRemote) continue;
    const name = typeof config.defaultName === "string" && config.defaultName
      ? config.defaultName.slice(0, 200)
      : config.host;
    out.push({
      id: `desktop-${localPort}`,
      name,
      ssh_host: config.host,
      remote_bin: remoteBin,
      remote_port: remotePort,
      local_port: localPort,
    });
  }
  return out;
}

function createConnectedComputersHost({
  spawn,
  resolveBin,
  getEnv = () => ({}),
  getInstances = () => [],
  isWindows = false,
  onState = () => {},
  log = () => {},
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
}) {
  let child = null;
  let wanted = false;
  let state = "stopped";
  let protection = "";
  let nextId = 1;
  let helloId = 0;
  let crashes = 0;
  let respawnTimer = null;
  let graceTimer = null;
  let buffer = "";
  // id -> { resolve, timer }
  const pending = new Map();

  function setState(next) {
    if (next === state) return;
    state = next;
    try {
      onState(next);
    } catch {
      // A listener's failure never stops the supervisor.
    }
  }

  function settle(id, reply) {
    const entry = pending.get(id);
    if (!entry) return;
    pending.delete(id);
    if (entry.timer) clearTimeoutFn(entry.timer);
    entry.resolve(reply);
  }

  function settleAll(reply) {
    for (const id of [...pending.keys()]) settle(id, reply);
  }

  function write(obj) {
    if (!child || !child.stdin) return false;
    try {
      child.stdin.write(JSON.stringify(obj) + "\n");
      return true;
    } catch (error) {
      log(`connected computers: could not write to the host: ${error && error.message}`);
      return false;
    }
  }

  function onLine(line) {
    let reply;
    try {
      reply = JSON.parse(line);
    } catch {
      // Not ours (a stray print the host could not redirect); never a reply.
      log(`connected computers: host said: ${line.slice(0, 200)}`);
      return;
    }
    if (!reply || typeof reply !== "object" || typeof reply.id !== "number") return;
    if (reply.id === helloId) {
      helloId = 0;
      if (reply.ok === true && typeof reply.state === "string") {
        protection = typeof reply.protection === "string" ? reply.protection : "";
        if (reply.state === "running") crashes = 0;
        setState(reply.state);
      } else {
        log(`connected computers: the host refused hello: ${reply.code || "unknown"}`);
        setState("failed");
      }
    }
    settle(reply.id, reply);
  }

  function onData(proc, chunk) {
    if (proc !== child) return;
    buffer += String(chunk);
    let index = buffer.indexOf("\n");
    while (index !== -1) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (line) onLine(line);
      index = buffer.indexOf("\n");
    }
    if (buffer.length > MAX_STDOUT_BUFFER) {
      log("connected computers: the host wrote an endless line; stopping it");
      buffer = "";
      try {
        proc.kill("SIGTERM");
      } catch {
        // Already gone; its exit handler runs either way.
      }
    }
  }

  function scheduleRespawn() {
    if (respawnTimer || !wanted) return;
    crashes += 1;
    if (crashes > MAX_RESPAWNS) {
      log("connected computers: the host keeps exiting; not restarting it");
      wanted = false;
      setState("failed");
      return;
    }
    setState("restarting");
    respawnTimer = setTimeoutFn(() => {
      respawnTimer = null;
      if (wanted && !child) spawnHost();
    }, RESPAWN_DELAY_MS);
  }

  function onExit(proc, code, signal) {
    if (proc !== child) return;
    child = null;
    buffer = "";
    helloId = 0;
    if (graceTimer) {
      clearTimeoutFn(graceTimer);
      graceTimer = null;
    }
    settleAll({ ok: false, code: "host_exited" });
    log(`connected computers: host exited (code=${code} signal=${signal})`);
    if (!wanted) {
      if (state !== "disabled" && state !== "elsewhere" && state !== "failed") setState("stopped");
      return;
    }
    if (code === EXIT_DISABLED) {
      wanted = false;
      setState("disabled");
      return;
    }
    if (code === EXIT_ELSEWHERE) {
      wanted = false;
      setState("elsewhere");
      return;
    }
    if (code === EXIT_PROTOCOL) {
      // The same build would make the same mistake again.
      wanted = false;
      setState("failed");
      return;
    }
    scheduleRespawn();
  }

  function spawnHost() {
    let proc;
    try {
      proc = spawn(resolveBin(), [...HOST_ARGS], {
        stdio: ["pipe", "pipe", "pipe"],
        env: getEnv(),
        windowsHide: true,
      });
    } catch (error) {
      log(`connected computers: could not start the host: ${error && error.message}`);
      scheduleRespawn();
      return;
    }
    child = proc;
    buffer = "";
    if (proc.stdout) proc.stdout.on("data", (chunk) => onData(proc, chunk));
    if (proc.stderr) {
      proc.stderr.on("data", (chunk) => {
        for (const line of String(chunk).split("\n")) {
          if (line.trim()) log(`connected computers: ${line.trim().slice(0, 500)}`);
        }
      });
    }
    // A write to an exited host's stdin is reported by its exit, not thrown.
    if (proc.stdin && typeof proc.stdin.on === "function") proc.stdin.on("error", () => {});
    proc.on("error", (error) => onExit(proc, null, `error: ${error && error.message}`));
    proc.on("exit", (code, signal) => onExit(proc, code, signal));
    setState("starting");
    helloId = nextId++;
    // No timeout: an unanswered hello ends with the host's exit.
    pending.set(helloId, { resolve: () => {}, timer: null });
    write({ id: helloId, op: "hello", instances: getInstances() });
  }

  /** Start the host; false when this computer or configuration cannot run one. */
  function start() {
    if (isWindows) {
      setState("unavailable");
      return false;
    }
    const instances = getInstances();
    if (!Array.isArray(instances) || instances.length === 0) return false;
    wanted = true;
    if (child || respawnTimer) return true;
    crashes = 0;
    spawnHost();
    return true;
  }

  /** Graceful stop: end the lifeline now, SIGTERM after the grace. */
  function stop() {
    wanted = false;
    if (respawnTimer) {
      clearTimeoutFn(respawnTimer);
      respawnTimer = null;
    }
    const proc = child;
    if (!proc) return;
    try {
      if (proc.stdin) proc.stdin.end();
    } catch {
      // The grace kill below still runs.
    }
    if (!graceTimer) {
      graceTimer = setTimeoutFn(() => {
        graceTimer = null;
        if (child === proc) {
          try {
            proc.kill("SIGTERM");
          } catch {
            // Already gone.
          }
        }
      }, STOP_GRACE_MS);
    }
  }

  /**
   * Ask the running host one thing. Never rejects: resolves the host's reply or
   * `{ok: false, code}` with not_running, busy, timeout or host_exited.
   */
  function request(op, fields = {}, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    if (!child || state !== "running") return Promise.resolve({ ok: false, code: "not_running" });
    if (pending.size >= MAX_PENDING) return Promise.resolve({ ok: false, code: "busy" });
    const id = nextId++;
    return new Promise((resolve) => {
      const timer = setTimeoutFn(() => {
        if (!pending.has(id)) return;
        pending.delete(id);
        resolve({ ok: false, code: "timeout" });
      }, timeoutMs);
      pending.set(id, { resolve, timer });
      if (!write({ ...fields, id, op })) settle(id, { ok: false, code: "host_exited" });
    });
  }

  /** Re-send the app's Remote Crew connections after an edit. */
  function syncInstances() {
    if (!child || state !== "running") return Promise.resolve({ ok: false, code: "not_running" });
    return request("instances", { items: getInstances() });
  }

  return Object.freeze({
    start,
    stop,
    request,
    syncInstances,
    state: () => state,
    protection: () => protection,
  });
}

module.exports = {
  createConnectedComputersHost,
  instancesFromStore,
  HOST_ARGS,
  MAX_INSTANCES,
  EXIT_PROTOCOL,
  EXIT_DISABLED,
  EXIT_ELSEWHERE,
  RESPAWN_DELAY_MS,
  STOP_GRACE_MS,
  MAX_RESPAWNS,
  MAX_PENDING,
  MAX_STDOUT_BUFFER,
};
