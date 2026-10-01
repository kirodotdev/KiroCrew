"use strict";

/**
 * THE PREDICATE that makes an ambiguous host safe to dial.
 *
 * `localhost` resolves to BOTH loopback families on an ordinary host, so dialing
 * it can land on either listener. Rewriting it to a literal was the earlier
 * answer, and it moved the document's web origin, which strands per-origin
 * storage and splits eight origin-authority comparisons. This is the other
 * answer: leave the name alone, and let the gateway EARN it by holding every
 * family the name can reach.
 *
 * The evidence is already on disk. A gateway publishes
 * `run/gateway-<port>-<address>.secret` per address it bound, so the set of those
 * files says which listeners on this port are this gateway's. One uncovered
 * family is a refusal, because the resolver may hand us exactly that listener.
 */

const assert = require("node:assert/strict");
const path = require("node:path");
const { describe, it } = require("node:test");
const {
  dialTarget,
  listenerSecretsFor,
  encodeBindAddress,
  listenerSecretPath,
  AMBIGUOUS_LOOPBACK_NAMES,
  LOOPBACK_FAMILY_BINDS,
} = require("../local-token");

const HOME = path.resolve(path.sep, "home");

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

function entry(port, address, secret) {
  return [listenerSecretPath(HOME, port, address, path), secret];
}

function secretsFor(backendUrl, files, reads = []) {
  const target = dialTarget(backendUrl);
  assert.ok(target, `dialTarget must accept ${backendUrl}`);
  return listenerSecretsFor({ target, home: HOME, path, fs: fakeFsOver(files, reads) });
}

describe("dialTarget", () => {
  it("names both families for an ambiguous host, and one for a literal", () => {
    for (const name of AMBIGUOUS_LOOPBACK_NAMES) {
      assert.deepEqual(dialTarget(`http://${name}:5476`).families, ["v4", "v6"]);
    }
    assert.deepEqual(dialTarget("http://127.0.0.1:5476").families, ["v4"]);
    assert.deepEqual(dialTarget("http://[::1]:5476").families, ["v6"]);
  });

  it("keeps the origin exactly as written, rewriting no host", () => {
    // The document must never leave the configured origin -- that is the entire
    // reason this shape was chosen over normalizing the host.
    assert.equal(dialTarget("http://localhost:5476").origin, "http://localhost:5476");
    assert.equal(dialTarget("http://localhost:5476/x?y=1").origin, "http://localhost:5476");
  });

  it("spells out the port, since a scheme-default port names no credential file", () => {
    assert.equal(dialTarget("http://localhost").port, "80");
    assert.equal(dialTarget("http://localhost:5476").port, "5476");
  });

  it("refuses anything that is not an http loopback target", () => {
    for (const url of [
      "https://localhost:5476",
      "http://example.com:5476",
      "http://localhost.evil.example:5476",
      "http://10.0.0.5:5476",
      "not a url",
      "",
    ]) {
      assert.equal(dialTarget(url), null, url || "(empty)");
    }
  });
});

