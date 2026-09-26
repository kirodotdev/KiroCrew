"use strict";

/**
 * The literal-loopback origin of `backendUrl` and the port it addresses.
 *
 * One parse serves both, because the credential is a function of the dial
 * target: resolving the origin here and the port somewhere else would let a
 * caller authenticate for one listener while dialing another.
 *
 * `URL.port` is "" on a scheme-default port, so the default is spelled out:
 * an empty port would name a credential file no gateway ever wrote.
 *
 * @param {string} backendUrl
 * @returns {{origin: string, port: string} | null} null when the URL is not a
 *   literal `http:` loopback origin, which is the only target a local secret
 *   may be sent to.
 */

/**
 * The loopback families a dialed host can land on, and the bind addresses that
 * cover each one.
 *
 * A family is covered by its own literal and by its wildcard. `::` is counted as
 * v6 ONLY: whether a v6 wildcard also accepts v4-mapped connections depends on
 * the host's `IPV6_V6ONLY` setting, which this side cannot establish, so counting
 * it for v4 would be a guess in the permissive direction.
 */
const LOOPBACK_FAMILY_BINDS = Object.freeze({
  v4: Object.freeze(["127.0.0.1", "0.0.0.0"]),
  v6: Object.freeze(["::1", "::"]),
});

/** Hostnames that name a SET of listeners rather than one. */
const AMBIGUOUS_LOOPBACK_NAMES = Object.freeze(["localhost", "kirocrew.localhost"]);

/**
 * What a `backendUrl` actually dials: its origin, its port, and every loopback
 * family the host can resolve to.
 *
 * The families are the whole point. `localhost` resolves to BOTH loopback
 * families on an ordinary host, so dialing it can land on either listener; a
 * literal address lands on exactly one. Naming the families here, rather than
 * rewriting the host to a literal, is what lets the caller ask the only question
 * that makes an ambiguous name safe: does this gateway hold all of them?
 *
 * @param {string} backendUrl
 * @returns {{origin: string, port: string, families: string[]} | null} null when
 *   the URL is not an `http:` loopback target, the only kind a local secret may
 *   be sent to.
 */
function dialTarget(backendUrl) {
  try {
    const url = new URL(backendUrl);
    if (url.protocol !== "http:") return null;
    const host = url.hostname;
    let families;
    if (AMBIGUOUS_LOOPBACK_NAMES.includes(host)) {
      families = ["v4", "v6"];
    } else if (host === "127.0.0.1") {
      families = ["v4"];
    } else if (host === "[::1]" || host === "::1") {
      families = ["v6"];
    } else {
      return null;
    }
    return { origin: url.origin, port: url.port || "80", families };
  } catch {
    return null;
  }
}

/**
 * The secrets it is safe to send to `target`, or `null` meaning refuse.
 *
 * THE PREDICATE, and the whole security argument of this change now rests here.
 * A gateway publishes `run/gateway-<port>-<address>.secret` for each address it
 * bound, so the set of those files IS the evidence of which listeners on this
 * port are this gateway's. The rule:
 *
 *   every family the dialed host can reach must be covered by an entry.
 *
 * For a literal host that is one family, which is the old behaviour. For an
 * ambiguous name it is both, and that is what makes the name safe to dial: if
 * the gateway holds v4 and v6 on this port, then whichever one the resolver
 * picks, the party reached is the gateway that published the credential. A
 * co-resident cannot be on either, because the gateway is.
 *
 * A single missing family is a refusal, not a narrowing. If the gateway bound
 * only v4 and something else holds `[::1]:<port>`, dialing `localhost` may reach
 * that squatter, so no secret goes out at all and the caller falls through to the
 * remote-token path and then the token prompt. That is the same fail-closed cost
 * a v6-bound gateway already pays, and it is paid by refusing rather than by
 * guessing which listener answered.
 *
 * Reading only local disk keeps the peer out of it: a process that merely holds
 * the port, whatever it presents itself as, is never consulted and never
 * believed.
 *
 * @returns {string[] | null} secrets to try in order, or null to refuse
 */
