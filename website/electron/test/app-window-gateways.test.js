/**
 * Which of the operator's gateways an app's windows show: every answer core can
 * give, and what each means. A definite answer falls back to this computer; a
 * non-answer keeps what is on screen ({keep: true}); only a connected crew where
 * the same app is on is ever a target, and the shell never opens a tunnel.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  SELF_GATEWAY,
  createGatewayChoices,
  createGatewayResolver,
  isLiveGateway,
  memoryStore,
  publicGateway,
} = require("../app-window-gateways");

const LIVE = { id: "crew", name: "Box", ssh_host: "h.example", local_port: 7778, status: { state: "connected" } };
const DOWN = { id: "cold", name: "Cold", local_port: 7779, status: { state: "disconnected" } };

/** host: path -> {status, body} | null; remote: {status, body} | Error. */
function resolver({ host, remote, now }) {
  const hostCalls = [];
  const remoteCalls = [];
  const r = createGatewayResolver({
    fetchHost: async (p, init) => {
      hostCalls.push([init && init.method === "POST" ? "POST" : "GET", p]);
      return Object.prototype.hasOwnProperty.call(host, p) ? host[p] : { status: 404, body: null };
    },
    fetchImpl: async (url) => {
      remoteCalls.push(url);
      if (remote instanceof Error) throw remote;
      return { status: remote.status, json: async () => remote.body };
    },
    now,
  });
  return { r, hostCalls, remoteCalls };
}

/** The windows this computer opens for "pets", and a remote row declaring *ids*. */
const WINDOWS = ["panel"];
const petsRow = (ids = WINDOWS, extra = {}) => ({
  name: "pets",
  enabled: true,
  manifest: { name: "pets", desktop: { windows: ids.map((id) => ({ id, entry: `desktop/${id}.mjs`, kind: "panel" })) } },
  ...extra,
});

/** Row fields for an app whose owner there approved *approved* and left *staged* waiting. */
const granted = (approved, staged = []) => ({
  desktopCapabilitiesGrant: { approved, staged },
  manifest: {
    name: "pets",
    desktop: { windows: WINDOWS.map((id) => ({ id, entry: `desktop/${id}.mjs`, kind: "panel" })) },
    permissions: { desktopCapabilities: [...approved, ...staged] },
  },
});

const listed = (instances, extra = {}) => ({ status: 200, body: { active: true, instances, ...extra } });
/** Connected-only: core refuses under its lock to reopen a tunnel a disconnect closed. */
const CONNECT = "/api/instances/crew/connect?only_if_connected=1";
const connected = { status: 200, body: { state: "connected", local_port: 7778, token: "rt" } };

test("self needs no request at all", async () => {
  const { r, hostCalls } = resolver({ host: {}, remote: { status: 200, body: [] } });
  assert.deepStrictEqual(await r.resolve("pets", SELF_GATEWAY), { instanceId: "self", baseUrl: "", token: "" });
  assert.deepStrictEqual(await r.resolve("pets", ""), { instanceId: "self", baseUrl: "", token: "" });
  assert.deepStrictEqual(hostCalls, []);
});

test("a connected crew with the app on is the target", async () => {
  const { r, hostCalls, remoteCalls } = resolver({
    host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
    remote: { status: 200, body: { apps: [petsRow(WINDOWS, granted(["gatewayChoice"]))] } },
  });
  assert.deepStrictEqual(await r.resolve("pets", "crew", WINDOWS), {
    instanceId: "crew",
    baseUrl: "http://localhost:7778",
    token: "rt",
    capabilities: ["gatewayChoice"],
  });
  assert.deepStrictEqual(hostCalls, [["GET", "/api/instances"], ["POST", CONNECT]]);
  assert.deepStrictEqual(remoteCalls, ["http://localhost:7778/api/apps?token=rt"]);
});

test("a crew's target carries what the app declares AND approved there, nothing else", async () => {
  const declared = ["localFiles", "screenCapture", "gatewayChoice"];
  const cases = [
    {
      grant: { approved: ["screenCapture", "gatewayChoice"], staged: ["localFiles"] },
      want: ["screenCapture", "gatewayChoice"],
    },
    { grant: { approved: ["gatewayChoice"], staged: ["localFiles", "screenCapture"] }, want: ["gatewayChoice"] },
  ];
  for (const c of cases) {
    const row = petsRow(WINDOWS, c.grant ? { desktopCapabilitiesGrant: c.grant } : {});
    row.manifest.permissions = { desktopCapabilities: declared };
    const { r } = resolver({
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: { apps: [row] } },
    });
    assert.deepStrictEqual((await r.resolve("pets", "crew", WINDOWS)).capabilities, c.want);
  }
});

test("a definite no falls back to self", async () => {
  const self = { instanceId: "self", baseUrl: "", token: "" };
  const cases = [
    { host: { "/api/instances": { status: 403, body: null } }, why: "multi-instance off" },
    { host: { "/api/instances": listed([]) }, why: "the id is gone" },
    { host: { "/api/instances": listed([{ ...DOWN, id: "crew" }]) }, why: "listed but down" },
    { host: { "/api/instances": listed([LIVE]), [CONNECT]: { status: 404, body: null } }, why: "connect says gone" },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: { status: 200, body: { state: "connecting" } } },
      why: "connect answered with no tunnel",
    },
    {
      host: {
        "/api/instances": listed([LIVE]),
        [CONNECT]: { status: 200, body: { state: "disconnected", code: "instance_not_connected" } },
      },
      why: "disconnected after the listing: core declines rather than reconnecting",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: [petsRow(WINDOWS, { enabled: false })] },
      why: "the app is off there",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: [{ name: "other", enabled: true }] },
      why: "the app is not installed there",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: [petsRow(["pet"])] },
      why: "an older manifest there lacks a window this computer opens, which its page would close",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: [{ name: "pets", enabled: true, manifest: { name: "pets" } }] },
      why: "the app there declares no desktop windows",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: [petsRow(WINDOWS, granted(["screenCapture"], ["gatewayChoice"]))] },
      why: "the owner there has not approved gatewayChoice, so the windows could not come back",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 200, body: [petsRow()] },
      why: "a gateway that predates the grant approves nothing there, gatewayChoice included",
    },
  ];
  for (const c of cases) {
    const { r } = resolver({ host: c.host, remote: c.remote || { status: 200, body: [] } });
    assert.deepStrictEqual(await r.resolve("pets", "crew", WINDOWS), self, c.why);
  }
});

