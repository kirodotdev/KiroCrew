/**
 * Mochi shell facade — the ONE surface main.js touches.
 *
 * Everything the Electron shell runs for the Mochi builtin lives under
 * electron/mochi/ (a deliberate FIRST-PARTY exception: window-layer code needs
 * main-process APIs, and a generic "apps run JS in the main process" extension
 * point was rejected as a sandbox bypass). This module is the single entry:
 * main.js calls initMochi() once at startup and shutdownMochi() once at quit,
 * and injects the three pieces of shell environment the watcher needs. No
 * other Mochi symbol crosses into main.js, so removing the app from the shell
 * is deleting this folder and the two calls.
 */

const http = require("http");
const { app, ipcMain } = require("electron");
const Store = require("electron-store");
const { parseMochiEnabled, enabledOrTrust, hostDisabledMeansTeardown } = require("./instanceGate");
const {
  SELF_INSTANCE,
  MACHINE_STORE_DEFAULTS,
  MIGRATED_KEY,
  petInstanceOf,
  setPetInstanceIn,
  shortcutsOf,
  setShortcutsIn,
  mergeAcceptedShortcuts,
  migrateMachinePrefs,
} = require("./machineStore");

/**
 * Mochi's own per-machine store, SEPARATE from the shell's main one.
 *
 * A distinct electron-store file (`mochi-machine.json`) rather than a few more
 * keys in main.js's store: this folder is Mochi's, so its state stays inside it
 * and removing the app stays "delete this folder and the two calls in main.js",
 * exactly as this module's header promises.
 */
const machineStore = new Store({ name: "mochi-machine", defaults: MACHINE_STORE_DEFAULTS });

// Injected by initMochi(); placeholders keep every function definable at load.
let BACKEND_URL = "";
let fetchLocalToken = async () => "";
let glog = () => {};

/**
 * Open Mochi's pet overlay when the builtin is enabled.
 *
 * Mochi ships defaultEnabled:false, so this is a no-op for anyone who has not
 * turned it on in the App Store. Enabled state lives in the gateway (it is an
 * app, not a shell setting), so the shell has to ask — using the same local
 * token path the dashboard window uses.
 *
 * Everything is best-effort: any failure (no token, gateway slow, app absent)
 * just means no pet this launch. The dashboard must never be held up by it.
 */
// Logged once per distinct outcome so a 5s poll cannot spam the log, while a
// state change (or a newly-broken gateway) still shows up.
let lastMochiProbe = "";
function probeLog(outcome) {
  if (outcome === lastMochiProbe) return;
  lastMochiProbe = outcome;
  console.log("Mochi pet probe:", outcome);
}

// Cached because the reconcile loop runs every few seconds and
// /api/token/local MINTS A NEW SESSION TOKEN on every call — polling it would
// issue hundreds of tokens an hour and grow the revoked-nonce table for no
// reason. Cleared on any 401/403 so a genuinely expired token is re-minted.
let cachedGatewayToken = "";

async function gatewayToken() {
  if (cachedGatewayToken) return cachedGatewayToken;
  cachedGatewayToken = (await fetchLocalToken()) || "";
  return cachedGatewayToken;
}

/**
 * Which gateway's Mochi the pet shows.
 *
 * Mochi is a builtin, so its data belongs to the gateway it lives in — and a
 * user can have several gateways reachable at once (one local "self" plus
 * remotes forwarded in over ssh -L, which CORE manages via /api/instances). The
 * pet, by contrast, is a single machine-wide resource: one pet on the screen. So
 * `petInstance` (a Mochi setting on the LOCAL gateway) chooses whose Mochi it
 * shows, and this is where that choice becomes an origin.
 *
 * SELF IS THE FLOOR. The id is stored opaquely and deliberately not validated at
 * write time — instances come and go (TTL expiry, tunnel down) and a saved
 * choice must survive one being briefly away. Resolution is therefore where the
 * fallback happens, and it can always land on the local gateway, which is why
 * "no usable instance" is not a state the pet can get stuck in.
 *
 * Cached in a module variable because the accelerator handlers are synchronous;
 * the reconcile tick refreshes it every few seconds.
 */
let mochiPetBaseUrl = BACKEND_URL;
/**
 * First-load token for the pet's windows. EMPTY for `self`, where the dashboard's
 * same-origin cookie is already established — so the local path is byte-identical
 * to before this feature existed.
 */
let mochiPetToken = "";
/**
 * WHICH instance the pet is showing ("self" or an instance id).
 *
 * Tracked separately from the origin because local ports are RECYCLED (the
 * allocator hands out "the first free port at or above 7778"), so a dead instance
 * releasing its port and another instance taking it produces the same
 * `localhost:<port>` for a DIFFERENT gateway. Comparing origins alone would read
 * that as "no change" and leave the windows showing the old instance's content.
 */
let mochiPetInstanceId = "self";

/**
 * Ask core to connect one instance and hand back its token.
 *
 * ONE call does everything the shell needs. `POST /api/instances/{id}/connect` is
 * idempotent (an already-connected tunnel returns its cached token) AND it
 * validates that token over the live tunnel before handing it over, re-minting
 * when it has gone stale — which happens after a remote `kirocrew restart` or a
 * failed self-heal. That is why this is a single connect rather than "list to
 * find the port, then fetch a token": a token we did not validate produces a
 * server-rendered 403 on first load, and a top-level window that lands on a 403
 * page has no way to recover itself.
 *
 * Called on the LOCAL gateway with the LOCAL token: this is core's control plane,
 * so the shell never needs a remote secret. Core mints the remote token for us.
 *
 * @returns {Promise<{localPort: number, token: string}|null>} null = unusable
 */
function connectInstance(instanceId, token, { timeoutMs = 15000 } = {}) {
  return new Promise((resolve) => {
    const req = http.request(
      `${BACKEND_URL}/api/instances/${encodeURIComponent(instanceId)}/connect?token=${encodeURIComponent(token)}`,
      { method: "POST", timeout: timeoutMs },
      (res) => {
        let data = "";
        res.on("data", (c) => { data += c; });
        res.on("end", () => {
          // 403 (feature off) and 404 (the saved id is gone) are ANSWERS: this
          // instance really cannot host the pet. 502/503 (link or manager down)
          // and every transport failure are NON-answers.
          if (res.statusCode === 403 || res.statusCode === 404) {
            return resolve({ known: true, usable: false });
          }
          if (res.statusCode !== 200) return resolve({ known: false });
          try {
            const body = JSON.parse(data);
            const localPort = Number(body && body.local_port);
            if (body && body.state === "connected" && body.token && Number.isInteger(localPort) && localPort > 0) {
              return resolve({ known: true, usable: true, localPort, token: String(body.token) });
            }
            // A 200 that is not a connected tunnel is still a real answer.
            return resolve({ known: true, usable: false });
          } catch { /* fall through */ }
          resolve({ known: false });
        });
        res.on("error", () => resolve({ known: false }));
      },
    );
    req.on("error", () => resolve({ known: false }));
    req.on("timeout", () => { req.destroy(); resolve({ known: false }); });
    req.end();
  });
}

