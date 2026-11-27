const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  createTokenRetryHandler,
  dashboardRetryPath,
  dashboardUrlFor,
  loadDashboardWithToken,
} = require("../token-retry");

describe("createTokenRetryHandler", () => {
  it("calls refreshFn on 403", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(403);
    assert.equal(called, 1);
  });

  it("does not call refreshFn on 200", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(200);
    assert.equal(called, 0);
  });

  it("stops after maxRetries (default 2)", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(403);
    await handler(403);
    await handler(403); // should be ignored
    await handler(403); // should be ignored
    assert.equal(called, 2);
  });

  it("resets retries on 200", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(403);
    await handler(403);
    assert.equal(called, 2);
    await handler(200); // reset
    await handler(403); // should work again
    assert.equal(called, 3);
  });

  it("respects custom maxRetries", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; }, 1);
    await handler(403);
    await handler(403); // should be ignored
    assert.equal(called, 1);
  });

  it("ignores other status codes", async () => {
    let called = 0;
    const handler = createTokenRetryHandler(() => { called++; });
    await handler(301);
    await handler(404);
    await handler(500);
    assert.equal(called, 0);
  });
});

describe("dashboardRetryPath", () => {
  const BACKEND = "http://localhost:3113";

  it("follows the window past a consumed ?new=1 intent", () => {
    // The SPA replaced /chat?new=1 with the slot it minted. A retry must not
    // re-run the intent, or it mints a second session over this one.
    assert.equal(
      dashboardRetryPath(`${BACKEND}/chat?sid=slot-abc&token=stale`, BACKEND, "/chat?new=1"),
      "/chat?sid=slot-abc",
    );
  });

  it("keeps an entry intent that has not been consumed yet", () => {
    assert.equal(
      dashboardRetryPath(`${BACKEND}/chat?new=1&token=stale`, BACKEND, "/chat?new=1"),
      "/chat?new=1",
    );
  });

  it("drops the stale token so the caller can append a fresh one", () => {
    assert.equal(dashboardRetryPath(`${BACKEND}/?token=stale`, BACKEND, ""), "/");
  });

  it("keeps the fallback for a URL this gateway does not serve", () => {
    // The splash screen is a local file:// load, and a second connection window
    // points at a different port — neither should retarget this window.
    assert.equal(dashboardRetryPath("file:///app/loading.html", BACKEND, "/chat?new=1"), "/chat?new=1");
    assert.equal(dashboardRetryPath("about:blank", BACKEND, "/chat?new=1"), "/chat?new=1");
    assert.equal(dashboardRetryPath("http://localhost:9999/chat", BACKEND, "/chat?new=1"), "/chat?new=1");
  });

  it("keeps the fallback for an unparseable URL", () => {
    assert.equal(dashboardRetryPath("", BACKEND, "/chat?new=1"), "/chat?new=1");
    assert.equal(dashboardRetryPath(undefined, BACKEND, "/chat?new=1"), "/chat?new=1");
  });
});

