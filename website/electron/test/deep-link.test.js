const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  SCHEME,
  deepLinkFromArgv,
  resolveDeepLink,
} = require("../deep-link");

describe("resolveDeepLink accepts exactly two shapes", () => {
  it("bare scheme raises the window and routes nowhere", () => {
    for (const raw of ["kirocrew://", "kirocrew:", "KIROCREW://"]) {
      assert.deepEqual(resolveDeepLink(raw), { ok: true, route: "", slotKey: "" }, raw);
    }
  });

  it("chat?sid= routes to the session and reports the slot key", () => {
    const r = resolveDeepLink("kirocrew://chat?sid=cos-quick-chat-high");
    assert.deepEqual(r, { ok: true, route: "/chat?sid=cos-quick-chat-high", slotKey: "cos-quick-chat-high" });
    assert.deepEqual(resolveDeepLink("kirocrew://chat/?sid=k"), { ok: true, route: "/chat?sid=k", slotKey: "k" });
  });

  it("re-encodes the sid rather than copying the input", () => {
    // `:` and `.` are legal in gateway slot keys; the route must carry them
    // encoded exactly as the SPA's own deep link does.
    const r = resolveDeepLink("kirocrew://chat?sid=slack_1712345678.123456");
    assert.equal(r.ok, true);
    assert.equal(r.route, `/chat?sid=${encodeURIComponent("slack_1712345678.123456")}`);
    const r2 = resolveDeepLink("kirocrew://chat?sid=hook%3Acos%3Amenubar-chat");
    assert.equal(r2.ok, true);
    assert.equal(r2.slotKey, "hook:cos:menubar-chat");
    assert.equal(r2.route, "/chat?sid=hook%3Acos%3Amenubar-chat");
  });
});

describe("resolveDeepLink refuses everything else", () => {
  const refused = [
    ["", "empty"],
    ["not a url", "unparseable"],
    ["https://github.com/kirodotdev/KiroCrew", "wrong scheme"],
    ["kirocrew://user:pw@chat", "credentials or port in URL"],
    ["kirocrew://chat:5476", "credentials or port in URL"],
    ["kirocrew://chat#frag", "fragment not allowed"],
    ["kirocrew://evil", "unknown route"],
    ["kirocrew://chat/extra?sid=x", "unknown route"],
    ["kirocrew://settings", "unknown route"],
    ["kirocrew://settings/browser", "unknown route"],
    ["kirocrew://artifacts/cr-queue", "unknown route"],
    ["kirocrew://chat", "missing sid"],
    ["kirocrew://chat?token=abc", "unknown query parameter"],
    ["kirocrew://chat?sid=a&token=b", "unknown query parameter"],
    ["kirocrew://chat?sid=a&sid=b", "unknown query parameter"],
    ["kirocrew://?sid=x", "unknown route"],
    ["kirocrew://chat?sid=", "invalid sid"],
    ["kirocrew://chat?sid=%3Cscript%3E", "invalid sid"],
    ["kirocrew://chat?sid=a%20b", "invalid sid"],
    ["kirocrew://chat?sid=-leading-dash", "invalid sid"],
    [`kirocrew://chat?sid=${"a".repeat(201)}`, "invalid sid"],
  ];
  for (const [raw, reason] of refused) {
    it(`${JSON.stringify(raw).slice(0, 60)} -> ${reason}`, () => {
      const r = resolveDeepLink(raw);
      assert.equal(r.ok, false);
      assert.equal(r.reason, reason);
    });
  }

  it("never returns a route containing characters outside the admitted set", () => {
    const probes = [
      "kirocrew://chat?sid=%0D%0Ainjected",
      "kirocrew://chat?sid=x%00y",
      "kirocrew://chat?sid=javascript:alert(1)",
      "kirocrew://chat?sid=..%2F..%2Fetc",
    ];
    for (const raw of probes) {
      const r = resolveDeepLink(raw);
      if (r.ok) assert.match(r.route, /^\/chat\?sid=[A-Za-z0-9._%-]*$/, raw);
    }
  });

  it("dot-segments are normalized by the URL parser and cannot escape the chat route", () => {
    // WHATWG parsing collapses `..` before this module sees the path, so a
    // traversal attempt lands on the chat route's root or on a refused path.
    assert.deepEqual(resolveDeepLink("kirocrew://chat/..?sid=k"), { ok: true, route: "/chat?sid=k", slotKey: "k" });
    assert.deepEqual(resolveDeepLink("kirocrew://chat/%2e%2e?sid=k"), { ok: true, route: "/chat?sid=k", slotKey: "k" });
    for (const raw of ["kirocrew://chat/../settings?sid=k", "kirocrew://settings/../chat?sid=k"]) {
      const r = resolveDeepLink(raw);
      if (r.ok) assert.match(r.route, /^\/chat\?sid=k$/, raw);
      else assert.equal(r.reason, "unknown route", raw);
    }
  });
});

describe("deepLinkFromArgv", () => {
  it("picks the kirocrew URL out of a Windows/Linux second-instance argv", () => {
    assert.equal(
      deepLinkFromArgv(["/usr/bin/kirocrew", "--no-sandbox", "kirocrew://chat?sid=x"]),
      "kirocrew://chat?sid=x",
    );
  });
  it("answers empty for a macOS launch (no URL in argv) and for junk", () => {
    assert.equal(deepLinkFromArgv(["/Applications/KiroCrew.app/Contents/MacOS/KiroCrew"]), "");
    assert.equal(deepLinkFromArgv(["x", "https://example.com"]), "");
    assert.equal(deepLinkFromArgv(null), "");
  });
  it("takes the last URL when more than one is present", () => {
    assert.equal(deepLinkFromArgv(["bin", "kirocrew://chat?sid=a", "kirocrew://chat?sid=b"]), "kirocrew://chat?sid=b");
  });
});

describe("the scheme is registered with electron-builder", () => {
  it("package.json build.protocols names the same scheme the resolver accepts", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
    const protocols = pkg.build && pkg.build.protocols;
    assert.ok(Array.isArray(protocols) && protocols.length, "build.protocols is declared");
    const schemes = protocols.flatMap((p) => p.schemes || []);
    assert.ok(schemes.includes(SCHEME), `protocols.schemes includes ${SCHEME}`);
  });
});
