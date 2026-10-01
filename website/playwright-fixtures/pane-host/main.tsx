/**
 * kc-46d84a E2E — the PRODUCTION-SOURCE Remote Crew pane host.
 *
 * This is the real dashboard's parent surface, reduced to exactly the incident
 * contract: it mounts the UNCHANGED production `InstancesViewport` inside the
 * SAME provider nesting `website/src/main.tsx` gives the app, and drives ONE
 * connected crew through the real page-load path (`InstancesViewport`'s own
 * auto-warm → `connectInstanceInto` → `api.openInstancePane` →
 * `parsePaneEndpoint` → `setWarm` → the relay iframe). Every relay-critical
 * decision — connect, iframe construction, the `window.name` envelope, endpoint
 * parsing, channel attribution, the `RelayStorageBank`, and lease renewal — is
 * production code imported verbatim; nothing here re-implements it.
 *
 * It replaces the hand-written `parent.html`/`parent.js` mirror the fixture used
 * before. The spec's observation hooks (`window.__paneTest`, `__validateForged`)
 * are installed here as PURE OBSERVERS over that production behaviour: the grant
 * and readiness are read from the real store, and every message decision is made
 * by the real `resolvePaneMessage` against a context built from the live store
 * endpoints and the live iframe `contentWindow`. The observer never decides
 * attribution itself, so a drift in the production authority surfaces as a spec
 * failure rather than being masked by a second copy of the rule.
 *
 * Approach note (kc-46d84a): mounting the ENTIRE built dashboard `<App/>` as the
 * parent is not viable in the fixture — it opens `/api/ws` against the hub,
 * runs the ui-prefs hydrate/reload dance, and fans out dozens of unrelated
 * gateway polls, none deterministic here. This dedicated entry is the sanctioned
 * second approach: it imports `InstancesViewport` and the production authorities
 * unchanged, so it is production frontend code, not a stand-in.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'

import { store } from '../../src/store'
import { queryClient } from '../../src/api/queryClient'
import InstancesViewport from '../../src/components/InstancesViewport'
import { setActiveId } from '../../src/store/instancesSlice'
import {
  initDashboardRuntime,
  resolveDashboardRuntime,
  relocateLoose,
  httpPath,
  type GatewayPath,
} from '../../src/lib/dashboardRuntime'
import {
  resolvePaneMode,
  resolvePaneMessage,
  type PaneEndpoint,
  type PaneMessageContext,
} from '../../src/lib/paneChannel'
import { PANE_CHANNEL_FIELD } from '../../src/lib/relayPaneBootstrap'

import { LanguageProvider } from '../../src/i18n/LanguageProvider'
import { ThemeProvider } from '../../src/hooks/useTheme'
import { UIModeProvider } from '../../src/hooks/useUIMode'
import { NavigationLeaveGuardProvider } from '../../src/components/NavigationLeaveGuard'
import { BrandingProvider } from '../../src/hooks/useBranding'
import { ProviderProvider } from '../../src/providers'
import { initI18n } from '../../src/i18n/all'

const INSTANCE_ID =
  document.querySelector('meta[name="kc-instance-id"]')?.getAttribute('content') || 'kc-46d84a'

// The parent is served at the hub root over HTTPS, so the production runtime
// resolves `direct` (its own gateway is the hub root) while `resolvePaneMode`
// resolves `same-origin-relay` (a published HTTPS parent frames capability
// panes) — exactly the production split. Resolve the runtime before render, as
// main.tsx does.
initDashboardRuntime()
initI18n()

const paneMode = resolvePaneMode(window.location)

// ── Observation state the spec reads. PURE observation of production. ─────────
interface RelocationProof {
  /** The live capability prefix the pane is served under (`/instance-pane/<cap>/`). */
  readonly prefix: string
  /** A representative app-scoped API path after the runtime helper relocates it,
   *  shaped like the request `scopedApi.ts` issues. Computed here by the helper,
   *  NOT by driving the scoped client — that call site is exercised by
   *  `src/test/appScopedApiRelayPane.test.tsx`. */
  readonly appApi: string
  /** A representative full-document route after the runtime helper relocates it,
   *  shaped like IncidentChat's navigation target. Computed here by the helper,
   *  NOT by driving the component — that call site is exercised by
   *  `src/apps/ops-mission-control/IncidentChat.paneNavigate.test.tsx`. */
  readonly nav: string
}
interface PaneTestState {
  grant: PaneEndpoint | null
  ready: boolean
  upwardAccepted: number
  upwardRejected: number
  acceptedTypes: string[]
  storageMsgs: number
  channelField: string
  errors: string[]
  /** The runtime helper's relocation of two representative same-dashboard paths
   *  against the LIVE capability the pane is riding. A real-browser cross-check
   *  that the production runtime prefixes same-dashboard URLs under the
   *  capability; the migrated call sites themselves are exercised by their own
   *  unit tests (see the field docs on RelocationProof). */
  relocation: RelocationProof | null
}
const state: PaneTestState = {
  grant: null,
  ready: false,
  upwardAccepted: 0,
  upwardRejected: 0,
  acceptedTypes: [],
  storageMsgs: 0,
  channelField: PANE_CHANNEL_FIELD,
  errors: [],
  relocation: null,
}

