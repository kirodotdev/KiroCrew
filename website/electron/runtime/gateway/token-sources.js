"use strict";

const { getRemoteHostConfig } = require("../../host-config");
const { validateRemoteSettings } = require("../../validation");
const { findSshBin } = require("../../find-bin");
const {
  buildRemoteTokenCommand,
  buildRemoteTokenSshArgs,
  describeSshFailure,
  parseTokenFromStdout,
} = require("../../remote-token");
const { defaultedPort } = require("../../gateway-auth-hint");
const { mintLocalToken: mintTokenFromHome } = require("../../local-token");
const { resolveHome } = require("../../home-dir");

/**
 * Where a dashboard token comes from: minted locally against this machine's
 * `.local_secret`, or fetched over SSH from the crew configured for a port.
 *
 * The local mint is the security-sensitive half. It sends the machine's
 * secret, so it runs only for a gateway this process spawned, that is still
 * alive, and that the kernel names as the port's listener; everything else
 * falls through to the SSH path and then to the token prompt.
 *
 * @param {object} deps
 * @param {() => object|null} deps.getGatewayProcess  the supervisor's current
 *        child, re-read at every site because it changes across awaits.
 */
function createTokenSources({
  store,
  port: PORT,
  backendUrl: BACKEND_URL,
  execFile,
  fs,
  path,
  http,
  isWindows,
  log: glog,
  sendStatus,
  snapshotGatewayPortPids,
  getGatewayProcess,
}) {
  function fetchRemoteToken(tokenPort) {
    const config = getRemoteHostConfig(store, tokenPort || PORT);
    if (!config || !config.host) return Promise.resolve({ token: "", error: null });
    const { host: remoteHost, binPath, remotePort, remotePath } = config;
    const validationError = validateRemoteSettings(
      remoteHost,
      binPath,
      remotePort,
      remotePath,
    );
    if (validationError) {
      console.error(`Refusing SSH token fetch: ${validationError}`);
      return Promise.resolve({ token: "", error: validationError });
    }

    const effectivePort = remotePort || tokenPort || PORT;
    const remoteCommand = buildRemoteTokenCommand(binPath, {
      port: effectivePort,
      remotePath: remotePath || undefined,
    });
    const timeoutMs = Math.max(store.get("sshTimeoutMs") || 20000, 5000);
    const sshArgs = buildRemoteTokenSshArgs(remoteHost, remoteCommand, { timeoutMs });
    const sshBin = findSshBin(fs, path, isWindows);
    if (!sshBin) {
      const error = "ssh client not found: the Windows system directory did not resolve.";
      console.error(`Refusing SSH token fetch: ${error}`);
      return Promise.resolve({ token: "", error });
    }

    return new Promise((resolve) => {
      sendStatus("Fetching token from remote dev desktop…");
      glog(`SSH token fetch: ${sshBin} ${remoteHost} for port ${effectivePort}`);
      execFile(
        sshBin,
        sshArgs,
        { timeout: timeoutMs },
        (error, stdout, stderr) => {
          if (error) {
            console.error("SSH token fetch failed:", error.message);
            if (stderr) console.error("SSH stderr:", stderr.trim().slice(0, 500));
            resolve({
              token: "",
              error: describeSshFailure(error, stderr, { sshBin, remoteHost, timeoutMs }),
            });
            return;
          }
          resolve({ token: parseTokenFromStdout(stdout), error: null });
        },
      );
    });
  }

  /**
   * Mint a dashboard token for `targetBackendUrl`.
   *
   * The token is delivered to the URL it was minted for, unchanged: the mint only
   * produces one after confirming this gateway holds every loopback family that
   * URL's host resolves to (`listenerSecretsFor`), so the host as written can
   * reach no other listener. Every refusal below answers with the same empty
   * string a miss produces, so a refusal and an absent credential are one case
   * for the caller.
   *
   * @returns {Promise<string>}
   */
  async function mintLocalToken(targetBackendUrl = BACKEND_URL) {
    // The secret is the thing that must not leave this machine, so the check
    // belongs here rather than on the adoption. `local-token.js` sends
    // `X-Local-Secret` to whatever answers a literal loopback origin, and an
    // `ssh -L` local end is one, so a gateway forwarded from another machine
    // receives it -- and the header is on the wire before its 403 is read, so
    // there is no recovering afterwards.
    //
    // The ONLY thing that authorises the send is first-hand knowledge: this
    // process started that gateway and that child is still alive, on our own
    // port. Nothing else is asked, and in particular the port's LISTEN owner is
    // NOT asked, because the answer cannot be trusted for this decision --
    // `isKirocrewCommand` matches the basename of the command line `ps` reports,
    // so any process running as this user can present itself as ours with
    // `exec -a kirocrew` and collect the secret. `gatewayOwnership`'s own
    // `reused-local` and `reused-service` states are no substitute:
    // `classifyAdoptedGateway` derives them from that same predicate.
    //
    // So silent authentication for a gateway this app merely adopted is not a
    // capability being traded away for safety -- it never worked. Reimplementing
    // it on an identity judgement that holds is its own design change, tracked
    // separately; until then an adopted gateway asks for a token, which is the
    // honest answer when this app cannot tell whose gateway it is.
    //
    // Refusing is not a dead end: the caller falls through to the remote-token
    // path and then to the token prompt, which is what the supervisor's own
    // comment already says should happen for a gateway this machine cannot mint
    // against.
    const mintPort = Number(defaultedPort(targetBackendUrl)) || PORT;
    // Reachable even for a gateway we spawned: the failure dialog's Add Remote
    // Crew writes a crew for this very port, so the store can name one after the
    // spawn. That port's holder is then a tunnel by construction.
    if (getRemoteHostConfig(store, mintPort)?.host) {
      glog(`not minting a local token for :${mintPort}: a remote crew is configured there`);
      return "";
    }
    // The gateway must be this process's child AND still running. The child is
    // the supervisor's `gatewayProcess`; its `gatewayOwnership` is NOT, and
    // reaching for it here was the bug. Ownership answers "may the recovery hook
    // respawn?", and `stopGatewayGracefully`'s own docstring says it deliberately
    // stays `"spawned"` after a stop so an aborted update can respawn -- the
    // child's `exit` handler likewise clears the child and leaves ownership
    // alone. So ownership outlives the child, and between that death and the
    // next spawn anything may bind the freed port: a token refresh would then
    // hand the secret to whatever took it over.
    //
    // Liveness is the same test `stopGatewayGracefully` uses on the same field.
    const liveChild = getGatewayProcess() && getGatewayProcess().exitCode === null;
    if (!liveChild || targetBackendUrl !== BACKEND_URL) {
      glog(`not minting a local token for :${mintPort}: no live gateway of this process on that port`);
      return "";
    }
    // Liveness says our child is RUNNING; it does not say our child is what
    // ANSWERED. The pre-spawn probe established the port was free then, and our
    // child binds only after its interpreter has imported -- seconds on a cold
    // bundled tree -- so anything may bind inside that window. An `ssh -L`
    // reconnect answers `/api/status` exactly as the gateway would, `waitForBackend`
    // accepts it, and the secret would go to the far end of the tunnel.
    //
    // The kernel's own pid-to-port mapping settles it, and that is the whole
    // reason to use it here: a pid cannot be presented the way a command line can,
    // so this is not the argv0 judgement #13826 is about -- `snapshotGatewayPortPids`
    // returns raw pids and `isKirocrewCommand` is not involved. The gateway binds
    // in the process we spawned (`web.TCPSite` in the dashboard server, no fork),
    // so our child's own pid is the one the kernel reports for this port.
    //
    // A null snapshot is "could not establish", whether the probe cannot run or
    // nothing is listening, and both refuse: the port is confirmed ours or the
    // secret stays put.
    const holders = await snapshotGatewayPortPids(mintPort);
    if (!holders || !holders.includes(getGatewayProcess().pid)) {
      glog(`not minting a local token for :${mintPort}: the kernel does not name our gateway (pid ${getGatewayProcess().pid}) as its listener`);
      return "";
    }
    // Re-resolve the home at call time so a KIROCREW_HOME change after Electron
    // starts is honored. The mint dials the configured loopback origin exactly
    // as written; `mintTokenFromHome` never rewrites it to a literal address.
    return mintTokenFromHome({
      backendUrl: targetBackendUrl,
      resolveHome,
      path,
      fs,
      http,
    });
  }

  return { fetchRemoteToken, mintLocalToken };
}

module.exports = { createTokenSources };
