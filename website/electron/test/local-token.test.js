"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { describe, it } = require("node:test");
const { fetchLocalToken, literalLoopbackUrl, listenerSecretPath } = require("../local-token");

// Keys are built with path.join, matching how fetchLocalToken composes the
// credential path: a POSIX literal would never match the backslash-separated
// path the real path.join produces on Windows, so the fake fs would return
// undefined and these tests would fail on path syntax rather than on the rules
// they exist to pin.
const CANONICAL_HOME = path.resolve(path.sep, "canonical");
const LEGACY_HOME = path.resolve(path.sep, "legacy");

/** An fs whose readFileSync answers from `files` and throws for anything else. */
function fakeFsOver(files, reads = []) {
  return {
    readFileSync(p) {
      reads.push(p);
      if (!files.has(p)) {
        const error = new Error(`ENOENT: ${p}`);
        error.code = "ENOENT";
        throw error;
      }
      return files.get(p);
    },
  };
}

/** An http whose get() records the secret sent and answers with `statusCode`. */
function fakeHttpRecording({ statusCode = 200, token = "minted-token", sent = [], urls = [] }) {
  return {
    get(url, options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      sent.push(options.headers["X-Local-Secret"]);
      urls.push(url);
      const response = new EventEmitter();
      response.statusCode = statusCode;
      response.resume = () => {};
      queueMicrotask(() => {
        callback(response);
        if (statusCode === 200) {
          response.emit("data", JSON.stringify({ token }));
          response.emit("end");
        }
      });
      return request;
    },
  };
}

describe("fetchLocalToken", () => {
  it("sends the credential the dialed listener published, to literal IPv4 loopback", async () => {
    const files = new Map([
      [listenerSecretPath(LEGACY_HOME, "5476", path), "listener-secret"],
    ]);
    const sent = [];
    const urls = [];

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => LEGACY_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent, urls }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["listener-secret"]);
    assert.deepEqual(urls, ["http://127.0.0.1:5476/api/token/local"]);
  });

  it("refuses the home-wide secret when the dialed listener published none", async () => {
    // The pin. A home-wide secret can belong to a gateway on another port, so a
    // port with no published credential of its own gets nothing on the wire —
    // the request is never made, rather than made and refused.
    const files = new Map([
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    const reads = [];
    let called = false;

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files, reads),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false, "no secret reaches the port");
    assert.deepEqual(reads, [listenerSecretPath(CANONICAL_HOME, "5476", path)]);
  });

  it("sends the listener credential, not the home-wide one, when both exist", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", path), "listener-secret"],
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    const sent = [];

    const token = await fetchLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["listener-secret"]);
  });

  it("keys the credential to the dialed port, not to a sibling listener", async () => {
    // Two gateways in one data home. Dialing :9099 must never read :5476's
    // credential, which authenticates against a listener this call is not
    // talking to.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", path), "secret-5476"],
      [listenerSecretPath(CANONICAL_HOME, "9099", path), "secret-9099"],
    ]);
    const sent = [];
    const urls = [];

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:9099",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent, urls }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["secret-9099"]);
    assert.deepEqual(urls, ["http://127.0.0.1:9099/api/token/local"]);
  });

  it("does not fall back to another credential when the listener rejects its own", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", path), "listener-secret"],
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
      [listenerSecretPath(LEGACY_HOME, "5476", path), "legacy-secret"],
    ]);
    const sent = [];

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ statusCode: 403, sent }),
    });

    assert.equal(token, "");
    assert.deepEqual(sent, ["listener-secret"]);
  });

  it("refuses to send a local secret to a non-literal remote address", async () => {
    let called = false;
    const reads = [];
    const token = await fetchLocalToken({
      backendUrl: "http://example.com:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(new Map(), reads),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false);
    assert.deepEqual(reads, [], "a non-loopback target is refused before any credential is read");
  });
});

describe("listenerSecretPath", () => {
  it("names the file the gateway publishes for its own listener", () => {
    assert.equal(
      listenerSecretPath(CANONICAL_HOME, "5476", path),
      path.join(CANONICAL_HOME, "run", "gateway-5476.secret"),
    );
  });
});

describe("literalLoopbackUrl", () => {
  it("preserves the port while replacing hostname aliases", () => {
    assert.equal(literalLoopbackUrl("http://localhost:6777"), "http://127.0.0.1:6777");
    assert.equal(literalLoopbackUrl("http://kirocrew.localhost:6777"), "http://127.0.0.1:6777");
  });

  it("refuses a non-http scheme and a non-loopback host", () => {
    assert.equal(literalLoopbackUrl("https://localhost:6777"), "");
    assert.equal(literalLoopbackUrl("http://example.com:6777"), "");
    assert.equal(literalLoopbackUrl("not a url"), "");
  });
});