function listenerSecretsFor({ target, home, path, fs }) {
  const perFamily = [];
  for (const family of target.families) {
    const candidates = [];
    for (const bindAddress of LOOPBACK_FAMILY_BINDS[family]) {
      let secret = "";
      try {
        secret = fs
          .readFileSync(listenerSecretPath(home, target.port, bindAddress, path), "utf8")
          .trim();
      } catch {
        // A missing/unreadable entry simply does not cover this family.
      }
      if (!secret) continue;
      // EVERY present entry is a candidate, not just the first. `clear_marker`
      // runs only on a graceful shutdown, so a crashed generation leaves its
      // entry behind; stopping at that stale secret would spend the walk and
      // refuse a gateway that is actually reachable through the wildcard entry
      // beside it. A refused candidate must not end the walk.
      if (!candidates.includes(secret)) candidates.push(secret);
    }
    // One uncovered family the dialed host can reach is enough to refuse: the
    // resolver may hand us exactly that listener.
    if (candidates.length === 0) return null;
    perFamily.push(candidates);
  }
  // THE INTERSECTION, and it is what makes coverage a fact about the LIVE
  // generation rather than about which files happen to exist.
  //
  // Presence alone fails OPEN. Nothing deletes a sidecar except a graceful
  // shutdown, so a SIGKILLed generation that held both families leaves its v6
  // entry behind; a co-resident then takes that address, this gateway restarts
  // and binds v4 only, and a presence test would call v6 "covered" by the DEAD
  // generation's file and send the LIVE secret to the squatter.
  //
  // What distinguishes them is already on disk: one generation writes the SAME
  // secret under every address it bound, and each start mints a fresh one. So a
  // secret appearing under an address in EVERY family the dialed host can reach
  // is proof that one generation holds them all, and a stale file from another
  // generation carries a different value and drops out of the intersection.
  //
  // A literal host has ONE family, so the intersection is that family's own
  // candidate list and this changes nothing for it -- including the stale-entry
  // walk above.
  const [first, ...rest] = perFamily;
  const shared = first.filter((secret) => rest.every((family) => family.includes(secret)));
  if (shared.length === 0) return null;
  return shared;
}

/**
 * Filename-safe spelling of a bind address, identical to the writer's.
 *
 * `:` is legal in an IPv6 literal and illegal in a Windows filename, so the
 * gateway rewrites it to `_` when it publishes (`run_marker.encode_bind_address`).
 * A reader that builds the raw spelling looks for a file that never exists, which
 * fails as a REFUSAL rather than as an error: the family reads as uncovered and
 * the mint declines. So this mapping is not cosmetic, and it is pinned against the
 * writer's own output by a test that names the literal file both sides produce.
 *
 * @param {string} bindAddress
 * @returns {string}
 */
function encodeBindAddress(bindAddress) {
  return String(bindAddress).replace(/:/g, "_");
}

/**
 * Path of the gateway credential for the listener at `bindAddress` on `port`.
 *
 * A gateway publishes its own in-memory credential, once it has bound, as
 * `run/gateway-<port>-<address>.secret`: mode 0600 inside an owner-only `run/`
 * directory. Reading that file answers "may a secret go to whatever answers this
 * address and port?" out of local disk state, so nothing is asked of the peer --
 * a process that merely holds the port, whatever it presents itself as, is never
 * consulted and never believed.
 *
 * The name carries the address because a port number identifies a SET of
 * listeners. Keyed by port alone, a gateway bound to the v6 loopback and a
 * tunnel's local end on v4 share one entry, and the app would read the former's
 * credential and send it to the latter.
 *
 * @param {string} home data home whose `config.json` governs this launch
 * @param {string} port port being dialed
 * @param {string} bindAddress address whose listener published the credential
 * @param {object} path node:path (injected)
 * @returns {string}
 */
function listenerSecretPath(home, port, bindAddress, path) {
  return path.join(home, "run", `gateway-${port}-${encodeBindAddress(bindAddress)}.secret`);
}