/**
 * Is Mochi enabled ON that instance?
 *
 * Part of "can X host the pet", NOT part of "should there be a pet" — those are
 * two different questions and they belong to two different machines. Whether a
 * pet exists at all is a LOCAL preference (see mochiEnabledState, which probes
 * self, alongside petInstance and the accelerators). Whether instance X can be
 * the one shown is a property OF X: tunnel up, port allocated, and its owner has
 * Mochi turned on.
 *
 * Treated exactly like a down tunnel: not usable => fall back to self. We never
 * enable anything remotely — the App Store toggle on that machine belongs to its
 * owner, and this only reads its current value. Without this check the remote
 * happily serves `/app-windows/mochi/pet.html` (those routes are gated on the file
 * existing, not on the app being enabled) and then 403s every single
 * `/api/apps/mochi/*` call — a pet that draws but is completely inert, with no
 * explanation anywhere.
 *
 * CACHED because the reconcile tick is 5s and this request crosses an SSH
 * tunnel. Enabled-ness only changes when a human flips it in an App Store, so a
 * minute of staleness is invisible; a round trip every 5s is not.
 */
const REMOTE_ENABLED_TTL_MS = 60_000;
const remoteEnabledCache = new Map();

async function remoteMochiEnabled(instanceId, localPort, token) {
  const cached = remoteEnabledCache.get(instanceId);
  if (cached && Date.now() - cached.at < REMOTE_ENABLED_TTL_MS) return cached.enabled;

  const enabled = await new Promise((resolve) => {
    const req = http.request(
      `http://localhost:${localPort}/api/apps?token=${encodeURIComponent(token)}`,
      { method: "GET", timeout: 8000 },
      (res) => {
        if (res.statusCode !== 200) { res.resume(); return resolve(null); }
        let data = "";
        res.on("data", (c) => { data += c; });
        res.on("end", () => {
          try {
            resolve(parseMochiEnabled(JSON.parse(data)));
          } catch { resolve(null); }
        });
        res.on("error", () => resolve(null));
      },
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.end();
  });

  // A non-answer is NOT cached and NOT read as disabled — see enabledOrTrust.
  if (enabled === null) return enabledOrTrust(enabled);
  remoteEnabledCache.set(instanceId, { at: Date.now(), enabled });
  return enabledOrTrust(enabled);
}

/**
 * Forget instances that are no longer configured.
 *
 * Without this the cache only ever grows, and `remoteEnabledSnapshot()` keeps
 * reporting ids the user has since deleted — a slow leak whose visible symptom is
 * a badge for something that no longer exists.
 */
function pruneRemoteEnabledCache(instances) {
  const alive = new Set((instances || []).map((i) => i && i.id).filter(Boolean));
  for (const id of [...remoteEnabledCache.keys()]) {
    if (!alive.has(id)) remoteEnabledCache.delete(id);
  }
}

/** Is there a still-fresh answer for this instance? Used to skip needless probes. */
function hasFreshEnabled(instanceId) {
  const entry = remoteEnabledCache.get(instanceId);
  return !!entry && Date.now() - entry.at < REMOTE_ENABLED_TTL_MS;
}

/** What the switcher shows per instance; also the shell's own resolvability input. */
function remoteEnabledSnapshot() {
  const out = {};
  for (const [id, entry] of remoteEnabledCache) out[id] = entry.enabled;
  return out;
}

/**
 * Learn "is Mochi on there" for every instance the user could actually pick.
 *
 * WHY ALL OF THEM, not just the selected one: Mochi ships defaultEnabled:false, so
 * a freshly added remote has Mochi OFF. That makes "reachable but Mochi is off" the
 * DEFAULT state of any remote the user has not explicitly enabled it on — the
 * first-run case, not an edge case. A badge that only appeared on the row you had
 * already chosen could never warn you before choosing, which was its whole point.
 *
 * WHY ONLY LIVE ONES: `connect` opens a tunnel. Probing every configured instance
 * would have the shell establishing SSH connections for instances the user never
 * picked, and would churn core's warm set (each warm instance is a full dashboard
 * SPA). Restricting to already-connected instances costs nothing new — the tunnel
 * is up and connect returns its cached token — and loses no coverage, because a
 * disconnected row is already shown as unpickable for that reason. Every row that
 * LOOKS pickable gets a real answer.
 *
 * Parallel, and every failure degrades to "unknown" (no badge) rather than blocking
 * the list. Answers land in the same 60s cache the resolver uses.
 */
async function probeAllLiveInstancesEnabled() {
  const localToken = await gatewayToken();
  if (!localToken) return remoteEnabledSnapshot();
  const listed = await fetchInstances(localToken);
  if (!listed.known) return remoteEnabledSnapshot();
  pruneRemoteEnabledCache(listed.instances);
  // Skip anything we already have a fresh answer for. This is what makes the
  // switcher safe to POLL: without it every refresh would re-issue a connect per
  // live instance, because the 60s cache sits on the enabled answer rather than on
  // the connect that fetches its token.
  const live = listed.instances.filter((i) => instanceIsLive(i) && !hasFreshEnabled(i.id));
  await Promise.all(
    live.map(async (inst) => {
      try {
        // Shorter connect timeout than the pet's resolve path: this one runs while
        // a user waits on a Settings pane, and a stale "connected" whose tunnel
        // actually died must not hang the whole list behind one row.
        const conn = await connectInstance(inst.id, localToken, { timeoutMs: 6000 });
        if (conn.known && conn.usable) {
          await remoteMochiEnabled(inst.id, conn.localPort, conn.token);
        }
      } catch {
        /* unknown = no badge; never let one row break the pane */
      }
    }),
  );
  return remoteEnabledSnapshot();
}

/**
 * Core's instance list, read from the LOCAL gateway (it owns the registry).
 *
 * READ-ONLY and side-effect free, which is why it comes first: `connect` opens a
 * tunnel, so it must never be the thing that discovers whether one is up.
 */
/**
 * Core's instance list, read from the LOCAL gateway (it owns the registry).
 *
 * READ-ONLY and side-effect free, which is why it comes first: `connect` opens a
 * tunnel, so it must never be the thing that discovers whether one is up.
 *
 * `state` mirrors the renderer's `InstancesView` discriminant so the switcher can
 * render the SAME four outcomes it renders on the same-origin path. Collapsing
 * 403 and `active:false` into "an empty ready list" costs the user the only
 * guidance they get: `disabled` says "enable multi-instance in Settings" and
 * `inactive` says "restart the gateway", and without them a user with the feature
 * off just sees "This computer" and no way forward.
 */
function fetchInstances(token) {
  return new Promise((resolve) => {
    const req = http.request(
      `${BACKEND_URL}/api/instances?token=${encodeURIComponent(token)}`,
      { method: "GET", timeout: 5000 },
      (res) => {
        // 403 is an ANSWER: instances.enabled is off, so there are genuinely no
        // remotes to point at. Every other non-200 is a NON-answer.
        if (res.statusCode === 403) {
          res.resume();
          return resolve({ known: true, state: "disabled", instances: [] });
        }
        if (res.statusCode !== 200) { res.resume(); return resolve({ known: false, state: "error" }); }
        let data = "";
        res.on("data", (c) => { data += c; });
        res.on("end", () => {
          try {
            const body = JSON.parse(data);
            const list = Array.isArray(body) ? body : body && body.instances;
            if (!Array.isArray(list)) return resolve({ known: false, state: "error" });
            // `active:false` = the registry is configured but the manager is not
            // running, i.e. "needs restart" — a distinct, actionable state.
            const state = body && body.active === false ? "inactive" : "ready";
            return resolve({ known: true, state, instances: list });
          } catch { resolve({ known: false, state: "error" }); }
        });
        res.on("error", () => resolve({ known: false, state: "error" }));
      },
    );
    req.on("error", () => resolve({ known: false, state: "error" }));
    req.on("timeout", () => { req.destroy(); resolve({ known: false, state: "error" }); });
    req.end();
  });
}

/**
 * Is this instance ALREADY carrying a live tunnel we can ride?
 *
 * The gate on ever calling `connect`. Mochi follows core's tunnels; it does not
 * open them. Attempting a connect on a down instance would mean the pet's 5s
 * reconcile silently establishing SSH connections the user never asked for — and
 * each attempt can block for as long as SSH takes to fail.
 */
function instanceIsLive(inst) {
  const localPort = inst ? Number(inst.local_port) : 0;
  return !!(
    inst &&
    inst.status &&
    inst.status.state === "connected" &&
    Number.isInteger(localPort) &&
    localPort > 0
  );
}

/**
 * Resolve `petInstance` to the origin, first-load token and IDENTITY the pet's
 * windows should use — or `{ keep: true }` meaning "we could not tell; change
 * nothing".
 *
 * THE KEEP OUTCOME EXISTS FOR THE SAME REASON `mochiEnabledState` is tri-state.
 * Falling back to self on a NON-answer looks harmless but is not: it flips the
 * resolved target, which makes `switched` true, which destroys and rebuilds every
 * Mochi window on self — and then does it a second time when the link recovers.
 * One 5s tick that timed out would cost the user their chat panel twice. A
 * definite answer (403 feature-off, 404 id-gone, a listed instance that is simply
 * not connected) still falls back, because that is a fact rather than a failure.
 *
 * IDENTITY, not just origin: local ports are recycled ("first free port at or
 * above 7778"), so instance A dying and instance B being connected can hand B the
 * same `localhost:<port>` A had. Comparing origins alone would then read as "no
 * change" and leave windows showing A's content under B's identity.
 */
async function resolveMochiTarget(choice) {
  const self = { baseUrl: BACKEND_URL, token: "", instanceId: SELF_INSTANCE };
  // The pointer comes from the SHELL's own store now, so there is no
  // "could not read the setting" case left to handle here — it is always
  // readable, including while the host gateway's Mochi is disabled, which is
  // precisely what lets a remote pet outlive a local disable.
  if (!choice || choice === SELF_INSTANCE) {
    mochiInstanceLog("showing this computer's Mochi");
    return self;
  }

  const localToken = await gatewayToken();
  if (!localToken) return { keep: true };

  // List FIRST. Only an already-live instance is offered a connect, so the pet
  // never brings a tunnel up on its own.
  const listed = await fetchInstances(localToken);
  if (!listed.known) {
    mochiInstanceLog("could not read the instance list — leaving Mochi where it is");
    return { keep: true };
  }
  pruneRemoteEnabledCache(listed.instances);

  const inst = listed.instances.find((i) => i && i.id === choice);
  if (!instanceIsLive(inst)) {
    // A real answer from core: it is listed-but-down, or no longer listed at all.
    mochiInstanceLog(`petInstance "${choice}" is not connected — showing this computer's Mochi`);
    return self;
  }

  const conn = await connectInstance(choice, localToken);
  if (!conn.known) {
    mochiInstanceLog(`petInstance "${choice}" did not answer — leaving Mochi where it is`);
    return { keep: true };
  }
  if (!conn.usable) {
    mochiInstanceLog(`petInstance "${choice}" is not usable — showing this computer's Mochi`);
    return self;
  }
  if (!(await remoteMochiEnabled(choice, conn.localPort, conn.token))) {
    mochiInstanceLog(`petInstance "${choice}" has Mochi turned off — showing this computer's Mochi`);
    return self;
  }
  mochiInstanceLog(`petInstance "${choice}" resolved to port ${conn.localPort}`);
  // localhost, not 127.0.0.1: the shell addresses every gateway this way, and the
  // auth cookie is named per-port (mc_token_<port>) precisely because browser
  // cookies are not isolated by port — so a consistent host keeps the local and
  // remote cookies from being confused for one another.
  return {
    baseUrl: `http://localhost:${conn.localPort}`,
    token: conn.token,
    instanceId: choice,
  };
}

/** Log resolution changes only — this runs every reconcile tick. */
let lastMochiInstanceLog = "";
function mochiInstanceLog(message) {
  if (message === lastMochiInstanceLog) return;
  lastMochiInstanceLog = message;
  glog(`mochi instance: ${message}`);
}

/**
 * TRI-STATE on purpose: "enabled" | "disabled" | "unknown".
 *
 * This used to return a bare boolean, so a transient failure — one 403 on a
 * token being re-minted, a 5s timeout, an unparseable body — was indistinguishable
 * from "the user turned Mochi off". The reconcile loop then ran its full teardown
 * (close the pet, hide the panel, close Settings, unregister the accelerators)
 * and rebuilt everything on the next tick. That is what read as the pet CRASHING
 * and restarting, and it also armed the panel's restore flag, after which every
 * later tick re-opened a panel the user had deliberately hidden.
 *
 * "unknown" now means DO NOTHING: whatever is on screen stays. A genuinely
 * disabled app still tears down, because the gateway answered and said so.
 */
async function mochiEnabledState() {
  const token = await gatewayToken();
  if (!token) { probeLog("no gateway token — cannot query /api/apps"); return "unknown"; }
  return new Promise((resolve) => {
    // `?token=` — NOT a cookie. The dashboard cookie is named
    // `mc_token_<browser-facing-port>` (token_auth.py::_cookie_port_from_host,
    // port-keyed so SSH-tunnelled instances don't collide), so a hand-built
    // `mc_token=` header silently fails auth. The query param is accepted on
    // the same line that reads the cookie, and needs no port knowledge.
    const req = http.request(
      `${BACKEND_URL}/api/apps?token=${encodeURIComponent(token)}`,
      { method: "GET", timeout: 5000 },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          // Drop a rejected token so the next tick mints a fresh one.
          if (res.statusCode === 401 || res.statusCode === 403) cachedGatewayToken = "";
          probeLog(`/api/apps returned HTTP ${res.statusCode}`);
          resolve("unknown");
          return;
        }
        let data = "";
        res.on("data", (c) => { data += c; });
        res.on("end", () => {
          try {
            const payload = JSON.parse(data);
            const apps = Array.isArray(payload) ? payload : payload.apps || [];
            const mochi = apps.find((a) => a && a.name === "mochi");
            if (!mochi) {
              // The gateway answered and Mochi is not installed — a real answer.
              probeLog(`mochi not among ${apps.length} installed apps`);
              resolve("disabled");
              return;
            }
            probeLog(mochi.enabled ? "mochi enabled — opening pet" : "mochi installed but disabled");
            resolve(mochi.enabled ? "enabled" : "disabled");
          } catch (err) {
            probeLog(`/api/apps response unparseable: ${err.message}`);
            resolve("unknown");
          }
        });
        res.on("error", () => resolve("unknown"));
      }
    );
    req.on("error", () => resolve("unknown"));
    req.on("timeout", () => { req.destroy(); resolve("unknown"); });
    req.end();
  });
}

