"use strict";
//
// gateway-auth-hint.js — WHICH machine should the user act on?
//
// Pure logic only (mirrors instance-guard.js / gateway-recovery.js); the shell
// owns the page load and the port probe.
//
// PROBLEM: when the app cannot mint a dashboard token it shows a "Token
// Required" page telling the user to run `kirocrew token` "on your dev
// desktop". That instruction is CORRECT for the case it was written for — an
// `ssh -L <port>:localhost:<port>` forward, where the gateway lives on another
// machine, so the CLI must run THERE (it authenticates with that host's own
// .local_secret, and the token it prints is signed by that gateway, which is
// the gateway this app is talking to through the tunnel).
//
// What the page never said is WHY it is asking, or how to tell which machine
// "your dev desktop" means. Two very different situations produce the same
// page:
//
//   foreign — the port is held by a gateway that is not this app's: an SSH
//             forward to another host, or an externally-started gateway. Our
//             own .local_secret cannot mint against it (the secret is
//             per-data-home and regenerated at each gateway start), so the
//             command has to run on the machine that gateway runs on.
//   local   — the port is held by OUR own local gateway and the mint still
//             failed. Telling that user to go to a "dev desktop" is nonsense;
//             the machine to act on is this one.
//
// This module picks between them from facts the shell already has after the
// boot-time port-owner probe (see classifyPortOwner in gateway-stop.js).

/**
 * Which machine hosts the gateway the user must mint a token on?
 *
 * Three outcomes, not two, because "we could not determine the owner" is a
 * genuinely different thing to tell the user than "the owner is not us". The
 * probe resolves to "unknown" on any platform without lsof (Windows), so
 * collapsing that into "foreign" would state an inference as fact and send a
 * purely-local user looking for another machine.
 *
 * @param {object} [facts]
 * @param {"kirocrew"|"foreign"|"none"|"unknown"} [facts.localOwner="unknown"]
 *        who owns the port's LISTEN socket locally
 * @param {string} [facts.remoteHost=""] a remote host configured for this port
 * @returns {"local"|"foreign"|"unknown"}
 *   "local"   — confirmed OUR gateway: act on THIS machine
 *   "foreign" — confirmed someone else's (a tunnel's `ssh`, an external
 *               gateway): act on the OTHER machine
 *   "unknown" — could not confirm either way: describe the likely case
 *               without asserting it
 */
function classifyAuthBlock({ localOwner = "unknown", remoteHost = "" } = {}) {
  // An explicitly configured remote host is the least ambiguous signal there
  // is: the user already told us the gateway lives elsewhere.
  if (typeof remoteHost === "string" && remoteHost !== "") return "foreign";
  if (localOwner === "kirocrew") return "local";
  if (localOwner === "foreign") return "foreign";
  // "none" (nothing listening locally, yet something answered) and "unknown"
  // (the probe could not run) both mean unconfirmed. Do not guess.
  return "unknown";
}

/**
 * The port of `rawUrl`, with a default-port URL resolved to its scheme's port.
 *
 * `URL.port` is deliberately "" when the URL uses the scheme default, so
 * `http://host/` on :80 yields no port. Every consumer here needs a concrete
 * number — the remote-host config is keyed by port, the owner probe takes a
 * port, and the page builds its submit URL from one — so an empty value would
 * silently address a different gateway than the one that returned the 403.
 *
 * @param {string} rawUrl
 * @returns {string} port digits, or "" if rawUrl is unparseable
 */
function defaultedPort(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return ""; }
  if (u.port) return u.port;
  return u.protocol === "https:" ? "443" : "80";
}

module.exports = { classifyAuthBlock, defaultedPort };
