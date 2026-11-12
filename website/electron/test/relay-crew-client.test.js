"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { requestRelayCrewToken, REFUSAL_CODES } = require("../relay-crew-client");

const ID = "a1b2c3d4e5f6g7h8i9j0k1";
const SECRET = "local-secret-value-0123456789";
const TOKEN = "relay-access-token-SENSITIVE-abcdef";
const CREW_URL = `https://${ID}.relay.example.test`;

function fakeFetch(status, body) {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    return {
      status,
      json: async () => {
        if (typeof body === "string") throw new SyntaxError("not json");
        return body;
      },
    };
  };
  return { calls, fetchFn };
}

const call = (fetchFn, extra = {}) => requestRelayCrewToken({
  backendUrl: "http://localhost:5476",
  endpointId: ID,
  getSecret: () => SECRET,
  fetchFn,
  ...extra,
});

test("posts the endpoint ID with X-Local-Secret to the loopback token route, no redirects", async () => {
  const f = fakeFetch(200, {
    accessToken: TOKEN, expiresAt: 1000, renewInSeconds: 240, serverDate: 700,
    crewUrl: CREW_URL, name: "Team Crew",
  });
  const r = await call(f.fetchFn);
  assert.deepEqual(r, {
    ok: true,
    grant: { accessToken: TOKEN, expiresAt: 1000, renewInSeconds: 240, serverDate: 700 },
    crewUrl: CREW_URL,
    name: "Team Crew",
    signInMarker: null,
  });
  assert.equal(f.calls.length, 1);
  const { url, init } = f.calls[0];
  assert.equal(url, "http://localhost:5476/api/relay-viewer/token");
  assert.equal(init.method, "POST");
  assert.equal(init.redirect, "error");
  assert.ok(init.signal);
  assert.deepEqual(init.headers, { "Content-Type": "application/json", "X-Local-Secret": SECRET });
  assert.deepEqual(JSON.parse(init.body), { endpointId: ID });
});

test("sends the local secret only to a literal loopback http origin", async () => {
  for (const backendUrl of [
    "https://localhost:5476",
    "http://example.test:5476",
    "http://10.0.0.1:5476",
    "http://localhost.example.test:5476",
    "http://127.0.0.2:5476",
    "not a url",
    "",
  ]) {
    const f = fakeFetch(200, {});
    const r = await call(f.fetchFn, { backendUrl });
    assert.deepEqual(r, { ok: false, code: "UNAVAILABLE", retryAfter: null }, backendUrl);
    assert.equal(f.calls.length, 0, `${backendUrl}: nothing may be sent`);
  }
  for (const backendUrl of ["http://127.0.0.1:5476", "http://[::1]:5476", "http://kirocrew.localhost:5476"]) {
    const f = fakeFetch(404, {});
    await call(f.fetchFn, { backendUrl });
    assert.equal(f.calls.length, 1, backendUrl);
    assert.equal(f.calls[0].url, `${new URL(backendUrl).origin}/api/relay-viewer/token`);
  }
});

test("reads the secret for the dialed gateway on every call, and sends nothing without one", async () => {
  const reads = [];
  const f = fakeFetch(404, {});
  for (const secret of ["", null, undefined]) {
    const r = await call(f.fetchFn, { getSecret: (u) => { reads.push(u); return secret; } });
    assert.equal(r.code, "UNAVAILABLE");
  }
  const r = await call(f.fetchFn, { getSecret: () => { throw new Error(SECRET); } });
  assert.equal(r.code, "UNAVAILABLE");
  assert.deepEqual(reads, ["http://localhost:5476", "http://localhost:5476", "http://localhost:5476"]);
  assert.equal(f.calls.length, 0);
});

