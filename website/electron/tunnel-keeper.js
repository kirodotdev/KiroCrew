"use strict";

const { getRemoteHostConfig } = require("./host-config");
const { validateRemoteSettings } = require("./validation");

/** Wait this long before respawning a keeper process that died on its own. */
const RESPAWN_DELAY_MS = 5000;

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

  /** The crew's coordinates when this port opted into a managed tunnel, else null. */
  function managedTarget() {
    if (isWindows) return null;
    const config = getRemoteHostConfig(store, port);
    if (!config || !config.host || config.manageTunnel !== true) return null;
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
    const relay = (chunk) => {
      for (const line of String(chunk).split("\n")) {
        if (line.trim()) log(line.trim());
      }
    };
    if (proc.stdout) proc.stdout.on("data", relay);
    if (proc.stderr) proc.stderr.on("data", relay);
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
      if (!wanted) return;
      log(`tunnel: keeper ${reason}; restarting`);
      scheduleRespawn();
    };
    proc.on("error", (error) => lost(`error: ${error && error.message}`));
    proc.on("exit", (code, signal) => lost(`exited (code=${code} signal=${signal})`));
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

  /** Stop the keeper and its forward; nothing respawns until start() again. */
  function stop() {
    wanted = false;
    if (respawnTimer) {
      clearTimeoutFn(respawnTimer);
      respawnTimer = null;
    }
    const proc = child;
    child = null;
    if (proc) terminate(proc);
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
    if (proc) terminate(proc);
    start();
  }

  return { start, stop, restart };
}

module.exports = { createTunnelKeeper, RESPAWN_DELAY_MS };