describe("listenerSecretsFor", () => {
  it("REFUSES an ambiguous host when only one family is held", () => {
    // THE PIN. The gateway bound v4 only, so `[::1]:5476` is free for a
    // co-resident and `localhost` may resolve straight to it. No secret goes out.
    const files = new Map([entry("5476", "127.0.0.1", "v4-secret")]);
    assert.equal(secretsFor("http://localhost:5476", files), null);
  });

  it("REFUSES an ambiguous host when the other family is the one held", () => {
    // The mirror case, so the refusal is about coverage rather than about v6.
    const files = new Map([entry("5476", "::1", "v6-secret")]);
    assert.equal(secretsFor("http://localhost:5476", files), null);
  });

  it("allows an ambiguous host when the gateway holds both families", () => {
    const files = new Map([
      entry("5476", "127.0.0.1", "one-generation"),
      entry("5476", "::1", "one-generation"),
    ]);
    // ONE generation writes ONE secret under every address it bound, and that
    // shared value is what proves it holds both families.
    assert.deepEqual(secretsFor("http://localhost:5476", files), ["one-generation"]);
  });

  it("collapses one secret held under both families to a single attempt", () => {
    // One gateway publishes the same credential for every address it bound, so
    // the common case must not spend two requests on the same value.
    const files = new Map([
      entry("5476", "127.0.0.1", "one-secret"),
      entry("5476", "::1", "one-secret"),
    ]);
    assert.deepEqual(secretsFor("http://localhost:5476", files), ["one-secret"]);
  });

  it("accepts a wildcard as cover for its own family only", () => {
    // `0.0.0.0` covers v4. `::` covers v6. Neither covers the other: whether a v6
    // wildcard accepts v4-mapped connections depends on the host's IPV6_V6ONLY,
    // which this side cannot establish, so counting it would be a guess in the
    // permissive direction.
    const wildcards = new Map([
      entry("5476", "0.0.0.0", "one-generation"),
      entry("5476", "::", "one-generation"),
    ]);
    assert.deepEqual(secretsFor("http://localhost:5476", wildcards), ["one-generation"]);
    const v6Only = new Map([entry("5476", "::", "v6-wildcard")]);
    assert.equal(secretsFor("http://localhost:5476", v6Only), null, "v6 wildcard is not v4 cover");
    const v4Only = new Map([entry("5476", "0.0.0.0", "v4-wildcard")]);
    assert.equal(secretsFor("http://localhost:5476", v4Only), null, "v4 wildcard is not v6 cover");
  });

  it("asks only for its own family when the host is literal", () => {
    // A literal names one listener, so requiring the other family would refuse a
    // dial that is already unambiguous.
    const files = new Map([entry("5476", "127.0.0.1", "v4-secret")]);
    const reads = [];
    assert.deepEqual(secretsFor("http://127.0.0.1:5476", files, reads), ["v4-secret"]);
    for (const read of reads) {
      assert.equal(read.includes("::"), false, `${read} is not a v4 candidate`);
    }
  });

  it("keys the credential to the dialed port, not a sibling listener", () => {
    const files = new Map([
      entry("5476", "127.0.0.1", "gen-5476"),
      entry("5476", "::1", "gen-5476"),
      entry("9099", "127.0.0.1", "gen-9099"),
      entry("9099", "::1", "gen-9099"),
    ]);
    assert.deepEqual(secretsFor("http://localhost:9099", files), ["gen-9099"]);
  });

  it("never reads the port-keyed entry or the home-wide secret", () => {
    // Both can hold a credential belonging to a gateway this call never reached:
    // the port-keyed name covers every listener on the port, and the shared file
    // is one slot per data home on a last-writer-wins basis.
    const files = new Map([
      entry("5476", "127.0.0.1", "one-generation"),
      entry("5476", "::1", "one-generation"),
      [path.join(HOME, "run", "gateway-5476.secret"), "port-keyed"],
      [path.join(HOME, ".local_secret"), "home-wide"],
    ]);
    const reads = [];
    const secrets = secretsFor("http://localhost:5476", files, reads);
    assert.deepEqual(secrets, ["one-generation"]);
    for (const read of reads) {
      assert.equal(read.endsWith("gateway-5476.secret"), false, "port-keyed entry must not be read");
      assert.equal(read.endsWith(".local_secret"), false, "home-wide secret must not be read");
    }
  });

  it("refuses with nothing published at all", () => {
    const reads = [];
    assert.equal(secretsFor("http://localhost:5476", new Map(), reads), null);
    assert.ok(reads.length > 0, "it looked before refusing");
  });
});