// Two representative same-dashboard paths: one shaped like the app SDK's
// permission-fenced request, one like a full-document route. This fixture
// relocates them with the runtime helper directly (below) as a browser
// cross-check; it does NOT drive the scoped client or IncidentChat, whose call
// sites are covered by their own unit tests.
const APP_SCOPED_API_PATH = '/api/apps/aws-control/accounts' as GatewayPath
const FULL_DOC_NAV_PATH = '/chat' as GatewayPath

/** Relocate the two representative paths with the PRODUCTION runtime resolver for
 *  the pane's LIVE capability, using the same `resolveDashboardRuntime` +
 *  `relocateLoose`/`httpPath` the relayed pane runs. This is a real-browser
 *  cross-check that the runtime helper prefixes same-dashboard URLs under the
 *  live capability and never leaves them at the hub root. It is NOT a test of the
 *  scoped-client or IncidentChat call sites, which mount the real provider and
 *  component in `src/test/appScopedApiRelayPane.test.tsx` and
 *  `src/apps/ops-mission-control/IncidentChat.paneNavigate.test.tsx`. */
function relocationFor(documentPath: string): RelocationProof {
  const runtime = resolveDashboardRuntime({ pathname: documentPath })
  return {
    prefix: documentPath,
    appApi: relocateLoose(runtime, APP_SCOPED_API_PATH),
    nav: httpPath(runtime, FULL_DOC_NAV_PATH),
  }
}
;(window as unknown as { __paneTest: PaneTestState }).__paneTest = state

/** The relay iframe the production InstancesViewport mounted (no test id set). */
function paneIframe(): HTMLIFrameElement | null {
  return document.querySelector<HTMLIFrameElement>('iframe[src^="/instance-pane/"]')
}

/**
 * The attribution context, built LIVE from the real store endpoints and the
 * real iframe `contentWindow` — the same inputs InstancesViewport feeds
 * `resolvePaneMessage`. Reading it fresh per message tracks a lease rotation
 * (new channel + new frame) with no bookkeeping of our own.
 */
function paneCtx(): PaneMessageContext {
  const warmNow = store.getState().instances.warm
  const endpoints = new Map<string, PaneEndpoint>(Object.entries(warmNow) as [string, PaneEndpoint][])
  const frames = new Map<string, Window>()
  const el = paneIframe()
  if (el?.contentWindow) frames.set(INSTANCE_ID, el.contentWindow)
  return { endpoints, frames }
}

// Track the current relay grant + readiness straight from the store. `setWarm`
// on a lease renewal writes a NEW endpoint (fresh documentPath + channel), so
// `state.grant` follows the rotation the spec asserts.
store.subscribe(() => {
  const s = store.getState().instances
  const w = s.warm[INSTANCE_ID] as PaneEndpoint | undefined
  if (w && w.kind === 'same-origin-relay') {
    state.grant = w
    // Re-run the helper cross-check against the live capability, so a lease
    // rotation (new documentPath) re-checks relocation against the fresh prefix.
    state.relocation = relocationFor(w.documentPath)
  }
  state.ready = !!s.ready[INSTANCE_ID]
})