/**
 * Reconcile the pet window against Mochi's enabled state, forever.
 *
 * A one-shot check at boot is not enough: enabling Mochi in the App Store has
 * to make the pet appear without restarting the shell (and disabling it has to
 * make the pet go away). The gateway does not broadcast app enable/disable over
 * the WebSocket today, so the shell polls.
 *
 * Polling is the deliberate trade: adding an `app_enabled` WS event would be
 * cleaner but means changing the apps framework, and this is one localhost
 * request every few seconds against a gateway in the same machine. Worth
 * revisiting if the framework ever grows that event.
 *
 * No state is tracked because both window operations are idempotent —
 * openPetWindow returns the existing window, closePetWindow no-ops when there
 * is none — so each tick can simply assert the desired end state.
 */
const MOCHI_PET_RECONCILE_MS = 5000;

/**
 * Mochi's settings object, or null on ANY failure (no token, non-200,
 * unparseable body).
 *
 * One fetch feeds every reconcile decision that needs stored state — the avatar
 * gate and the global accelerators — so a tick makes ONE request rather than one
 * per consumer.
 */
async function mochiSettings() {
  const token = await gatewayToken();
  if (!token) return null;
  return new Promise((resolve) => {
    const req = http.request(
      `${BACKEND_URL}/api/apps/mochi/settings?token=${encodeURIComponent(token)}`,
      { method: "GET", timeout: 5000 },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          if (res.statusCode === 401 || res.statusCode === 403) cachedGatewayToken = "";
          resolve(null);
          return;
        }
        let data = "";
        res.on("data", (c) => { data += c; });
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data);
            resolve(parsed && typeof parsed === "object" ? parsed : null);
          } catch {
            resolve(null);
          }
        });
      },
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
    req.end();
  });
}