/**
 * Ask the gateway at `dialedOrigin` to exchange the local secret for a token.
 *
 * `dialedOrigin` is the origin exactly as the caller's configured URL spells it --
 * usually `http://localhost:<port>`. Sending a secret there is safe only because
 * the caller checked first that this gateway holds every loopback family that host
 * can reach (`listenerSecretsFor`); this function performs no check of its own and
 * must not be called without that one.
 */
async function requestLocalToken(http, dialedOrigin, secret) {
  if (!dialedOrigin) return "";
  return new Promise((resolve) => {
    const req = http.get(
      `${dialedOrigin}/api/token/local`,
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
 * A dashboard token minted against the gateway that owns the dialed listener.
 *
 * The credential sent is the one that gateway published for its own listener,
 * identified by the address AND the port that were dialed. Neither the home-wide
 * `.local_secret` nor a port-keyed entry is a second place to look. The shared
 * file holds one slot per data home on a last-writer-wins basis, and a port-keyed
 * entry names every listener sharing that port number, so either can hold a
 * credential belonging to a gateway this call never reached. Sending that to
 * whoever answers here would surrender a credential which authenticates
 * elsewhere. An entry keyed by the dialed listener cannot escalate, because the
 * only listener it authenticates against is the one it was just sent to.
 *
 * An absent entry therefore denies rather than widens. A port no local gateway
 * bound on this address, an `ssh -L` forward's local end among them, whether or
 * not a v6-bound gateway holds the same port number, has no credential to read,
 * so the caller falls through to the remote-token path and then to the token
 * prompt.
 *
 * A REFUSED entry is not the end of the walk either. `clear_marker` runs only on
 * a graceful shutdown, so a crashed gateway leaves its entry behind, and a stale
 * exact-address entry sitting beside the live wildcard one would otherwise spend
 * the single attempt and report no token while a working credential was never
 * tried. Continuing costs nothing that matters: a dead generation's secret
 * authenticates against no listener at all, and every candidate names a listener
 * on the address being dialed, so the walk never reaches a party this call did
 * not reach.
 *
 * The minted token is returned WITH the origin it was minted against, and every
 * request that carries it must be addressed to that origin. The token is a
 * bearer: whoever receives it holds dashboard authority until it expires. The
 * dial resolves `localhost` to the literal address here, so handing back the
 * token alone would leave each caller to re-derive a destination from the
 * configured URL -- and `localhost` names both loopback families on an ordinary
 * host, so the request can land on `[::1]:<port>`, which a co-resident is free
 * to hold while a v4-bound gateway supplies the credential. Carrying the origin
 * makes the destination a property of the token rather than a second lookup that
 * can disagree with the first.
 *
 * `origin` is empty exactly when `token` is, so a caller with no minted token
 * addresses its request the way it did before: the remote-token and
 * token-prompt paths are unchanged, and a v6-bound gateway -- which publishes no
 * entry any candidate here covers -- keeps being reached under its configured
 * name.
 *
 * @returns {Promise<{token: string, origin: string}>}
 */
async function mintLocalToken({ backendUrl, resolveHome, path, fs, http }) {
  const target = dialTarget(backendUrl);
  if (!target) return "";
  const secrets = listenerSecretsFor({ target, home: resolveHome(), path, fs });
  // `null` is a REFUSAL, not a miss: some family this host resolves to has no
  // sidecar, so the gateway does not demonstrably hold every listener the name
  // can reach, and a co-resident could be holding one of them. Sending nothing
  // costs one explicit sign-in; sending the secret could cost the secret.
  if (!secrets) return "";
  for (const secret of secrets) {
    // `target.origin` is the URL's OWN origin, never rewritten -- that is why
    // the document's storage, and every comparison against the configured
    // string, stay where they are.
    const token = await requestLocalToken(http, target.origin, secret);
    if (token) return token;
  }
  return "";
}

module.exports = {
  mintLocalToken,
  encodeBindAddress,
  listenerSecretPath,
  dialTarget,
  listenerSecretsFor,
  AMBIGUOUS_LOOPBACK_NAMES,
  LOOPBACK_FAMILY_BINDS,
};