describe("dashboardUrlFor", () => {
  // Same-origin paths resolve exactly as a plain `new URL(path, base)` does.
  const KEPT = [
    ["http://127.0.0.1:5476", "/chat?sid=abc#m-3", "http://127.0.0.1:5476/chat?sid=abc#m-3"],
    ["http://localhost:5476", "/settings/about?x=1&y=%2F#h", "http://localhost:5476/settings/about?x=1&y=%2F#h"],
    ["http://localhost:5476", "/chat?new=1", "http://localhost:5476/chat?new=1"],
    ["http://[::1]:5476", "/chat?sid=a", "http://[::1]:5476/chat?sid=a"],
    ["http://localhost:5476/", "/apps/x/?q=a%20b#frag", "http://localhost:5476/apps/x/?q=a%20b#frag"],
    ["http://localhost:43123", "/chat?sid=r", "http://localhost:43123/chat?sid=r"],
    ["http://127.0.0.1:5476", "?x=1", "http://127.0.0.1:5476/?x=1"],
    ["http://127.0.0.1:5476", "#only-hash", "http://127.0.0.1:5476/#only-hash"],
    ["http://127.0.0.1:5476", "/%2F%2Fevil", "http://127.0.0.1:5476/%2F%2Fevil"],
  ];
  for (const [base, path, expected] of KEPT) {
    it(`keeps ${JSON.stringify(path)} on ${base}`, () => {
      assert.equal(dashboardUrlFor(base, path), expected);
      assert.equal(dashboardUrlFor(base, path), new URL(path, base).toString());
    });
  }

  // Paths that would resolve to another origin land on the gateway root.
  const BASE = "http://127.0.0.1:5476";
  const ROOT = "http://127.0.0.1:5476/";
  const LEAVING = [
    "//evil/x",
    "/\\evil/x",
    "/\t/evil",
    "/\n/evil",
    "\\\\evil",
    "https://evil/",
    "http://127.0.0.1:9999/chat",
    "javascript:alert(1)",
    "data:text/html,x",
    "file:///etc/passwd",
    "blob:http://127.0.0.1:5476/id",
    "filesystem:http://127.0.0.1:5476/temporary/x",
  ];
  for (const path of LEAVING) {
    it(`sends ${JSON.stringify(path)} to the root`, () => {
      assert.equal(dashboardUrlFor(BASE, path), ROOT);
    });
  }

  it("returns the root for an empty or missing path", () => {
    assert.equal(dashboardUrlFor(BASE, ""), ROOT);
    assert.equal(dashboardUrlFor(BASE), ROOT);
    assert.equal(dashboardUrlFor(BASE, undefined), ROOT);
  });

  it("returns the root for a path that does not parse", () => {
    assert.equal(dashboardUrlFor(BASE, "http://[bad"), ROOT);
  });

  it("still throws for a bad backend URL, as before", () => {
    assert.throws(() => dashboardUrlFor("not a url", "/chat"));
  });
});

describe("loadDashboardWithToken", () => {
  function fakeContents(fetchImpl) {
    const calls = [];
    return {
      calls,
      session: {
        fetch: async (url, init) => {
          calls.push(["fetch", url, init]);
          return fetchImpl();
        },
      },
      isDestroyed: () => false,
      loadURL: (url) => calls.push(["load", url]),
    };
  }

  it("signs in through the window's session, then loads the page without the token", async () => {
    // Chromium logs the page's address beside every console line, so the token
    // goes on a request on the page's own origin and never into the address.
    const contents = fakeContents(async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(0) }));
    const token = "token with spaces & punctuation?";
    await loadDashboardWithToken(contents, "http://localhost:5476/chat?new=1", token);

    assert.equal(contents.calls.length, 2);
    const [[firstKind, exchangeUrl, init], [secondKind, loaded]] = contents.calls;
    assert.equal(firstKind, "fetch");
    // Bounded: the boot path starts the liveness monitor only after the load.
    assert.ok(init.signal instanceof AbortSignal);
    // The very request a tokened load made, so any gateway that took one takes this.
    const exchange = new URL(exchangeUrl);
    assert.equal(exchange.origin, "http://localhost:5476");
    assert.equal(exchange.pathname, "/chat");
    assert.equal(exchange.searchParams.get("new"), "1");
    assert.deepEqual(exchange.searchParams.getAll("token"), [token]);
    assert.deepEqual([secondKind, loaded], ["load", "http://localhost:5476/chat?new=1"]);
  });

  it("loads nothing into a window closed during the exchange", async () => {
    const contents = fakeContents(async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(0) }));
    contents.isDestroyed = () => contents.calls.length > 0;
    await loadDashboardWithToken(contents, "http://localhost:5476", "secret-link");

    assert.deepEqual(contents.calls.map(([kind]) => kind), ["fetch"]);
  });

  it("never puts the token in the address when the exchange fails", async () => {
    const contents = fakeContents(async () => { throw new Error("net::ERR_CONNECTION_REFUSED"); });
    await loadDashboardWithToken(contents, "http://localhost:5476", "secret-link");

    const loads = contents.calls.filter(([kind]) => kind === "load").map(([, url]) => url);
    assert.deepEqual(loads, ["http://localhost:5476"]);
  });
});