/**
 * The user's chosen avatar, or null when they have not picked one yet.
 *
 * Null on any failure is the safe direction: a transient gateway hiccup shows
 * the picker rather than starting a pet whose avatar — and therefore personality
 * — was never chosen.
 */
function mochiAvatarOf(settings) {
  return settings && typeof settings.avatar === "string" ? settings.avatar : null;
}

/**
 * Bind the global accelerators, and rebind when the stored ones change.
 *
 * The reconcile loop is the fallback path (up to one tick of lag): the Settings
 * window applies a rebind immediately over `mochi-shortcuts:apply` so the user
 * gets instant feedback on whether the combination was available. This drift
 * check is what makes a change stick after a disable/enable cycle, and covers a
 * rebind made from another surface.
 */
function applyMochiShortcuts(desired) {
  const {
    registerMochiShortcuts,
    areMochiShortcutsRegistered,
    currentMochiShortcuts,
    MOCHI_SHORTCUT_ACTIONS,
    ACCELERATORS,
  } = require("./shortcuts");
  const live = currentMochiShortcuts();
  const target = {};
  for (const [action] of MOCHI_SHORTCUT_ACTIONS) {
    target[action] = (desired && desired[action]) ?? ACCELERATORS[action];
  }
  // An action the OS refused is absent from `live`, so this comparison also
  // retries a previously-taken key on the next tick — cheap, and it means
  // quitting the app that held the key eventually gives it back.
  const drifted = MOCHI_SHORTCUT_ACTIONS.some(
    ([action]) => live[action] !== (target[action] || undefined),
  );
  if (areMochiShortcutsRegistered() && !drifted) return null;
  return registerMochiShortcuts(
    {
      onToggleWindow: mochiToggleChatPanel,
      onHideAll: mochiToggleHideAll,
      onScreenCapture: mochiStartSnip,
    },
    target,
  );
}

/**
 * Global screen-capture shortcut.
 *
 * Capture runs in the CROP WINDOW's renderer, on KiroCrew's existing
 * getDisplayMedia path (`session.setDisplayMediaRequestHandler` is already
 * registered — see display-media.js, which also gates on the macOS Screen
 * Recording permission and surfaces guidance when it is denied). There is still
 * only one capture mechanism and one permission path.
 *
 * It used to run in the PANEL renderer, which also hosted the crop surface. That
 * put a full-screen frame inside a 320x470 window: the image scaled to ~288px
 * wide, so a pixel of drag moved ~13 source pixels and no useful region could be
 * selected. The crop surface needs a host the size of the screen — see
 * snipWindow.js.
 */
