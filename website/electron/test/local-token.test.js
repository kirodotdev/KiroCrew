"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { describe, it } = require("node:test");
const {
  mintLocalToken,
  listenerSecretPath,
} = require("../local-token");

// Keys are built with path.join, matching how mintLocalToken composes the
// credential path: a POSIX literal would never match the backslash-separated
// path the real path.join produces on Windows, so the fake fs would return
// undefined and these tests would fail on path syntax rather than on the rules
// they exist to pin.
const CANONICAL_HOME = path.resolve(path.sep, "canonical");
const LEGACY_HOME = path.resolve(path.sep, "legacy");

/** The port-keyed name the gateway also publishes for port-only readers. */
function portKeyedPath(home, port) {
  return path.join(home, "run", `gateway-${port}.secret`);
}

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

describe("mintLocalToken", () => {
  it("sends the credential the dialed listener published, to literal IPv4 loopback", async () => {
    const files = new Map([
      [listenerSecretPath(LEGACY_HOME, "5476", "127.0.0.1", path), "v4-loopback-secret"],
    ]);
    const sent = [];
    const urls = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => LEGACY_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent, urls }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["v4-loopback-secret"]);
    assert.deepEqual(urls, ["http://127.0.0.1:5476/api/token/local"]);
  });

  it("accepts a wildcard v4 bind, which answers the dialed loopback address", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "wildcard-secret"],
    ]);
    const sent = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["wildcard-secret"]);
  });

  it("refuses when only another bind address's listener published a credential", async () => {
    // THE PIN. A gateway bound to the v6 loopback leaves IPv4 127.0.0.1:5476
    // free, so an `ssh -L 127.0.0.1:5476` tunnel can hold the address this call
    // dials. Its entry, and the port-keyed entry that names every listener on
    // 5476, both hold the v6 gateway's credential. Neither may be read: the
    // credential would go down the tunnel to a remote peer, unrecoverably.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "__1", path), "v6-gateway-secret"],
      [portKeyedPath(CANONICAL_HOME, "5476"), "v6-gateway-secret"],
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    const reads = [];
    let called = false;

    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files, reads),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false, "no secret reaches the dialed address");
    assert.deepEqual(reads, [
      listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path),
      listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path),
    ], "only entries naming a listener on the dialed address are even read");
  });

  it("refuses the home-wide secret when no listener on this address published one", async () => {
    const files = new Map([
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    let called = false;

    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false, "no secret reaches the port");
  });

  it("prefers the exact dialed address over the wildcard entry", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "exact-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "wildcard-secret"],
    ]);
    const sent = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["exact-secret"]);
  });

  it("keys the credential to the dialed port, not to a sibling listener", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "secret-5476"],
      [listenerSecretPath(CANONICAL_HOME, "9099", "127.0.0.1", path), "secret-9099"],
    ]);
    const sent = [];
    const urls = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:9099",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent, urls }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["secret-9099"]);
    assert.deepEqual(urls, ["http://127.0.0.1:9099/api/token/local"]);
  });

  it("tries the other covering address when a stale entry is refused", async () => {
    // `clear_marker` runs only on a graceful shutdown, so a crashed gateway
    // leaves its entry behind. A stale exact-address entry beside the live
    // wildcard one must not spend the walk: a dead generation's secret
    // authenticates against no listener, and both candidates name a listener on
    // the address being dialed.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "stale-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "live-secret"],
    ]);
    const sent = [];
    const fakeHttp = {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const secret = options.headers["X-Local-Secret"];
        sent.push(secret);
        const response = new EventEmitter();
        response.statusCode = secret === "live-secret" ? 200 : 403;
        response.resume = () => {};
        queueMicrotask(() => {
          callback(response);
          if (response.statusCode === 200) {
            response.emit("data", JSON.stringify({ token: "minted-token" }));
            response.emit("end");
          }
        });
        return request;
      },
    };

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttp,
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["stale-secret", "live-secret"]);
  });

  it("never reaches the port-keyed or home-wide secret, even when every entry is refused", async () => {
    // The boundary is WHICH entries may be tried, not how many attempts are
    // made. Entries naming a listener on the dialed address are fair game; an
    // entry that merely shares the port number, and the home-wide file, are not.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "exact-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "wildcard-secret"],
      [portKeyedPath(CANONICAL_HOME, "5476"), "port-keyed-secret"],
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    const sent = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ statusCode: 403, sent }),
    });

    assert.equal(token, "");
    assert.deepEqual(sent, ["exact-secret", "wildcard-secret"]);
    assert.equal(sent.includes("port-keyed-secret"), false);
    assert.equal(sent.includes("home-wide-secret"), false);
  });

  it("refuses to send a local secret to a non-literal remote address", async () => {
    let called = false;
    const reads = [];
    const token = await mintLocalToken({
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

  it("dials the host exactly as written, never a rewritten spelling", async () => {
    // THE PIN for B. The document stays on the configured origin, so the mint
    // must not move the request either: `localhost` is dialed as `localhost`.
    // Rewriting it to a literal is what would split the document's storage and
    // break every comparison that holds the configured string.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "shared-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "::1", path), "shared-secret"],
    ]);
    const urls = [];

    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ urls }),
    });

    assert.equal(token, "minted-token");
    assert.equal(new URL(urls[0]).origin, "http://localhost:5476", "dialed as written");
    assert.equal(
      urls.filter((u) => u.includes("127.0.0.1")).length,
      0,
      "no request is addressed to a rewritten literal",
    );
  });

  it("refuses a name when one family it reaches has no entry", async () => {
    // THE SECURITY PIN. `localhost` reaches both loopback families, so a
    // credential sent there can land on whichever family this gateway does NOT
    // hold -- free for a co-resident. Holding only one family is therefore not
    // enough to earn the name, in EITHER direction, and the cost of refusing is
    // one explicit sign-in.
    for (const held of ["127.0.0.1", "::1"]) {
      const files = new Map([
        [listenerSecretPath(CANONICAL_HOME, "5476", held, path), "one-family-secret"],
      ]);
      let called = false;

      const token = await mintLocalToken({
        backendUrl: "http://localhost:5476",
        resolveHome: () => CANONICAL_HOME,
        path,
        fs: fakeFsOver(files),
        http: {
          get: () => {
            called = true;
          },
        },
      });

      assert.equal(token, "", held);
      assert.equal(called, false, `no request is made while holding only ${held}`);
    }
  });

  it("refuses a non-http scheme and an unparseable origin", async () => {
    for (const backendUrl of ["https://localhost:5476", "not a url"]) {
      let called = false;
      const reads = [];
      const token = await mintLocalToken({
        backendUrl,
        resolveHome: () => CANONICAL_HOME,
        path,
        fs: fakeFsOver(new Map(), reads),
        http: { get: () => { called = true; } },
      });

      assert.equal(token, "", backendUrl);
      assert.equal(called, false, backendUrl);
      assert.deepEqual(reads, [], backendUrl);
    }
  });
});

describe("listenerSecretPath", () => {
  it("names the file the gateway publishes for one listener", () => {
    assert.equal(
      listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path),
      path.join(CANONICAL_HOME, "run", "gateway-5476-127.0.0.1.secret"),
    );
  });

  it("is a different file per bind address on one port", () => {
    assert.notEqual(
      listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path),
      listenerSecretPath(CANONICAL_HOME, "5476", "__1", path),
    );
  });
});
