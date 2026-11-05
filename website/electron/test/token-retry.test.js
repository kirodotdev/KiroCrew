const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createTokenRetryHandler, dashboardRetryPath, loadDashboardWithToken } = require("../token-retry");

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
