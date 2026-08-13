/**
 * withGatewayAuth() and gatewayToken() cannot be exercised directly — index.js
 * requires "electron" at load time (`app`, `ipcMain`), which does not exist
 * outside a running Electron process. The behavioural claims are pinned as
 * source guards instead, the same pattern instanceSwitchRules.test.js uses for
 * the rest of this module.
 *
 * What these guard: a credential borrowed from the main window's ALREADY
 * -established session (see ../../mochi-session-token.js) must be delivered
 * as a `Cookie` header, never as `?token=`. The gateway's auth middleware
 * checks a query-delivered value against its 5-minute link-click `exp` claim,
 * which is almost always already in the past for a borrowed session
 * credential (its `exp` froze at the moment that session was ORIGINALLY
 * exchanged). Delivering it as `?token=` would validate for a few minutes
 * after app launch and then silently 401 forever — the same "pet never
 * appears" bug this file exists to fix, just delayed rather than prevented.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const MAIN = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

test("withGatewayAuth delivers a cookie-sourced credential as a Cookie header, not a query token", () => {
  const start = MAIN.indexOf("function withGatewayAuth(");
  assert.ok(start !== -1, "withGatewayAuth must exist");
  const body = MAIN.slice(start, MAIN.indexOf("\n}", start));
  assert.ok(
    /if \(auth\.viaCookie\)/.test(body),
    "must branch on how the credential was sourced",
  );
  assert.ok(
    /headers: \{ Cookie: `mc_token_\$\{port\}=\$\{auth\.value\}` \}/.test(body),
    "a viaCookie credential must be sent as an mc_token_<port> Cookie header",
  );
  // The cookie branch must return before falling into the query-append path.
  const cookieBranch = body.indexOf("if (auth.viaCookie)");
  const queryAppend = body.indexOf("token=${encodeURIComponent");
  assert.ok(
    cookieBranch !== -1 && queryAppend !== -1 && cookieBranch < queryAppend,
    "the cookie path must be chosen before any query-token fallback runs",
  );
});

test("every local-gateway request built from gatewayToken() routes through withGatewayAuth", () => {
  // mochiEnabledState, mochiSettings, fetchInstances and connectInstance all
  // consume gatewayToken()'s answer; every one of them must hand it to
  // withGatewayAuth rather than re-inlining `?token=${encodeURIComponent(...)}`,
  // or the cookie-sourced path silently regresses to the query-only behavior
  // this fix removes.
  const calls = MAIN.match(/withGatewayAuth\(/g) || [];
  assert.ok(
    calls.length >= 4,
    `expected withGatewayAuth to be used by mochiEnabledState, mochiSettings, ` +
      `fetchInstances and connectInstance (found ${calls.length} call sites)`,
  );
  // Anchored on BACKEND_URL specifically: remoteMochiEnabled's own
  // `/api/apps?token=` call is a DIFFERENT, already-minted remote-instance
  // token (never sourced from gatewayToken()) and is deliberately unchanged.
  assert.ok(
    !/\$\{BACKEND_URL\}\/api\/apps\?token=\$\{encodeURIComponent/.test(MAIN),
    "mochiEnabledState must not go back to inlining a bare query token",
  );
  assert.ok(
    !/\$\{BACKEND_URL\}\/api\/apps\/mochi\/settings\?token=\$\{encodeURIComponent/.test(MAIN),
    "mochiSettings must not go back to inlining a bare query token",
  );
});

test("gatewayToken() fails closed to an empty credential, never a fabricated one", () => {
  const start = MAIN.indexOf("async function gatewayToken(");
  assert.ok(start !== -1, "gatewayToken must exist");
  const body = MAIN.slice(start, MAIN.indexOf("\n}", start));
  assert.ok(
    /await fetchGatewayAuth\(\)\) \|\| \{ value: "" \}/.test(body),
    "a fetchGatewayAuth() failure (undefined/null) must fall back to an empty credential",
  );
});

test("blank-overlay recovery separates gateway auth value from its delivery mode", () => {
  const start = MAIN.indexOf("if (hasBlankedOverlay())");
  const end = MAIN.indexOf("restorePanelOnEnable", start);
  const body = MAIN.slice(start, end);
  assert.match(body, /const auth = await gatewayToken\(\)/,
    "self recovery resolves the gateway auth record once");
  assert.match(body, /rearmToken = auth\.value/,
    "recovery passes the credential string, not the auth record");
  assert.match(body, /rearmViaCookie = auth\.viaCookie/,
    "recovery preserves cookie delivery mode separately");
  assert.match(body, /rearmBlankedOverlays\(mochiPetBaseUrl, rearmToken, rearmViaCookie\)/,
    "rearm receives scalar credential arguments");
});

test("a rejected credential (401/403) is dropped so the next tick re-resolves", () => {
  const drops = MAIN.match(/cachedGatewayAuth = \{ value: "" \};/g) || [];
  // mochiEnabledState and mochiSettings each clear the cache on 401/403.
  assert.ok(
    drops.length >= 2,
    `expected at least 2 call sites clearing cachedGatewayAuth on a rejected credential ` +
      `(found ${drops.length})`,
  );
});
