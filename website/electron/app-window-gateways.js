"use strict";

const { desktopDeclarations } = require("./app-window-host-rules");

/**
 * app-window-gateways.js: which of the operator's gateways an app's desktop windows show.
 *
 * The desktop window host loads an app's windows from the gateway this shell runs
 * against ("self"). An app holding the `gatewayChoice` capability may instead point
 * its windows at another gateway the operator configured in core's instance registry
 * (`/api/instances`: remote crews forwarded over SSH or SSM). The choice is a fact
 * about this computer, so the shell keeps it, one per app; a window never stores it
 * on whichever gateway served it.
 *
 * Core owns every tunnel. This module rides one only when core reports it already
 * connected, asks core's connected-only `connect` for that crew's token (validated
 * over the live tunnel), and never opens an SSH connection itself or handles a remote
 * secret. A remote is shown only when the same app is enabled THERE, its manifest
 * there declares every window this computer opens (the windows load the remote's own
 * `/popout/desktop/<app>/<id>` route, which closes a window its app does not declare),
 * and its owner there approved `gatewayChoice`, so the windows can always come back.
 * A resolved remote carries the capabilities the app holds THERE (declared and
 * approved), so the host serves a window only what both gateways approved.
 *
 * Every probe is tri-state. A definite answer (feature off, id gone, not connected,
 * app off there) falls back to self; a non-answer (timeout, garbled body, link
 * restarting) keeps whatever is on screen, so one slow tick never rebuilds an app's
 * windows twice. Names no app, and is pure apart from the injected `fetch`, so
 * `test/app-window-gateways.test.js` pins every rule without Electron.
 */

/** The gateway this shell runs against. */
const SELF_GATEWAY = "self";
const ID_MAX = 128;
const LIST_TIMEOUT_MS = 5_000;
const CONNECT_TIMEOUT_MS = 15_000;
const REMOTE_PROBE_TIMEOUT_MS = 8_000;
/** How long one app's listing is reused, so a page polling it costs no extra probes. */
const LIST_REUSE_MS = 10_000;

/** A choice as stored: a non-empty string of bounded length, else self. */
function normalizeChoice(value) {
  return typeof value === "string" && value && value.length <= ID_MAX ? value : SELF_GATEWAY;
}

/**
 * Per-app choices over a key-value store (electron-store in the shell, a Map in tests).
 *
 * The stored id is not checked against the live list: crews come and go, and a
 * choice must survive one being briefly away. Resolution is where self takes over.
 */
function createGatewayChoices(store) {
  const KEY = "appWindowGatewayChoices";
  const read = () => {
    const raw = store.get(KEY);
    return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  };
  return {
    get: (app) => normalizeChoice(Object.prototype.hasOwnProperty.call(read(), app) ? read()[app] : ""),
    set: (app, id) => {
      const next = { ...read() };
      const choice = normalizeChoice(id);
      if (choice === SELF_GATEWAY) delete next[app];
      else next[app] = choice;
      store.set(KEY, next);
      return choice;
    },
  };
}

/** An in-memory store for a host started without a persistent one. */
function memoryStore() {
  const map = new Map();
  return { get: (k) => map.get(k), set: (k, v) => map.set(k, v) };
}

/** Whether core reports this crew's tunnel up with a port to reach it on. */
function isLiveGateway(inst) {
  const port = inst ? Number(inst.local_port) : 0;
  return !!(inst && inst.status && inst.status.state === "connected" && Number.isInteger(port) && port > 0);
}

/** What a page is shown about one crew: no host names, ports or credentials. */
function publicGateway(inst) {
  return {
    id: String(inst.id),
    name: typeof inst.name === "string" && inst.name ? inst.name : String(inst.id),
    state: inst.status && typeof inst.status.state === "string" ? inst.status.state : "unknown",
    connected: isLiveGateway(inst),
  };
}