function mochiStartSnip() {
  try {
    const snip = require("./snipWindow");
    // Idempotent: the accelerator can fire again while a crop is in progress, and
    // a second full-screen surface would cover the first.
    if (snip.snipWindowIsOpen()) return;
    snip.openSnipWindow(mochiPetBaseUrl, mochiPetToken);
  } catch (err) {
    glog(`Mochi snip: could not start capture: ${err && err.message}`);
  }
}

/**
 * At most one reconcile at a time, for EVERY caller.
 *
 * The guard has to live here rather than in the interval's closure: the
 * apply-now IPC (a Settings save) calls reconcile directly, and that is precisely
 * the call most likely to coincide with a tick — both resolve `petInstance`, both
 * write mochiPetBaseUrl, and both can tear windows down. Two overlapping runs can
 * destroy and reopen the same window, or let the LOSER's origin win the variable.
 *
 * A tick JOINS an in-flight run instead of skipping outright: it is idempotent, so
 * joining gives the same answer and keeps the interval honest about completion.
 */
let reconcileInFlight = null;

function reconcileMochiOnce() {
  if (reconcileInFlight) return reconcileInFlight;
  reconcileInFlight = reconcileMochi().finally(() => {
    reconcileInFlight = null;
  });
  return reconcileInFlight;
}

/**
 * Reconcile with a run that STARTS after this call.
 *
 * Joining an in-flight run is not good enough for a just-saved setting: that run
 * may have read `petInstance` from the gateway BEFORE the save landed, so it would
 * faithfully re-apply the old value. Wait for it to finish, then run a fresh one.
 */
async function reconcileMochiAfterCurrent() {
  if (reconcileInFlight) {
    try {
      await reconcileInFlight;
    } catch {
      /* the in-flight run's own failure is not this caller's problem */
    }
  }
  return reconcileMochiOnce();
}

async function reconcileMochi() {
  const { openPetWindow, closePetWindow } = require("./petOverlays");
  const {
    closeAvatarWindowFromReconcile,
    setAvatarBaseUrl,
    resetAvatarChoiceGuard,
  } = require("./avatarWindow");
  const { hidePanelOnDisable, restorePanelOnEnable } = require("./panelWindow");
  const { closeSettingsWindow, setSettingsBaseUrl } = require("./settingsWindow");
  const { unregisterMochiShortcuts, areMochiShortcutsRegistered } = require("./shortcuts");

  // Keep the avatar module's gateway origin current every tick, so a user's
  // right-click > Avatars works even when the picker was never opened at
  // startup (avatar already set => the reconcile loop never passed a base url).
  setAvatarBaseUrl(mochiPetBaseUrl, mochiPetToken);
  // Same for Settings: its open channel is registered at module load, so the
  // origin is the only thing it still needs from the reconcile tick.
  setSettingsBaseUrl(mochiPetBaseUrl, mochiPetToken);

  const state = await mochiEnabledState();
  // Could not tell: leave every window exactly as it is. Tearing down on a
  // failed probe is what made the pet appear to crash every few seconds.
  if (state === "unknown") return;

  // ONE-SHOT migration of the per-machine prefs out of the host's Mochi
  // settings, so an existing choice is not reset by the upgrade that moves it.
  // Only while the host is ENABLED (the settings route 403s otherwise) and only
  // until it succeeds, so the steady state costs no request at all. Runs BEFORE
  // the resolve so a migrated pointer takes effect on this same tick.
  if (state === "enabled" && machineStore.get(MIGRATED_KEY) !== true) {
    migrateMachinePrefs(machineStore, await mochiSettings());
  }

  // RESOLVE BEFORE DECIDING. Every route the resolve needs — core's
  // /api/instances on the host, the remote's own /api/apps — sits outside the
  // host's Mochi gate, and the pointer now comes from the shell's store, so this
  // answer is available even while the host has Mochi switched off. Deciding on
  // teardown first and resolving second is exactly what made a local disable
  // take a remote pet with it.
  const target = await resolveMochiTarget(petInstanceOf(machineStore));
  // `keep` = we could not tell. Whatever is on screen stays, so the id that
  // matters for the teardown decision is the one already showing.
  const shownInstanceId = target.keep ? mochiPetInstanceId : target.instanceId;
  // On `keep` we do not know, and not-knowing must never destroy anything — the
  // same discipline as enabledState's "unknown". A definite resolve onto self
  // means the remote is gone, and `hostDisabledMeansTeardown` handles self.
  const shownStillUsable = target.keep ? true : target.instanceId !== SELF_INSTANCE;

  if (state === "disabled" && hostDisabledMeansTeardown(shownInstanceId, shownStillUsable)) {
    closePetWindow();
    // Hide the panel rather than orphan an opaque always-on-top rectangle over
    // the desktop; re-enable restores it if it was visible.
    hidePanelOnDisable();
    // Only close a reconcile-opened picker; a user-opened Avatars window lives.
    closeAvatarWindowFromReconcile();
    // Settings has no user-owned lifetime worth preserving across a disable, and
    // leaving an always-on-top form floating over the desktop for a disabled app
    // is the same orphan bug the panel had.
    closeSettingsWindow();
    // Re-enabling with no avatar set should ask again rather than silently
    // starting a pet the user never chose.
    resetAvatarChoiceGuard();
    // Drop the global shortcuts while disabled, and forget any hideAll toggle
    // state so a later enable starts from a clean (windows-shown) baseline.
    if (areMochiShortcutsRegistered()) unregisterMochiShortcuts();
    mochiWindowsHidden = false;
    // Re-arm the first-open chat panel for the next enable.
    mochiPanelAutoOpened = false;
    return;
  }

  // Past here the pet is alive: either the host's Mochi is on, or it is off and
  // the pet is being served by a remote that is still live and still has Mochi
  // enabled. Everything below addresses the SHOWN gateway, so both cases take
  // the identical path — a disabled host simply stops doing its own backend work
  // (its on_shutdown cancels the pollers, watchlist guard and stats), which is
  // what the user asked for by switching it off.

  // `keep` = we could not tell. Change NOTHING: falling back would flip the
  // target, and a flipped target rebuilds every window (twice — once now and
  // again when the link recovers). Same discipline as enabledState's "unknown".
  if (!target.keep) {
    // Compare the INSTANCE, not just the origin: recycled local ports mean two
    // different instances can present the same `localhost:<port>`.
    const switched = mochiPetInstanceId !== target.instanceId || mochiPetBaseUrl !== target.baseUrl;
    mochiPetBaseUrl = target.baseUrl;
    mochiPetToken = target.token;
    mochiPetInstanceId = target.instanceId;
    if (switched) {
      glog(`mochi instance: target changed — rebuilding Mochi's windows at ${target.baseUrl}`);
      closePetWindow();
      // Destroy, don't hide: a hidden panel is still a live window that the next
      // open would simply show() — still rendering the OLD instance's chat slot.
      // This variant remembers it was open, so it comes back on the new instance.
      require("./panelWindow").dropPanelForInstanceSwitch();
      closeSettingsWindow();
      closeAvatarWindowFromReconcile();
      // The rebuilt overlays come up VISIBLE, so a hideAll that was in effect is
      // now a lie: the pet is back on screen while the flag still says hidden, and
      // the next Cmd+Shift+H would take the "show" branch — the user would have to
      // press it twice to put the pet away again. Same reset the disable path does.
      mochiWindowsHidden = false;
      // New instance = a genuinely new "first open": re-arm the chat panel pop.
      mochiPanelAutoOpened = false;
    }
  }
  setAvatarBaseUrl(mochiPetBaseUrl, mochiPetToken);
  setSettingsBaseUrl(mochiPetBaseUrl, mochiPetToken);
  // The pet's menu actions go through one-shot IPC handlers, so the panel module
  // needs the current origin pushed to it rather than captured once.
  require("./panelWindow").setPanelTarget(mochiPetBaseUrl, mochiPetToken);

  // NO first-run gate. The original blocked the pet until the user picked an
  // avatar; here the backend defaults to Mochi Cat, so enabling the app from the
  // App Store gets you a companion immediately and the choice stays reversible
  // (pet right-click > Avatars, or the dashboard Appearance card). The avatar
  // window is now the Avatars gallery, opened on demand rather than at startup.
  closeAvatarWindowFromReconcile();
  openPetWindow(mochiPetBaseUrl, mochiPetToken);
  // Fully enabled again: bring the panel back if disable had hidden it.
  restorePanelOnEnable(mochiPetBaseUrl, mochiPetToken);
  // FIRST OPEN: on the first enabled tick of a session (fresh enable, or the pet
  // first shown on a new instance), pop the chat panel once so a new user sees
  // the companion's chat and the composer's rotating shortcut tips right away.
  // One-shot (re-armed on disable / instance switch) so we never reopen a panel
  // the user then closes; restorePanelOnEnable already covers the disable→enable
  // restore, and the isPanelWindowOpen() guard avoids a double-open.
  if (!mochiPanelAutoOpened) {
    mochiPanelAutoOpened = true;
    const panel = require("./panelWindow");
    if (!panel.isPanelWindowOpen()) {
      panel.openPanelWindow(mochiPetBaseUrl, mochiPetToken);
    }
  }
  // Bind (or rebind) the user's accelerators from the SHELL's store — one
  // keyboard is a property of this machine, not of whichever gateway the pet
  // happens to show, and holding them here is also what keeps them bound when
  // the host's Mochi is switched off. applyMochiShortcuts no-ops when they
  // already match, so the 5s loop does not unregister+re-register every tick —
  // which would briefly drop the key.
  applyMochiShortcuts(shortcutsOf(machineStore));
}

