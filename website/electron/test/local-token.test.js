"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { describe, it } = require("node:test");
const { fetchLocalToken, literalLoopbackUrl } = require("../local-token");

describe("fetchLocalToken", () => {
  it("sends only the call-time authoritative secret to literal IPv4 loopback", async () => {
    // Keys are built with path.join, matching how fetchLocalToken composes the
    // secret path: a POSIX literal would never match the backslash-separated
    // path the real path.join produces on Windows, so the fake fs would return
    // undefined and the test would fail on path syntax rather than on the
    // call-time-authoritative-secret rule it exists to pin.
    const CANONICAL_HOME = path.resolve(path.sep, "canonical");
    const LEGACY_HOME = path.resolve(path.sep, "legacy");
    const files = new Map([
      [path.join(CANONICAL_HOME, ".local_secret"), "canonical-secret"],
      [path.join(LEGACY_HOME, ".local_secret"), "legacy-secret"],
    ]);
    const attempted = [];
    const requestedUrls = [];
    const fakeFs = {
      readFileSync(path) { return files.get(path); },
    };
    const fakeHttp = {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const secret = options.headers["X-Local-Secret"];
        attempted.push(secret);
        requestedUrls.push(url);
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        queueMicrotask(() => {
          callback(response);
          response.emit("data", JSON.stringify({ token: "migrated-token" }));
          response.emit("end");
        });
        return request;
      },
    };

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => LEGACY_HOME,
      path,
      fs: fakeFs,
      http: fakeHttp,
    });

    assert.equal(token, "migrated-token");
    assert.deepEqual(attempted, ["legacy-secret"]);
    assert.deepEqual(requestedUrls, ["http://127.0.0.1:5476/api/token/local"]);
  });

  it("does not fall back to another home when the authoritative secret is rejected", async () => {
    const attempted = [];
    const fakeFs = {
      readFileSync(secretPath) {
        assert.equal(secretPath, path.join("/canonical", ".local_secret"));
        return "canonical-secret";
      },
    };
    const fakeHttp = {
      get(_url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        attempted.push(options.headers["X-Local-Secret"]);
        const response = new EventEmitter();
        response.statusCode = 403;
        response.resume = () => {};
        queueMicrotask(() => callback(response));
        return request;
      },
    };

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => "/canonical",
      path,
      fs: fakeFs,
      http: fakeHttp,
    });

    assert.equal(token, "");
    assert.deepEqual(attempted, ["canonical-secret"]);
  });

  it("refuses to send a local secret to a non-literal remote address", async () => {
    let called = false;
    const token = await fetchLocalToken({
      backendUrl: "http://example.com:5476",
      resolveHome: () => "/canonical",
      path,
      fs: { readFileSync: () => "canonical-secret" },
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false);
  });
});

describe("literalLoopbackUrl", () => {
  it("preserves the port while replacing hostname aliases", () => {
    assert.equal(literalLoopbackUrl("http://localhost:6777"), "http://127.0.0.1:6777");
    assert.equal(literalLoopbackUrl("http://kirocrew.localhost:6777"), "http://127.0.0.1:6777");
  });
});
