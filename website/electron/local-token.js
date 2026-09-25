"use strict";

/**
 * The literal-loopback origin of `backendUrl` and the port it addresses.
 *
 * One parse serves both, because the credential is a function of the dial
 * target: resolving the origin here and the port somewhere else would let a
 * caller authenticate for one listener while dialing another.
 *
 * `URL.port` is "" on a scheme-default port, so the default is spelled out —
 * an empty port would name a credential file no gateway ever wrote.
 *
 * @param {string} backendUrl
 * @returns {{origin: string, port: string} | null} null when the URL is not a
 *   literal `http:` loopback origin, which is the only target a local secret
 *   may be sent to.
 */
function loopbackTarget(backendUrl) {
  try {
    const url = new URL(backendUrl);
    if (url.protocol !== "http:") return null;
    if (url.hostname === "localhost" || url.hostname === "kirocrew.localhost") {
      url.hostname = "127.0.0.1";
    }
    if (url.hostname !== "127.0.0.1") return null;
    return { origin: url.origin, port: url.port || "80" };
  } catch {
    return null;
  }
}

function literalLoopbackUrl(backendUrl) {
  const target = loopbackTarget(backendUrl);
  return target ? target.origin : "";
}

/**
 * Path of the gateway credential paired with the listener on `port`.
 *
 * A gateway publishes its own in-memory credential as
 * `run/gateway-<port>.secret` once it has bound that port, mode 0600 inside an
 * owner-only `run/` directory. Reading that file answers "may a secret go to
 * whatever answers this port?" out of local disk state, so nothing is asked of
 * the peer: a process that merely holds the port, whatever it presents itself
 * as, is never consulted and never believed.
 *
 * @param {string} home data home whose `config.json` governs this launch
 * @param {string} port port being dialed
 * @param {object} path node:path (injected)
 * @returns {string}
 */
function listenerSecretPath(home, port, path) {
  return path.join(home, "run", `gateway-${port}.secret`);
}

async function requestLocalToken(http, literalUrl, secret) {
  if (!literalUrl) return "";
  return new Promise((resolve) => {
    const req = http.get(
      `${literalUrl}/api/token/local`,
      { headers: { "X-Local-Secret": secret }, timeout: 5000 },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          resolve("");
          return;
        }
        let data = "";
        res.on("error", () => resolve(""));
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          try { resolve(JSON.parse(data).token || ""); } catch { resolve(""); }
        });
      },
    );
    req.on("error", () => resolve(""));
    req.on("timeout", () => { req.destroy(); resolve(""); });
  });
}

/**
 * A dashboard token minted against the gateway that owns the dialed port.
 *
 * The credential sent is the one that gateway published for its own listener,
 * and the home-wide `.local_secret` is deliberately not a second place to look.
 * That file holds one slot per data home on a last-writer-wins basis, so its
 * value can belong to a gateway on a DIFFERENT port; sending it to whoever
 * answers this port would surrender a credential that authenticates elsewhere.
 * A listener-scoped credential cannot escalate, because the only listener it
 * authenticates against is the one it was just sent to.
 *
 * An absent file therefore denies rather than widens: a port no local gateway
 * bound — an `ssh -L` forward's local end among them — has no credential to
 * read, so the caller falls through to the remote-token path and then to the
 * token prompt.
 */
async function fetchLocalToken({ backendUrl, resolveHome, path, fs, http }) {
  const target = loopbackTarget(backendUrl);
  if (!target) return "";
  let secret = "";
  try {
    const authoritativeHome = resolveHome();
    secret = fs
      .readFileSync(listenerSecretPath(authoritativeHome, target.port, path), "utf8")
      .trim();
  } catch {
    // A missing/unreadable listener credential is an ordinary token miss.
  }
  if (!secret) return "";
  return requestLocalToken(http, target.origin, secret);
}

module.exports = { fetchLocalToken, literalLoopbackUrl, listenerSecretPath };