/**
 * @param {{
 *   fetchHost: (path: string, init?: {method?: string, timeoutMs?: number})
 *     => Promise<{status: number, body: unknown} | null>,
 *   fetchImpl?: typeof fetch,
 *   now?: () => number,
 * }} deps `fetchHost` reaches THIS shell's gateway with its credential and answers
 *   null on a transport failure; `fetchImpl` reaches a remote over its tunnel.
 */
function createGatewayResolver(deps) {
  const fetchHost = deps.fetchHost;
  const fetchImpl = deps.fetchImpl || globalThis.fetch;
  const now = deps.now || Date.now;
  /** app -> {at, key, value}: the last listing for one window set, reused for LIST_REUSE_MS. */
  const listings = new Map();

  /** @returns {Promise<{known: boolean, state: string, instances: object[]}>} */
  async function listInstances() {
    const res = await fetchHost("/api/instances", { timeoutMs: LIST_TIMEOUT_MS });
    // 403 is an answer: multi-instance is off, so there is nothing to point at.
    if (res && res.status === 403) return { known: true, state: "disabled", instances: [] };
    if (!res || res.status !== 200) return { known: false, state: "error", instances: [] };
    const body = res.body;
    const list = Array.isArray(body) ? body : body && body.instances;
    if (!Array.isArray(list)) return { known: false, state: "error", instances: [] };
    const instances = list.filter((i) => i && typeof i.id === "string" && i.id && i.id.length <= ID_MAX);
    // `active: false`: configured, but the manager is not running until a restart.
    return { known: true, state: body && body.active === false ? "inactive" : "ready", instances };
  }

  /** @returns {Promise<{known: boolean, usable?: boolean, localPort?: number, token?: string}>} */
  async function connect(id) {
    // `only_if_connected`: core checks the tunnel is up under its manager lock, so a
    // disconnect landing after the listing is never undone here. A declined connect
    // is a 200 whose state is not `connected`, read below as "not usable".
    const res = await fetchHost(`/api/instances/${encodeURIComponent(id)}/connect?only_if_connected=1`, {
      method: "POST",
      timeoutMs: CONNECT_TIMEOUT_MS,
    });
    if (!res) return { known: false };
    // 403 (feature off) and 404 (id gone) are answers; 5xx is the link or manager down.
    if (res.status === 403 || res.status === 404) return { known: true, usable: false };
    if (res.status !== 200) return { known: false };
    const body = res.body;
    const localPort = Number(body && body.local_port);
    if (body && body.state === "connected" && typeof body.token === "string" && body.token
      && Number.isInteger(localPort) && localPort > 0) {
      return { known: true, usable: true, localPort, token: body.token };
    }
    return { known: true, usable: false };
  }

  /**
   * Whether *app* can show *windowIds* on the crew behind *localPort*:
   * `{state: "enabled" | "disabled" | "unknown", capabilities}`, where `capabilities`
   * are the ones the app declares and has approved there ([] unless enabled).
   * Enabled there is not enough: an older manifest there that lacks one of these
   * windows would close it on load, and a crew that has not approved `gatewayChoice`
   * would serve the windows without the call that brings them back here.
   */
  async function appEnabledOn(app, localPort, token, windowIds) {
    const unknown = { state: "unknown", capabilities: [] };
    if (typeof fetchImpl !== "function") return unknown;
    let body;
    try {
      const res = await fetchImpl(`http://localhost:${localPort}/api/apps?token=${encodeURIComponent(token)}`, {
        redirect: "manual",
        signal: AbortSignal.timeout(REMOTE_PROBE_TIMEOUT_MS),
      });
      if (res.status !== 200) return unknown;
      body = await res.json();
    } catch {
      return unknown;
    }
    const apps = Array.isArray(body) ? body : body && body.apps;
    if (!Array.isArray(apps)) return unknown;
    const there = desktopDeclarations(apps).get(app);
    const declared = new Set(there ? there.windows.map((w) => w.id) : []);
    if (!there || !(windowIds || []).every((id) => declared.has(id))) return { state: "disabled", capabilities: [] };
    if (!there.capabilities.includes("gatewayChoice")) return { state: "disabled", capabilities: [] };
    return { state: "enabled", capabilities: [...there.capabilities] };
  }

  /**
   * Where *app*'s windows (*windowIds*, as declared on this computer) should load from for *choice*.
   *
   * @returns {Promise<{keep: true} | {instanceId: string, baseUrl: string, token: string, capabilities?: string[]}>}
   *   `baseUrl` "" means this shell's own gateway; `keep` means "could not tell".
   *   A crew's target carries `capabilities`: what the app holds there.
   */
  async function resolve(app, choice, windowIds) {
    const self = { instanceId: SELF_GATEWAY, baseUrl: "", token: "" };
    if (normalizeChoice(choice) === SELF_GATEWAY) return self;
    const listed = await listInstances();
    if (!listed.known) return { keep: true };
    const inst = listed.instances.find((i) => i.id === choice);
    // Listed but down, or gone: a fact. The shell never brings a tunnel up itself.
    if (!isLiveGateway(inst)) return self;
    const conn = await connect(choice);
    if (!conn.known) return { keep: true };
    if (!conn.usable) return self;
    const there = await appEnabledOn(app, conn.localPort, conn.token, windowIds);
    if (there.state === "unknown") return { keep: true };
    if (there.state === "disabled") return self;
    // localhost, as every gateway origin in this shell, so per-port cookies stay apart.
    return {
      instanceId: choice,
      baseUrl: `http://localhost:${conn.localPort}`,
      token: conn.token,
      capabilities: there.capabilities,
    };
  }

  /**
   * The crews a page may choose among, with whether *app* can show *windowIds* in each connected one.
   *
   * Only connected crews are probed (their tunnel is already up), so listing opens
   * no connection. A crew that does not answer reads as `appEnabled: null`.
   */
  async function list(app, windowIds) {
    const key = JSON.stringify(windowIds || []);
    const reused = listings.get(app);
    if (reused && reused.key === key && now() - reused.at < LIST_REUSE_MS) return reused.value;
    const value = await listFresh(app, windowIds);
    listings.set(app, { at: now(), key, value });
    return value;
  }

  async function listFresh(app, windowIds) {
    const listed = await listInstances();
    if (!listed.known) return { state: listed.state, gateways: [] };
    const gateways = await Promise.all(listed.instances.map(async (inst) => {
      const view = publicGateway(inst);
      if (!view.connected) return { ...view, appEnabled: null };
      try {
        const conn = await connect(inst.id);
        if (!conn.known || !conn.usable) return { ...view, appEnabled: null };
        const there = await appEnabledOn(app, conn.localPort, conn.token, windowIds);
        return { ...view, appEnabled: there.state === "unknown" ? null : there.state === "enabled" };
      } catch {
        return { ...view, appEnabled: null };
      }
    }));
    return { state: listed.state, gateways };
  }

  /**
   * Whether *target*, a crew resolved earlier, is still that crew on the same port:
   * `{known: false}` when core does not answer, else `{known: true, ok}`.
   *
   * A disconnect frees the crew's local port and the next crew to connect can be
   * given it, so a reused target is re-read from core's registry before a window
   * loads from it: a port that is no longer that crew's is never sent its credential.
   */
  async function confirm(target) {
    const listed = await listInstances();
    if (!listed.known) return { known: false };
    const inst = listed.instances.find((i) => i.id === target.instanceId);
    const ok = isLiveGateway(inst) && `http://localhost:${Number(inst.local_port)}` === target.baseUrl;
    return { known: true, ok };
  }

  /** Forget *app*'s reused listing, so the next one after a choice reads fresh. */
  function forget(app) {
    listings.delete(app);
  }

  return { confirm, forget, list, resolve, _listInstances: listInstances, _connect: connect, _appEnabledOn: appEnabledOn };
}

module.exports = {
  SELF_GATEWAY,
  createGatewayChoices,
  createGatewayResolver,
  isLiveGateway,
  memoryStore,
  normalizeChoice,
  publicGateway,
};