test("maps each gateway answer to a bridge code", async () => {
  const cases = [
    [404, { code: "RELAY_VIEWER_OFF" }, "RELAY_VIEWER_OFF"],
    [404, "not found", "RELAY_VIEWER_OFF"],
    [403, { code: "invalid_secret" }, "UNAVAILABLE"],
    [403, { code: "loopback_only" }, "UNAVAILABLE"],
    [403, { code: "member_owner_token_refused" }, "UNAVAILABLE"],
    [503, { code: "not_available" }, "UNAVAILABLE"],
    [500, "oops", "UNAVAILABLE"],
    [418, { code: "SOMETHING_NEW" }, "UNAVAILABLE"],
    [200, { accessToken: "", expiresAt: 1, crewUrl: CREW_URL }, "BAD_ANSWER"],
    [200, { accessToken: TOKEN, expiresAt: "1", crewUrl: CREW_URL }, "BAD_ANSWER"],
    [200, { accessToken: TOKEN, expiresAt: 1 }, "BAD_ANSWER"],
    [200, "not json", "BAD_ANSWER"],
  ];
  for (const code of REFUSAL_CODES) cases.push([403, { code, error: "plain" }, code]);
  for (const [status, body, want] of cases) {
    const r = await call(fakeFetch(status, body).fetchFn);
    assert.equal(r.ok, false, JSON.stringify(body));
    assert.equal(r.code, want, `${status} ${JSON.stringify(body)}`);
    assert.equal(JSON.stringify(r).includes(TOKEN), false);
  }
});

test("carries retryAfter for THROTTLED only, and nulls unusable renewal fields", async () => {
  assert.deepEqual(await call(fakeFetch(429, { code: "THROTTLED", retryAfter: 7 }).fetchFn),
    { ok: false, code: "THROTTLED", retryAfter: 7 });
  assert.equal((await call(fakeFetch(429, { code: "THROTTLED", retryAfter: -1 }).fetchFn)).retryAfter, null);
  assert.equal((await call(fakeFetch(403, { code: "CREW_OFFLINE", retryAfter: 7 }).fetchFn)).retryAfter, null);
  const r = await call(fakeFetch(200, {
    accessToken: TOKEN, expiresAt: 5, renewInSeconds: "x", serverDate: null, crewUrl: CREW_URL, name: 9,
  }).fetchFn);
  assert.deepEqual(r.grant, { accessToken: TOKEN, expiresAt: 5, renewInSeconds: null, serverDate: null });
  assert.equal(r.name, "");
});

test("a network failure, timeout or refused redirect is UNAVAILABLE and carries no secret", async () => {
  for (const err of [new TypeError("fetch failed"), new DOMException("timed out", "TimeoutError")]) {
    const r = await call(async () => { throw err; });
    assert.deepEqual(r, { ok: false, code: "UNAVAILABLE", retryAfter: null });
    assert.equal(JSON.stringify(r).includes(SECRET), false);
  }
});

test("passes a string sign-in marker through outside the grant, and anything else as null", async () => {
  const grant = { accessToken: TOKEN, expiresAt: 1000, crewUrl: CREW_URL, name: "Team Crew" };
  const r = await call(fakeFetch(200, { ...grant, signInMarker: "0123456789abcdef" }).fetchFn);
  assert.equal(r.signInMarker, "0123456789abcdef");
  assert.equal("signInMarker" in r.grant, false, "renewal never sees it");
  for (const bad of [undefined, null, "", 7, { x: 1 }]) {
    assert.equal((await call(fakeFetch(200, { ...grant, signInMarker: bad }).fetchFn)).signInMarker, null, String(bad));
  }
});

test("sends the window's sign-in marker only when there is one, and passes SIGN_IN_CHANGED through", async () => {
  const grant = { accessToken: TOKEN, expiresAt: 1000, crewUrl: CREW_URL, name: "Team Crew" };
  const sent = fakeFetch(200, grant);
  await call(sent.fetchFn, { signInMarker: "0123456789abcdef" });
  assert.deepEqual(JSON.parse(sent.calls[0].init.body), { endpointId: ID, signInMarker: "0123456789abcdef" });
  for (const none of [undefined, null, ""]) {
    const f = fakeFetch(200, grant);
    await call(f.fetchFn, { signInMarker: none });
    assert.deepEqual(JSON.parse(f.calls[0].init.body), { endpointId: ID }, String(none));
  }
  const r = await call(fakeFetch(409, { code: "SIGN_IN_CHANGED", error: "changed" }).fetchFn, { signInMarker: "0123456789abcdef" });
  assert.deepEqual(r, { ok: false, code: "SIGN_IN_CHANGED", retryAfter: null });
});
