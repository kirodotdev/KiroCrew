const { test } = require("node:test");
const assert = require("node:assert");
const { channelForFlavor, channelForVersion, buildFeedUrl, fetchFeedHttps, initAutoUpdate } = require("../auto-update");

test("channelForVersion: nightly stamp -> nightly feed", () => {
  assert.strictEqual(channelForVersion("0.1.0-nightly.20260721042000"), "nightly");
});

test("channelForVersion mirrors release.yml: any non-nightly prerelease -> insider", () => {
  assert.strictEqual(channelForVersion("0.1.0-insider.1"), "insider");
  assert.strictEqual(channelForVersion("1.2.3-rc.1"), "insider");
});

test("channelForVersion: bare semver -> stable, unstamped/missing -> null", () => {
  assert.strictEqual(channelForVersion("1.2.3"), "stable");
  assert.strictEqual(channelForVersion(undefined), null);
});

test("fetchFeedHttps rejects http on non-loopback hosts", async () => {
  await assert.rejects(
    () => fetchFeedHttps("http://cdn.example.dev/feed/stable/latest-mac.json"),
    /must be https/,
  );
});

test("channelForFlavor maps beta -> insider", () => {
  assert.strictEqual(channelForFlavor("beta"), "insider");
});

test("channelForFlavor maps stable -> stable", () => {
  assert.strictEqual(channelForFlavor("stable"), "stable");
});

test("channelForFlavor defaults non-beta to stable", () => {
  assert.strictEqual(channelForFlavor(undefined), "stable");
  assert.strictEqual(channelForFlavor("anything"), "stable");
});

test("buildFeedUrl points at the channel's static latest-mac.json", () => {
  const url = buildFeedUrl({ base: "https://cdn.example.dev/feed", channel: "insider" });
  assert.strictEqual(url, "https://cdn.example.dev/feed/insider/latest-mac.json");
});

test("buildFeedUrl strips trailing slashes from base", () => {
  const url = buildFeedUrl({ base: "https://cdn.example.dev/feed///", channel: "stable" });
  assert.strictEqual(url, "https://cdn.example.dev/feed/stable/latest-mac.json");
});

test("buildFeedUrl url-encodes the channel segment", () => {
  const url = buildFeedUrl({ base: "https://cdn.example.dev/feed", channel: "a b" });
  assert.strictEqual(url, "https://cdn.example.dev/feed/a%20b/latest-mac.json");
});

// ---------------------------------------------------------------------------
// Client-side compare gate: Squirrel must only be engaged when the static
// feed's version differs from the running app (a static 200 feed would
// otherwise read as "update available" on every check, forever).
// ---------------------------------------------------------------------------

function makeDeps({ appVersion, feed, fetchErr }) {
  const calls = { checkForUpdates: 0, setFeedURL: [], states: [] };
  const handlers = {};
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => appVersion,
      once: () => {},
      removeListener: () => {},
    },
    autoUpdater: {
      setFeedURL: (o) => calls.setFeedURL.push(o.url),
      checkForUpdates: () => { calls.checkForUpdates += 1; },
      on: (ev, fn) => { handlers[ev] = fn; },
      quitAndInstall: () => {},
    },
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "beta",
    stopGateway: async () => {},
    feedBase: "https://cdn.example.dev/feed",
    fetchFeed: async () => {
      if (fetchErr) throw fetchErr;
      return feed;
    },
    onUpdateState: (s) => calls.states.push(s.state),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  return { deps, calls, handlers };
}

// Force darwin so initAutoUpdate does not bail on non-mac test runners.
function withDarwin(fn) {
  const orig = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value: "darwin" });
  return Promise.resolve()
    .then(fn)
    .finally(() => Object.defineProperty(process, "platform", orig));
}

test("same feed version does NOT engage Squirrel (loop guard)", () =>
  withDarwin(async () => {
    const { deps, calls } = makeDeps({
      appVersion: "1.0.0",
      feed: { version: "1.0.0", url: "https://cdn.example.dev/desktop/insider/1.0.0/KiroCrew.zip" },
    });
    const u = initAutoUpdate(deps);
    await u.check();
    // allow the async safeCheck to settle
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(calls.checkForUpdates, 0);
    assert.ok(calls.states.includes("not-available"));
  }));

test("different feed version engages Squirrel with the static feed URL", () =>
  withDarwin(async () => {
    const { deps, calls } = makeDeps({
      appVersion: "1.0.0",
      feed: { version: "1.0.1", url: "https://cdn.example.dev/desktop/stable/1.0.1/KiroCrew.zip" },
    });
    const u = initAutoUpdate(deps);
    await u.check();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(calls.checkForUpdates, 1);
    // Bare-semver running version resolves to the STABLE channel -- the
    // version-derived channel wins over the fixture's "beta" flavor.
    assert.ok(calls.setFeedURL.includes("https://cdn.example.dev/feed/stable/latest-mac.json"));
  }));

test("malformed feed surfaces error and does not engage Squirrel", () =>
  withDarwin(async () => {
    const { deps, calls } = makeDeps({
      appVersion: "1.0.0",
      feed: { name: "no version or url" },
    });
    const u = initAutoUpdate(deps);
    await u.check();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(calls.checkForUpdates, 0);
    assert.ok(calls.states.includes("error"));
  }));

test("feed fetch failure surfaces error and does not engage Squirrel", () =>
  withDarwin(async () => {
    const { deps, calls } = makeDeps({
      appVersion: "1.0.0",
      fetchErr: new Error("feed HTTP 403"),
    });
    const u = initAutoUpdate(deps);
    await u.check();
    await new Promise((r) => setImmediate(r));
    assert.strictEqual(calls.checkForUpdates, 0);
    assert.ok(calls.states.includes("error"));
  }));

test("stamped nightly build tracks the NIGHTLY feed, not stable (no channel migration)", () =>
  withDarwin(async () => {
    const { deps, calls } = makeDeps({
      appVersion: "0.1.0-nightly.20260721042000",
      feed: { version: "0.1.0-nightly.20260722042000", url: "https://cdn.example.dev/desktop/nightly/x/KiroCrew.zip" },
    });
    const u = initAutoUpdate(deps);
    await u.check();
    await new Promise((r) => setImmediate(r));
    assert.ok(calls.setFeedURL.includes("https://cdn.example.dev/feed/nightly/latest-mac.json"));
    assert.strictEqual(calls.checkForUpdates, 1);
  }));

test("fetchFeedHttps accepts plain http on loopback (local update harness)", async () => {
  const http = require("http");
  const srv = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ version: "9.9.9", url: "http://127.0.0.1/z.zip" }));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  try {
    const feed = await fetchFeedHttps(`http://127.0.0.1:${srv.address().port}/feed/stable/latest-mac.json`);
    assert.strictEqual(feed.version, "9.9.9");
  } finally {
    srv.close();
  }
});