// ── Mochi global-shortcut handlers ─────────────────────────────────────────
// Live here (not in shortcuts.js) because they need the gateway origin and
// reach across both window modules — mirroring the original index.ts, which
// passed handlers into registerShortcuts.

/** CMD+SHIFT+M — show/hide the chat panel (the tip the composer advertises). */
function mochiToggleChatPanel() {
  require("./panelWindow").togglePanelWindow(mochiPetBaseUrl, mochiPetToken);
}

// hideAll toggle state. Unlike the original (a standalone app that used
// app.hide()/app.show() to hide its own windows), the builtin must NOT hide the
// host — app.hide() would take the dashboard with it — so it toggles Mochi's
// two windows directly. `panelWasVisibleBeforeHideAll` restores the panel to
// exactly its pre-hide visibility, matching the original's wasExpandedBeforeHide.
let mochiWindowsHidden = false;

/**
 * One-shot: has the chat panel been auto-opened for the CURRENT enabled session?
 *
 * Popping the panel the first time Mochi becomes enabled (or the pet first shows
 * on a fresh instance) lets a new user immediately see the companion's chat and
 * its composer's rotating shortcut tips. It fires ONCE per enabled session so it
 * never fights a user who then closes the panel; it is re-armed on disable and on
 * an instance switch (a genuinely new "first open").
 */
let mochiPanelAutoOpened = false;
let panelWasVisibleBeforeHideAll = false;

/** CMD+SHIFT+H — hide, then restore, all Mochi windows (pet + chat panel). */
function mochiToggleHideAll() {
  const pet = require("./petOverlays");
  const panel = require("./panelWindow");
  if (!mochiWindowsHidden) {
    panelWasVisibleBeforeHideAll = panel.hidePanelWindow();
    pet.hidePetWindow();
    mochiWindowsHidden = true;
  } else {
    pet.showPetWindow();
    // Only re-open the panel if it was visible when we hid — never surface a
    // panel the user had already put away.
    if (panelWasVisibleBeforeHideAll) panel.openPanelWindow(mochiPetBaseUrl, mochiPetToken);
    mochiWindowsHidden = false;
  }
}

