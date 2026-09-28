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
