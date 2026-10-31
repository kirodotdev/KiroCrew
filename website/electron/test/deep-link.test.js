const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  SCHEME,
  deepLinkFromArgv,
  resolveDeepLink,
} = require("../deep-link");

describe("resolveDeepLink accepts exactly three shapes", () => {
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

  it("apps/detail/<name> routes to that app's detail page and carries no session", () => {
    assert.deepEqual(resolveDeepLink("kirocrew://apps/detail/keeb-crew"), {
      ok: true,
      route: "/apps/detail/keeb-crew",
      slotKey: "",
    });
    // A trailing slash and host case are the URL parser's business, as for chat.
    assert.deepEqual(resolveDeepLink("kirocrew://apps/detail/mochi/"), { ok: true, route: "/apps/detail/mochi", slotKey: "" });
    assert.deepEqual(resolveDeepLink("kirocrew://APPS/detail/a1"), { ok: true, route: "/apps/detail/a1", slotKey: "" });
  });

  it("an app name is admitted only in the manifest's own kebab-case grammar", () => {
    // Mirrors KEBAB_RE in src/kiro_crew/apps/manifest_names.py: a name the
    // manifest validator would refuse never becomes a route.
    for (const raw of [
      "kirocrew://apps/detail/Keeb-Crew",
      "kirocrew://apps/detail/keeb_crew",
      "kirocrew://apps/detail/-keeb",
      "kirocrew://apps/detail/keeb-",
      "kirocrew://apps/detail/keeb--crew",
      "kirocrew://apps/detail/keeb.crew",
      "kirocrew://apps/detail/keeb%2Dcrew",
      `kirocrew://apps/detail/${"a".repeat(201)}`,
    ]) {
      assert.deepEqual(resolveDeepLink(raw), { ok: false, reason: "invalid app name" }, raw);
    }
    assert.equal(resolveDeepLink(`kirocrew://apps/detail/${"a".repeat(200)}`).ok, true);
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
    // Under apps/ only the detail page is a target: not the store pages, not the
    // app's own page, and nothing the page could read as an instruction.
    ["kirocrew://apps", "unknown route"],
    ["kirocrew://apps/library", "unknown route"],
    ["kirocrew://apps/-/updates", "unknown route"],
    ["kirocrew://apps/keeb-crew", "unknown route"],
    ["kirocrew://apps/migrate/keeb-crew", "unknown route"],
    ["kirocrew://apps/detail", "unknown route"],
    ["kirocrew://apps/detail/keeb-crew/settings", "unknown route"],
    ["kirocrew://apps/detail/keeb-crew?autoAction=install", "unknown query parameter"],
    ["kirocrew://apps/detail/keeb-crew?sid=x", "unknown query parameter"],
    ["kirocrew://apps/detail/keeb-crew#enable", "fragment not allowed"],
    ["kirocrew://apps:5476/detail/keeb-crew", "credentials or port in URL"],
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
    const appProbes = [
      "kirocrew://apps/detail/%0D%0Ainjected",
      "kirocrew://apps/detail/x%00y",
      "kirocrew://apps/detail/javascript:alert(1)",
      "kirocrew://apps/detail/..%2F..%2Fsettings",
      "kirocrew://apps/detail/keeb-crew%3Fautoaction%3Dinstall",
    ];
    for (const raw of appProbes) {
      const r = resolveDeepLink(raw);
      if (r.ok) assert.match(r.route, /^\/apps\/detail\/[a-z0-9-]+$/, raw);
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

  it("dot-segments cannot turn an app link into another route", () => {
    // `..` is collapsed before the segments are read, so a traversal lands on
    // a two-segment detail path (rebuilt under /apps/detail/) or is refused.
    assert.deepEqual(resolveDeepLink("kirocrew://apps/../detail/mochi"), { ok: true, route: "/apps/detail/mochi", slotKey: "" });
    for (const raw of [
      "kirocrew://apps/detail/../library",
      "kirocrew://apps/detail/../../settings",
      "kirocrew://apps/detail/keeb-crew/../../../settings",
      "kirocrew://chat/../apps/detail/mochi?sid=k",
    ]) {
      const r = resolveDeepLink(raw);
      if (r.ok) assert.match(r.route, /^\/apps\/detail\/[a-z0-9-]+$/, raw);
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