test("a non-answer keeps what is on screen", async () => {
  const cases = [
    { host: { "/api/instances": null }, why: "the host did not answer" },
    { host: { "/api/instances": { status: 500, body: null } }, why: "the host errored" },
    { host: { "/api/instances": { status: 200, body: { nope: 1 } } }, why: "a garbled list" },
    { host: { "/api/instances": listed([LIVE]), [CONNECT]: { status: 502, body: null } }, why: "the link is down" },
    { host: { "/api/instances": listed([LIVE]), [CONNECT]: null }, why: "connect timed out" },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: new Error("ECONNRESET"),
      why: "the crew did not answer",
    },
    {
      host: { "/api/instances": listed([LIVE]), [CONNECT]: connected },
      remote: { status: 503, body: null },
      why: "the crew errored",
    },
  ];
  for (const c of cases) {
    const { r } = resolver({ host: c.host, remote: c.remote || { status: 200, body: [] } });
    assert.deepStrictEqual(await r.resolve("pets", "crew", WINDOWS), { keep: true }, c.why);
  }
});

test("listing opens no tunnel and shows a page no host name, port or credential", async () => {
  let t = 0;
  const { r, hostCalls } = resolver({
    host: { "/api/instances": listed([LIVE, DOWN]), [CONNECT]: connected },
    remote: { status: 200, body: [petsRow(WINDOWS, granted(["gatewayChoice"]))] },
    now: () => t,
  });
  const out = await r.list("pets", WINDOWS);
  assert.deepStrictEqual(out, {
    state: "ready",
    gateways: [
      { id: "crew", name: "Box", state: "connected", connected: true, appEnabled: true },
      { id: "cold", name: "Cold", state: "disconnected", connected: false, appEnabled: null },
    ],
  });
  assert.ok(!hostCalls.some(([, p]) => p === "/api/instances/cold/connect?only_if_connected=1"), "a down crew is not connected");
  // Polled again within the reuse window: no new request.
  const before = hostCalls.length;
  t = 5_000;
  await r.list("pets", WINDOWS);
  assert.strictEqual(hostCalls.length, before);
  r.forget("pets");
  await r.list("pets", WINDOWS);
  assert.ok(hostCalls.length > before, "forget reads fresh");
});

test("confirm: a held crew is still itself only while core shows it connected on the same port", async () => {
  const held = { instanceId: "crew", baseUrl: "http://localhost:7778", token: "rt" };
  const on = (instances) => resolver({ host: { "/api/instances": listed(instances) }, remote: { status: 200, body: [] } }).r;
  assert.deepStrictEqual(await on([LIVE]).confirm(held), { known: true, ok: true });
  // Disconnected, its port now another crew's, moved to another port, or gone: a definite no.
  assert.deepStrictEqual(await on([{ ...LIVE, status: { state: "disconnected" } }, { ...LIVE, id: "other" }]).confirm(held), { known: true, ok: false });
  assert.deepStrictEqual(await on([{ ...LIVE, local_port: 7790 }]).confirm(held), { known: true, ok: false });
  assert.deepStrictEqual(await on([]).confirm(held), { known: true, ok: false });
  const { r, remoteCalls } = resolver({ host: { "/api/instances": null }, remote: { status: 200, body: [] } });
  assert.deepStrictEqual(await r.confirm(held), { known: false }, "core not answering is no answer");
  assert.deepStrictEqual(remoteCalls, [], "confirming sends nothing to the port");
});

test("an inactive manager is reported as such, with nothing to choose", async () => {
  const { r } = resolver({ host: { "/api/instances": listed([], { active: false }) }, remote: { status: 200, body: [] } });
  assert.deepStrictEqual(await r.list("pets", WINDOWS), { state: "inactive", gateways: [] });
});

test("choices are kept per app; self is stored as no entry", () => {
  const store = memoryStore();
  const choices = createGatewayChoices(store);
  assert.strictEqual(choices.get("pets"), "self");
  assert.strictEqual(choices.set("pets", "crew"), "crew");
  assert.strictEqual(choices.set("notes", "x".repeat(200)), "self", "an oversized id is no choice");
  assert.strictEqual(choices.get("pets"), "crew");
  assert.strictEqual(choices.get("notes"), "self");
  choices.set("pets", "self");
  assert.deepStrictEqual(store.get("appWindowGatewayChoices"), {});
  store.set("appWindowGatewayChoices", ["corrupt"]);
  assert.strictEqual(choices.get("pets"), "self");
});

test("isLiveGateway and publicGateway", () => {
  assert.strictEqual(isLiveGateway(LIVE), true);
  assert.strictEqual(isLiveGateway(DOWN), false);
  assert.strictEqual(isLiveGateway({ ...LIVE, local_port: 0 }), false);
  assert.strictEqual(isLiveGateway(null), false);
  assert.deepStrictEqual(publicGateway({ id: "x", status: {} }), { id: "x", name: "x", state: "unknown", connected: false });
});