function startMochiWatcher() {
  // Route pet/panel/shortcut lifecycle logging into gateway-launch.log via the
  // same helper the rest of the shell uses — a renderer crash and its forwarded
  // page errors are otherwise invisible in the packaged app.
  try { require("./panelWindow").setPanelLogger(glog); } catch { /* module shape changed */ }
  try { require("./shortcuts").setShortcutLogger(glog); } catch { /* module shape changed */ }

  /**
   * Which instances have Mochi turned on.
   *
   * The ONE thing the switcher cannot learn by itself: Settings is served by the
   * local gateway, and asking a remote whether its Mochi is enabled needs that
   * remote's token — which only the shell has, because core mints it through the
   * local control plane (`/api/instances/{id}/connect`). Everything else in the
   * switcher comes straight from core's `/api/instances` over plain HTTP.
   *
   * This DOES reach out (see probeAllLiveInstancesEnabled) — but only to instances
   * whose tunnel is already up, in parallel, behind a 60s cache, and only when a
   * user opens the pane. It never opens a tunnel. An instance absent from the
   * returned map simply has no answer, which the UI treats as "fine" rather than
   * as "off".
   */
  ipcMain.handle("mochi-instances:enabled-map", async () => {
    try {
      return await probeAllLiveInstancesEnabled();
    } catch (err) {
      glog(`mochi instance: enabled-map failed — ${err && err.message}`);
      return {};
    }
  });

  /**
   * Apply the CURRENT pointer now instead of on the next tick.
   *
   * Kept alongside `mochi-instances:set` for the surfaces that only need "act on
   * what is stored" — a Settings save that changed other things, for instance.
   * Runs the ordinary reconcile rather than a special switch path: it already
   * resolves, rebuilds on change, and is idempotent, so there is exactly one code
   * path for switching and no second one to drift.
   */
  ipcMain.handle("mochi-instances:apply-now", async () => {
    try {
      // A run that STARTS now: joining an in-flight tick could re-apply the value
      // that tick already read, from before the save landed.
      await reconcileMochiAfterCurrent();
      return { ok: true };
    } catch (err) {
      glog(`mochi instance: apply-now failed — ${err && err.message}`);
      return { ok: false };
    }
  });

  /**
   * Rebind immediately when Settings saves an accelerator, and answer with the
   * per-action result.
   *
   * `handle` (not `on`) on purpose: whether a combination is AVAILABLE is only
   * knowable at register() time, so the renderer has to get an answer back. The
   * alternative — save and hope — is how the app previously advertised ⌘⇧M
   * while nothing was registered at all. The reconcile loop's drift check is the
   * backstop; this exists so the user is not waiting up to 5s to learn their new
   * key is taken.
   */
  ipcMain.on("mochi-pet:hide-all", () => {
    mochiToggleHideAll();
  });

  // ── Crop window relay ─────────────────────────────────────────────────────
  //
  // The crop window and the chat panel are separate renderers, so the crop has
  // to pass through the main process. Only the CROPPED png travels — the full
  // frame stays in the crop renderer, where it was captured.
  ipcMain.on("mochi-snip:ready", () => {
    // The capture resolved, so the OS picker is gone and it is safe to cover the
    // screen. Showing earlier would hide the picker behind the surface.
    try {
      require("./snipWindow").showSnipWindow();
    } catch (err) {
      glog(`Mochi snip: could not show the crop surface: ${err && err.message}`);
    }
  });

  ipcMain.on("mochi-snip:result", (_e, base64) => {
    // Trust nothing from a renderer: a non-string here would be handed straight
    // to the panel and thrown at the composer's data: URL builder.
    if (typeof base64 !== "string" || base64 === "") return;
    try {
      const win = require("./panelWindow").openPanelWindow(mochiPetBaseUrl, mochiPetToken);
      if (!win || win.isDestroyed()) return;
      const wc = win.webContents;
      const send = () => {
        try {
          if (!win.isDestroyed() && !wc.isDestroyed()) wc.send("mochi-panel:snip-delivered", base64);
        } catch {
          /* panel torn down between open and send */
        }
      };
      // A crop taken while the panel had never been opened has to wait for the
      // first load, exactly like showPanelView's own first-open timing.
      if (wc.isLoading()) wc.once("did-finish-load", send);
      else send();
    } catch (err) {
      glog(`Mochi snip: could not deliver the crop: ${err && err.message}`);
    }
  });

  ipcMain.on("mochi-snip:close", () => {
    try {
      require("./snipWindow").closeSnipWindow();
    } catch (err) {
      glog(`Mochi snip: could not close the crop surface: ${err && err.message}`);
    }
  });

  /**
   * Hand a local IMAGE to the OS viewer, so a panel thumbnail can be seen full
   * size. The panel is 320px wide, so an in-page lightbox would not actually
   * enlarge anything -- Preview (or the platform equivalent) is the real answer,
   * and it costs no new window.
   *
   * Trust nothing from the renderer: the panel displays AGENT-authored markdown,
   * so a hostile `![x](/Users/me/.ssh/id_rsa)` must not become "open this in an
   * app". Hence: an image-extension allowlist, realpath (so a symlink cannot
   * point elsewhere after the check), and a regular-file requirement. Note this
   * channel only hands a path to the OS; READING bytes into the page still goes
   * through core's /api/file-raw, which keeps its own sensitive-path gate.
   */
  ipcMain.handle("mochi-pet:open-image", async (_e, filePath) => {
    if (typeof filePath !== "string" || filePath === "") return false;
    const fs = require("fs");
    const path = require("path");
    const { shell } = require("electron");
    const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]);
    try {
      // realpath BEFORE the extension test: a `.png` symlink to a key file must
      // be judged by what it actually resolves to.
      const real = fs.realpathSync(filePath);
      if (!IMAGE_EXTS.has(path.extname(real).toLowerCase())) return false;
      if (!fs.statSync(real).isFile()) return false;
      // Non-empty return value means the OS refused to open it.
      const err = await shell.openPath(real);
      return err === "";
    } catch (err) {
      glog(`Mochi open-image refused: ${err && err.message}`);
      return false;
    }
  });

  ipcMain.handle("mochi-shortcuts:apply", (_e, accelerators) => {
    // Trust nothing from the renderer: only the two known actions, only strings.
    const { MOCHI_SHORTCUT_ACTIONS } = require("./shortcuts");
    const desired = {};
    if (accelerators && typeof accelerators === "object") {
      for (const [action] of MOCHI_SHORTCUT_ACTIONS) {
        const value = accelerators[action];
        if (typeof value === "string") desired[action] = value;
      }
    }
    try {
      // BIND FIRST, then persist only what the OS actually accepted.
      //
      // Registration is the only way to learn whether a combination is free, and
      // storing a refused one would leave that action with no working key while
      // the store claims it has one — the user closes Settings and the accelerator
      // is simply dead. Keeping the previous value instead means the next drift
      // check rebinds something that works.
      //
      // Ordering is safe because this handler is SYNCHRONOUS: the 5s reconcile
      // tick cannot interleave between the bind and the write, so the "bound but
      // not persisted, then undone by the next tick" hazard does not arise here.
      const prev = shortcutsOf(machineStore);
      const result = applyMochiShortcuts({ ...prev, ...desired }) || {};
      // Tolerates `prev` being undefined, which is `shortcutsOf`'s documented
      // answer for "nothing stored yet" — see mergeAcceptedShortcuts.
      const keep = mergeAcceptedShortcuts(
        prev,
        desired,
        result,
        MOCHI_SHORTCUT_ACTIONS.map(([action]) => action),
      );
      // `byUser` records the intent, so a migration that lands later cannot
      // import the stale gateway copy over this rebind.
      setShortcutsIn(machineStore, keep, { byUser: true });
      return result;
    } catch (err) {
      glog(`Mochi shortcuts apply failed: ${err && err.message}`);
      return {};
    }
  });

  /**
   * The per-MACHINE prefs, read from the shell's own store.
   *
   * WHY THIS EXISTS AT ALL: every Mochi window is loaded FROM the gateway it
   * shows (pageUrl.js) and the renderer's API seam is same-origin, so a switcher
   * inside a pet that is showing a REMOTE would read and write that remote's
   * copy — while the shell reads this machine's. That mismatch is what made the
   * instance switch a one-way door. Routing both prefs through IPC gives every
   * window the same single copy regardless of who served it.
   */
  ipcMain.handle("mochi-machine:get", () => ({
    petInstance: petInstanceOf(machineStore),
    shortcuts: shortcutsOf(machineStore) || null,
  }));

  /**
   * Point the pet at an instance, and move it now rather than on the next tick.
   *
   * Write and apply in ONE call, deliberately: they were two (a same-origin
   * settings POST plus `apply-now`), and a renderer that did the first without
   * the second — or did them against different gateways — produced a stored
   * choice nothing acted on. One handler cannot half-happen.
   *
   * The id is stored OPAQUELY, not validated against the live list: instances
   * come and go, and a saved choice must survive one being briefly away.
   * Resolution is where the fallback to self lives.
   */
  ipcMain.handle("mochi-instances:set", async (_e, instanceId) => {
    try {
      // The STORE WRITE COMES FIRST, and it cannot be ordered the other way:
      // reconcile reads the store to learn which instance to build for, so
      // there is nothing to reconcile until the pointer is set.
      //
      // A reconcile that then throws therefore leaves a stored choice the 5s
      // loop keeps retrying — the switch is deferred, not lost. The renderer's
      // failure copy promises exactly that instead of claiming nothing was
      // saved, which would contradict the pet moving on a later tick. Rolling
      // the pointer back here would be the alternative, but it would discard a
      // deliberate pick over what is usually a transient link failure.
      const saved = setPetInstanceIn(machineStore, instanceId, { byUser: true });
      // A run that STARTS now: joining an in-flight tick could re-apply the
      // value that tick already read, from before this write landed.
      await reconcileMochiAfterCurrent();
      // REPORT WHERE THE PET ACTUALLY IS, not merely that reconcile did not
      // throw. Most ways a switch fails are silent, non-throwing returns:
      // reconcileMochi bails out entirely when the host's enabled-state probe
      // is unreadable, and resolveMochiTarget falls back to this computer when
      // the chosen instance is listed-but-down, no longer listed, unusable, or
      // has Mochi turned off. Returning ok:true on any of those closed Settings
      // over a pet that never moved.
      //
      // Compared against the shell's own record rather than a second predicate
      // over the same conditions — one source of truth cannot disagree with
      // itself. `mochiPetInstanceId` is SELF_INSTANCE exactly when the pet is on
      // this computer, so a 'self' pick compares equal without special-casing.
      //
      // Not covered: a host-disable teardown in the same pass that the pet was
      // already showing `saved` reports success although the windows are gone.
      // The next tick corrects it, and the reported value is still the truth
      // about the pointer.
      return { ok: mochiPetInstanceId === saved, petInstance: saved };
    } catch (err) {
      glog(`mochi instance: set failed — ${err && err.message}`);
      return { ok: false };
    }
  });

  /**
   * Core's instance list for THIS MACHINE's host gateway.
   *
   * The switcher used to fetch `/api/instances` same-origin, which meant that
   * once the pet was on a remote it listed the REMOTE's registry — a different
   * set of crews, or none at all if that gateway has the feature off, so the crew
   * the user wanted to return to could be missing from the list entirely. The
   * host owns the registry that the pointer's ids refer to, so the shell answers
   * from there.
   */
  ipcMain.handle("mochi-instances:list", async () => {
    try {
      const token = await gatewayToken();
      if (!token) return { known: false, state: "error", instances: [] };
      const listed = await fetchInstances(token);
      return {
        known: !!listed.known,
        state: listed.state || (listed.known ? "ready" : "error"),
        instances: listed.instances || [],
      };
    } catch (err) {
      glog(`mochi instance: list failed — ${err && err.message}`);
      return { known: false, state: "error", instances: [] };
    }
  });

  // Through the shared serializer, NOT reconcileMochi directly: a tick can make
  // requests through the SSH tunnel when petInstance names a remote, and those are
  // slower than the 5s interval on a bad link. See reconcileMochiOnce.
  const tick = () => {
    reconcileMochiOnce().catch((err) => {
      // Never let a transient gateway hiccup kill the watcher.
      console.warn("Mochi pet reconcile failed:", err?.message || err);
    });
  };
  tick();
  const timer = setInterval(tick, MOCHI_PET_RECONCILE_MS);
  app.on("before-quit", () => clearInterval(timer));
}

/**
 * Start the pet watcher. `backendUrl`/`fetchLocalToken`/`glog` are the shell's
 * own: the local gateway origin, the local-token fetcher (the same path the
 * dashboard window uses), and the gateway-launch logger.
 */
function initMochi(deps) {
  BACKEND_URL = deps.backendUrl;
  fetchLocalToken = deps.fetchLocalToken;
  glog = deps.glog;
  startMochiWatcher();
}

/**
 * Tear down every Mochi window and global shortcut at app quit.
 *
 * The pet overlay goes first: it is a frameless, skipTaskbar, non-focusable
 * window, so a stray one keeps the app alive with nothing the user can click
 * to quit it. Shortcuts are dropped so no stale registration outlives the app.
 */
function shutdownMochi() {
  try { require("./petOverlays").closePetWindow(); } catch { /* never opened */ }
  try { require("./panelWindow").closePanelWindow(); } catch { /* never opened */ }
  try { require("./avatarWindow").closeAvatarWindow(); } catch { /* never opened */ }
  try { require("./settingsWindow").closeSettingsWindow(); } catch { /* never opened */ }
  try { require("./shortcuts").unregisterMochiShortcuts(); } catch { /* never registered */ }
}

module.exports = { initMochi, shutdownMochi };
