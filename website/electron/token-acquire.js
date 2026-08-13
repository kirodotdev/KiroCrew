"use strict";
//
// Pure, injectable token-mint retry policy, extracted from main.js so it is
// unit-testable without Electron (mirrors gateway-recovery.js / blocking-prompt.js).
//
// PROBLEM: showLoadingThenConnect waits for /api/status to answer, then mints a
// dashboard token via fetchLocalToken (reads $KIROCREW_HOME/.local_secret and
// calls GET /api/token/local). A gateway we just (re)started — boot, wedge
// recovery, auto-update relaunch — REGENERATES that secret at startup, so for a
// brief warmup window right after /api/status first answers, the local mint can
// transiently 403 while the secret settles. The old code treated that single
// 403 as permanent and dropped straight to the token-required prompt, so a
// perfectly healthy local gateway that just restarted would spuriously demand a
// token the user then had to paste (or force-kill past).
//
// FIX: for OUR OWN gateway (auth-block kind !== "foreign") a 403-with-no-token is
// self-healing — retry the mint a few times with backoff before falling back to
// the prompt. A "foreign" gateway (an `ssh -L` forward or an externally started
// one) signs with a secret on the REMOTE machine that our local CLI cannot read,
// so retrying the local mint can never succeed — prompt immediately, no spin.
//
// Both functions are pure so the policy is covered independently of the Electron
// plumbing and the network.

// Extra mint attempts after the first, for a warming-up local gateway. Worst-case
// added latency only applies when there is genuinely no token AND a 403 — the
// healthy path mints on the first attempt and never enters the retry.
const TOKEN_MINT_MAX_RETRIES = 3;

/**
 * Should we retry the local token mint instead of showing the token prompt?
 *
 * @param {object} o
 * @param {string} o.kind     auth-block classification from classifyAuthBlock:
 *                            "foreign" means a gateway we did not start (remote
 *                            secret) — never retry. Anything else ("local",
 *                            unknown) is our own / same-host gateway — retry.
 * @param {number} o.attempt  zero-based attempt index already spent.
 * @returns {boolean}
 */
function shouldRetryLocalTokenMint({ kind, attempt }) {
  return kind !== "foreign" && attempt < TOKEN_MINT_MAX_RETRIES;
}

/**
 * Backoff before the next mint retry. Exponential, capped: 500ms, 1s, 2s.
 * @param {number} attempt  zero-based attempt index already spent.
 * @returns {number} milliseconds
 */
function tokenMintRetryDelayMs(attempt) {
  return Math.min(500 * 2 ** attempt, 2000);
}

module.exports = { TOKEN_MINT_MAX_RETRIES, shouldRetryLocalTokenMint, tokenMintRetryDelayMs };