describe("LOOPBACK_FAMILY_BINDS", () => {
  it("covers each family by its own literal and wildcard, and nothing else", () => {
    assert.deepEqual(LOOPBACK_FAMILY_BINDS.v4, ["127.0.0.1", "0.0.0.0"]);
    assert.deepEqual(LOOPBACK_FAMILY_BINDS.v6, ["::1", "::"]);
    // Frozen, because a fifth address added here silently widens what counts as
    // cover for a family and the predicate's refusal is the security boundary.
    assert.equal(Object.isFrozen(LOOPBACK_FAMILY_BINDS.v4), true);
    assert.equal(Object.isFrozen(LOOPBACK_FAMILY_BINDS.v6), true);
  });

  it("offers EVERY entry for a covered family, so a stale one cannot end the walk", () => {
    // `clear_marker` runs only on a graceful shutdown, so a crashed generation
    // leaves its entry behind. Returning just the first covering secret would
    // spend the walk on a dead generation and refuse a gateway that is reachable
    // through the wildcard entry beside it.
    const files = new Map([
      [listenerSecretPath(HOME, "5476", "127.0.0.1", path), "stale-secret"],
      [listenerSecretPath(HOME, "5476", "0.0.0.0", path), "live-secret"],
    ]);
    const secrets = listenerSecretsFor({
      target: dialTarget("http://127.0.0.1:5476"),
      home: HOME,
      path,
      fs: fakeFsOver(files),
    });
    assert.deepEqual(secrets, ["stale-secret", "live-secret"]);
  });
});

describe("the on-disk file name matches the gateway's own", () => {
  // THE CROSS-SEAM PIN, and the reason it is a literal rather than a call to
  // listenerSecretPath: this suite built its fixtures with that same helper, so it
  // agreed with the reader's bug and stayed green while the assembled system could
  // never mint. The literal below is asserted independently in
  // `test/test_instance_credential_pairing.py` against the WRITER's output.
  it("spells a v6 address the way the writer does", () => {
    assert.equal(
      path.basename(listenerSecretPath("/h", "5476", "::1", path)),
      "gateway-5476-__1.secret",
      "a raw `::1` here would look for a file the gateway never writes",
    );
    assert.equal(
      path.basename(listenerSecretPath("/h", "5476", "::", path)),
      "gateway-5476-__.secret",
    );
  });

  it("leaves a v4 address alone", () => {
    assert.equal(
      path.basename(listenerSecretPath("/h", "5476", "127.0.0.1", path)),
      "gateway-5476-127.0.0.1.secret",
    );
  });

  it("rewrites the colon and nothing else", () => {
    assert.equal(encodeBindAddress("::1"), "__1");
    assert.equal(encodeBindAddress("127.0.0.1"), "127.0.0.1");
    assert.equal(encodeBindAddress("fe80::1%eth0"), "fe80__1%eth0");
  });

  it("REFUSES when the families are covered by DIFFERENT generations", () => {
    // THE FAIL-OPEN PIN -- the defect two reviewers found independently.
    //
    // Nothing deletes a sidecar except a graceful shutdown, so a SIGKILLed
    // generation that held both families leaves its v6 entry behind. A co-resident
    // takes that address; this gateway restarts, its second bind gets EADDRINUSE,
    // and it republishes ONLY v4. Counting the survivor as coverage would send the
    // LIVE v4 secret to `localhost`, and an IPv6-first resolver hands it to the
    // squatter.
    //
    // Two different secrets cannot come from one generation, so an empty
    // intersection IS this situation and the answer is refuse.
    const files = new Map([
      entry("5476", "127.0.0.1", "live-v4-secret"),
      entry("5476", "::1", "dead-generations-secret"),
    ]);
    assert.equal(secretsFor("http://localhost:5476", files), null);
  });

  it("never offers a secret only one family can vouch for", () => {
    // The mirror: a live generation on both families PLUS a stale extra entry. Only
    // the shared value is offered, so an orphan is never even tried.
    const files = new Map([
      entry("5476", "127.0.0.1", "one-generation"),
      entry("5476", "0.0.0.0", "someone-elses"),
      entry("5476", "::1", "one-generation"),
    ]);
    assert.deepEqual(secretsFor("http://localhost:5476", files), ["one-generation"]);
  });
});