// Observe every child→parent message through the REAL attribution authority.
// This runs ALONGSIDE InstancesViewport's own listener (which does the real
// work); here we only count what production attributed, so the spec's
// accepted/rejected/storage figures are the production decision, not ours.
window.addEventListener('message', (e) => {
  const data = e.data
  const id = resolvePaneMessage({ source: e.source, origin: e.origin, data }, paneMode, paneCtx())
  if (id !== null) {
    state.upwardAccepted++
    const t = (data && typeof data === 'object' && (data as { type?: unknown }).type) || '(none)'
    const type = String(t)
    if (!state.acceptedTypes.includes(type)) state.acceptedTypes.push(type)
    if (type === 'mc-relay-storage') state.storageMsgs++
    return
  }
  // Count a rejection only for a message that LOOKS like a pane message (carries
  // our channel field) but failed attribution — ignore unrelated window chatter.
  if (data && typeof data === 'object' && PANE_CHANNEL_FIELD in (data as Record<string, unknown>)) {
    state.upwardRejected++
  }
})

// Drive the REAL `resolvePaneMessage` with forged inputs so the spec can prove
// rejection of a stale/wrong/absent channel and a foreign frame — attribution
// by the production function, not a mirror.
;(window as unknown as { __validateForged: (kind: string) => string | null }).__validateForged = (
  kind: string,
) => {
  const ctx = paneCtx()
  const ch = state.grant?.channel ?? ''
  const el = paneIframe()
  const frame: Window | null = el?.contentWindow ?? null
  const run = (data: unknown, source: Window | null) =>
    resolvePaneMessage({ source, origin: 'null', data }, paneMode, ctx)
  if (kind === 'good') return run({ type: 'probe', [PANE_CHANNEL_FIELD]: ch }, frame)
  if (kind === 'wrong-channel') return run({ type: 'probe', [PANE_CHANNEL_FIELD]: ch + 'X' }, frame)
  if (kind === 'no-channel') return run({ type: 'probe' }, frame)
  if (kind === 'wrong-frame') return run({ type: 'probe', [PANE_CHANNEL_FIELD]: ch }, window)
  return 'unknown-kind'
}

// E2E-only, gated on `?delayBootstrapMs=N`: delay the AUTHENTICATED port
// handshake to prove the fail-closed TIMING fix in a real browser — a reply
// arriving well AFTER the old two-second fallback window still boots the pane
// with the real storage bank and channel, never an empty-channel release and the
// reload loop that followed. This is FIXTURE code and does NOT touch production
// InstancesViewport: it intercepts the child's port-carrying bootstrap request in
// the CAPTURE phase (before production's bubble-phase listener), holds it, and
// re-dispatches it to production after the delay. The child's port-less retries
// pass straight through (production simply has no bound port to answer yet), and
// the re-dispatched event carries the same port so production binds and replies.
const _delayMs = Number(new URLSearchParams(window.location.search).get('delayBootstrapMs') || 0)
if (_delayMs > 0) {
  const held = new WeakSet<MessagePort>()
  window.addEventListener(
    'message',
    (e: MessageEvent) => {
      const port = e.ports && e.ports[0]
      if (!port || (e.data as { type?: unknown })?.type !== 'mc-relay-bootstrap-request') return
      if (held.has(port)) return // our own re-dispatch — let production handle it
      held.add(port)
      e.stopImmediatePropagation() // block production's listener for the original
      window.setTimeout(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            data: e.data,
            origin: e.origin,
            source: e.source as Window,
            ports: [port],
          }),
        )
      }, _delayMs)
    },
    true, // capture
  )
}

// Make this crew the active pane. Its connection is warmed by InstancesViewport's
// own auto-warm (the real page-load path): the instances poll reports it
// connected, auto-warm fires `connectInstanceInto('auto-warm', {onlyIfConnected})`,
// and the relay pane mounts. We do NOT connect it by hand, so exactly one
// `openInstancePane` runs and the grant/channel the spec captures is the one the
// iframe rides.
store.dispatch(setActiveId(INSTANCE_ID))

const tree = (
  <QueryClientProvider client={queryClient}>
    <Provider store={store}>
      <LanguageProvider>
        <ThemeProvider>
          <UIModeProvider>
            <NavigationLeaveGuardProvider>
              <BrowserRouter>
                <BrandingProvider>
                  <ProviderProvider>
                    <InstancesViewport />
                  </ProviderProvider>
                </BrandingProvider>
              </BrowserRouter>
            </NavigationLeaveGuardProvider>
          </UIModeProvider>
        </ThemeProvider>
      </LanguageProvider>
    </Provider>
  </QueryClientProvider>
)

createRoot(document.getElementById('root')!).render(tree)
