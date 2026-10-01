"use strict";

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { borrowSessionToken } = require("../mochi-session-token");

describe("borrowSessionToken", () => {
  it("returns the mc_token_<port> cookie value for the backend's port", async () => {
    const seen = [];
    const electronSession = {
      cookies: {
        get(filter) {
          seen.push(filter);
          return Promise.resolve([{ name: "mc_token_5476", value: "session-cookie-value" }]);
        },
      },
    };

    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });

    assert.equal(token, "session-cookie-value");
    assert.deepEqual(seen, [{ url: "http://localhost:5476", name: "mc_token_5476" }]);
  });

  it("declines to guess a cookie name on a scheme-default port", async () => {
    // `mc_token_<port>` is named by the gateway, after its OWN listen port when
    // the Host header carries no port -- which is what a browser sends for a
    // scheme default. Behind a tunnel that is the remote port, unknowable here,
    // so resolving the URL's default would name whichever gateway last served
    // :80. Cookies are host-scoped only, so that cookie is in this same jar and
    // borrowing it would hand one gateway's session to another.
    // Writing the default out changes nothing: the URL API strips it either way,
    // so there is no "stated default port" case for this path to treat apart.
    for (const backendUrl of [
      "http://localhost", "http://localhost:80",
      "https://localhost", "https://localhost:443",
      "http://127.0.0.1",
    ]) {
      let asked = false;
      const electronSession = {
        cookies: {
          get() {
            asked = true;
            return Promise.resolve([{ name: "mc_token_80", value: "other-gateways-session" }]);
          },
        },
      };
      assert.equal(await borrowSessionToken({ electronSession, backendUrl }), "", backendUrl);
      assert.equal(asked, false, `${backendUrl} must not reach the cookie jar at all`);
    }
  });

  it("borrows on a port that is not its scheme's default", async () => {
    // The control: without it the test above cannot tell "declines to guess"
    // from "never borrows". A non-default port survives parsing, so the cookie
    // name it produces is the one the gateway's Host header carried.
    for (const [backendUrl, expected] of [
      ["http://localhost:5476", "mc_token_5476"],
      ["http://localhost:443", "mc_token_443"],
      ["https://localhost:80", "mc_token_80"],
    ]) {
      const seen = [];
      const electronSession = {
        cookies: {
          get(filter) {
            seen.push(filter.name);
            return Promise.resolve([{ name: filter.name, value: "v" }]);
          },
        },
      };
      assert.equal(await borrowSessionToken({ electronSession, backendUrl }), "v", backendUrl);
      assert.deepEqual(seen, [expected], backendUrl);
    }
  });

  it("returns empty when no session was ever established (no matching cookie)", async () => {
    const electronSession = { cookies: { get: () => Promise.resolve([]) } };

    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });

    assert.equal(token, "");
  });

  it("fails closed when there is no session/cookie API at all", async () => {
    assert.equal(
      await borrowSessionToken({ electronSession: null, backendUrl: "http://localhost:5476" }),
      "",
    );
    assert.equal(
      await borrowSessionToken({ electronSession: {}, backendUrl: "http://localhost:5476" }),
      "",
    );
  });

  it("fails closed on an unparsable backend URL rather than throwing", async () => {
    const electronSession = { cookies: { get: () => Promise.resolve([{ value: "x" }]) } };
    const token = await borrowSessionToken({ electronSession, backendUrl: "not-a-url" });
    assert.equal(token, "");
  });

  it("fails closed when the cookie store rejects", async () => {
    const electronSession = { cookies: { get: () => Promise.reject(new Error("boom")) } };
    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });
    assert.equal(token, "");
  });

  it("never fabricates a value: a non-string cookie value resolves to empty", async () => {
    const electronSession = {
      cookies: { get: () => Promise.resolve([{ value: undefined }]) },
    };
    const token = await borrowSessionToken({
      electronSession,
      backendUrl: "http://localhost:5476",
    });
    assert.equal(token, "");
  });

  it("keys the cookie name off the backend's own port, not a hardcoded one", async () => {
    const seen = [];
    const electronSession = {
      cookies: {
        get(filter) {
          seen.push(filter.name);
          return Promise.resolve([{ value: "t" }]);
        },
      },
    };
    await borrowSessionToken({ electronSession, backendUrl: "http://localhost:7778" });
    assert.deepEqual(seen, ["mc_token_7778"]);
  });
});
