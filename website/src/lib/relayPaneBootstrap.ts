/**
 * The relay-pane bootstrap: the versioned `window.name` envelope the parent
 * seeds, and the child boot step that consumes it before any app module runs.
 *
 * ## The problem it solves
 *
 * A relay pane is an opaque-origin iframe (sandboxed without
 * `allow-same-origin`), so `window.localStorage` / `window.sessionStorage`
 * throw `SecurityError` on access, and the SPA reads storage during module
 * evaluation. The child therefore needs its storage shims installed BEFORE the
 * app bundle evaluates, and it needs three facts the URL cannot safely carry:
 * the postMessage `channel` the parent will authenticate it by, the parent's
 * own origin (its opaque origin is `null`, so it cannot address the parent
 * otherwise), and an initial storage snapshot.
 *
 * `window.name` carries all three. It is readable synchronously at the very
 * first line of script, survives the document swap the navigation performs, and
 * — unlike the URL — never appears in the address bar, history, referrer, or
 * server logs, so the channel and snapshot stay out of browser-visible state.
 * The child parses it, installs the shims, and CLEARS `window.name` immediately
 * so the secret does not linger or leak to app code or across a later
 * navigation.
 *
 * ## Wiring (integration)
 *
 * `bootstrapRelayPane(window)` must run before the app bundle reads storage. In
 * a relay pane that means an inline pre-module `<script>` (injected by the relay
 * HTML rewrite) or the first statement of the entry module. In a direct (root)
 * dashboard `window.name` holds no envelope, so it returns `null` and native
 * storage is left completely untouched — direct mode is byte-identical.
 */
import {
  DEFAULT_RELAY_STORAGE_CAPS,
  createRelayStorage,
  type RelayStorage,
  type RelayStorageCaps,
  type RelayStorageMutation,
} from './relayStorage'

/** Envelope schema version. A parent and child that disagree fail closed. */
export const RELAY_ENVELOPE_VERSION = 1 as const

/**
 * The reserved discriminant key. `window.name` is also used by popouts, so the
 * parse must reject anything that is not exactly our tagged envelope.
 */
const ENVELOPE_TAG = '__kcRelayPane'

/**
 * The reserved message field every relay postMessage (both directions) carries,
 * stamped with the pane's random channel. The receiver binds on it together
 * with the exact `contentWindow`; the opaque frame's `event.origin` is `null`
 * and is never used as authentication. One source of the field name, imported
 * by the parent messaging layer (`paneChannel.ts`), the embedded-pane sender
 * (`embeddedParent.ts`), and the child bootstrap alike.
 */
export const PANE_CHANNEL_FIELD = 'mcPaneChannel' as const

/** postMessage `type` for a child→parent relay-storage mutation. */
export const RELAY_STORAGE_MESSAGE = 'mc-relay-storage' as const

/** postMessage `type` for a parent→child authoritative storage update. */
export const RELAY_STORAGE_UPDATE = 'mc-relay-storage-update' as const

/**
 * postMessage `type` for a child→parent SUBSEQUENT-DOCUMENT bootstrap request.
 *
 * `window.name` carries the envelope only into the FIRST document; it is cleared
 * on boot so the channel/snapshot never linger. A full-page navigation within
 * the same pane (a link, a hard SPA navigation, a reload after the parent
 * emptied the frame `name`) therefore lands on a fresh document with an empty
 * `window.name` and no envelope. Rather than render blank (opaque-origin storage
 * throws on access), that document derives its own capability prefix from
 * `location.pathname` and asks the parent to reseed. The request carries NO
 * channel — the new document does not have it yet — so the parent binds it on
 * the exact sender frame AND the capability documentPath it issued, never on the
 * opaque `event.origin`. See `resolvePaneBootstrapRequest` in `paneChannel.ts`.
 */
export const RELAY_BOOTSTRAP_REQUEST = 'mc-relay-bootstrap-request' as const

/**
 * postMessage `type` for the parent→child reply to {@link RELAY_BOOTSTRAP_REQUEST}.
 *
 * The subsequent document derives its own capability prefix (the first
 * `/instance-pane/<cap>/` segments of `location.pathname`) and sends it as the
 * request `documentPath`; the parent answers only when it exactly equals the
 * `documentPath` it issued for the matched frame (`resolvePaneBootstrapRequest`).
 * The derivation itself runs in the hub-injected pre-module bootstrap
 * (`instance_pane_relay._RELAY_PANE_BOOTSTRAP_SCRIPT`), which is the pane's actual
 * runtime; the app-bundle copy would run too late to gate the first module.
 */
export const RELAY_BOOTSTRAP_REPLY = 'mc-relay-bootstrap-reply' as const

/** The two Web Storage areas the pane mirrors. */
export type RelayStorageArea = 'local' | 'session'

/** The versioned bootstrap envelope the parent seeds into `window.name`. */
export interface RelayBootstrapEnvelope {
  readonly v: typeof RELAY_ENVELOPE_VERSION
  /** The random per-pane channel the parent authenticates this frame's messages by. */
  readonly channel: string
  /** The parent's own origin, so the opaque child can address it. */
  readonly parentOrigin: string
  /** The pane-relay wire protocol (pinned to 1). */
  readonly protocol: number
  /** The initial storage snapshot for each area. */
  readonly storage: { readonly local: Record<string, string>; readonly session: Record<string, string> }
}

/** The context the child boot step resolves once the port handshake completes. */
export interface RelayPaneContext {
  readonly channel: string
  readonly parentOrigin: string
  readonly protocol: number
  readonly localStorage: RelayStorage
  readonly sessionStorage: RelayStorage
}

/**
 * The handle {@link bootstrapRelayPane} returns for a relay document. The shims
 * are installed synchronously; `context()` is `null` until the authenticated
 * port handshake completes (and stays `null` forever on a fail-closed document —
 * an off-capability path, or a parent that never replies). `cancel()` stops the
 * bounded retry (teardown / tests).
 */
export interface RelayPaneBootstrap {
  readonly localStorage: RelayStorage
  readonly sessionStorage: RelayStorage
  context(): RelayPaneContext | null
  cancel(): void
}

/** Retry cadence + ceiling for the bootstrap port handshake (mirrors the inline
 *  Python bootstrap): re-send the request+port every {@link RELAY_HANDSHAKE_RETRY_MS}
 *  until {@link RELAY_HANDSHAKE_MAX_MS} of wall clock, then give up WITHOUT
 *  releasing the app (the parent's readiness watchdog owns recovery). */
export const RELAY_HANDSHAKE_RETRY_MS = 2000
export const RELAY_HANDSHAKE_MAX_MS = 15000

/** The capability prefix a relay document's path is under, e.g.
 *  `/instance-pane/<cap>/…`. The child derives its documentPath from this. */
const RELAY_DOC_PATH_RE = /^(\/instance-pane\/[^/?#]+\/)/

/** A per-document nonce (hex), pairing a bootstrap reply to its request. */
function _bootstrapNonce(win: Window): string {
  try {
    const arr = new Uint8Array(16)
    ;(win.crypto || (win as unknown as { msCrypto?: Crypto }).msCrypto)!.getRandomValues(arr)
    let s = ''
    for (let i = 0; i < arr.length; i++) s += ('0' + arr[i].toString(16)).slice(-2)
    return s
  } catch {
    return String(Math.random()) + '.' + String(Date.now())
  }
}

/** Serialize an envelope for `window.name`. */
export function buildBootstrapEnvelope(env: RelayBootstrapEnvelope): string {
  return JSON.stringify({
    [ENVELOPE_TAG]: RELAY_ENVELOPE_VERSION,
    v: env.v,
    channel: env.channel,
    parentOrigin: env.parentOrigin,
    protocol: env.protocol,
    storage: {
      local: env.storage.local,
      session: env.storage.session,
    },
  })
}

function _isStringRecord(x: unknown): x is Record<string, string> {
  if (!x || typeof x !== 'object' || Array.isArray(x)) return false
  for (const v of Object.values(x)) if (typeof v !== 'string') return false
  return true
}

/** Narrow an `unknown` to an indexable object, so field reads need no cast. */
function _isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null
}

/**
 * Parse a `window.name` value into an envelope, or `null` if it is not exactly a
 * current-version relay-pane envelope. Strict on every field so a popout name,
 * empty string, garbage, wrong version, or wrong shape all fall through to
 * native (direct) behaviour rather than half-initialising a relay.
 */
export function parseBootstrapEnvelope(name: string): RelayBootstrapEnvelope | null {
  if (!name) return null
  let raw: unknown
  try {
    raw = JSON.parse(name)
  } catch {
    return null
  }
  if (!_isRecord(raw)) return null
  const o = raw
  if (o[ENVELOPE_TAG] !== RELAY_ENVELOPE_VERSION) return null
  if (o.v !== RELAY_ENVELOPE_VERSION) return null
  if (typeof o.channel !== 'string' || !o.channel) return null
  if (typeof o.parentOrigin !== 'string' || !o.parentOrigin) return null
  if (typeof o.protocol !== 'number') return null
  const storage = o.storage
  if (!_isRecord(storage)) return null
  const s = storage
  if (!_isStringRecord(s.local) || !_isStringRecord(s.session)) return null
  return {
    v: RELAY_ENVELOPE_VERSION,
    channel: o.channel,
    parentOrigin: o.parentOrigin,
    protocol: o.protocol,
    storage: { local: s.local, session: s.session },
  }
}

/**
 * The child boot step, unified for the first load and every subsequent
 * navigation and mirroring the inline Python bootstrap
 * (`instance_pane_relay._RELAY_PANE_BOOTSTRAP_SCRIPT`).
 *
 * A relay document (its path under `/instance-pane/<cap>/`, or a first load
 * carrying a `window.name` envelope) installs synchronous storage shims — so the
 * opaque frame's storage never throws — pre-seeds them from a first-load envelope
 * for a warm first paint, and CLEARS `window.name` immediately so the seed never
 * lingers in browser-visible state. It then runs an authenticated port handshake:
 * ONE `MessageChannel` retained for the document's life — the document keeps
 * port1 (its downstream inbox) and TRANSFERS port2 to `window.parent` on the
 * FIRST ask, with the capability documentPath + a per-document nonce; bounded
 * retries re-ask WITHOUT a port (it cannot be transferred twice), so a slow
 * parent re-replies on the port it already holds rather than thrashing channels.
 * On the parent's reply over that port it adopts the channel + parent origin,
 * RESEEDS the shims from
 * the authoritative bank, publishes `window.__kcRelayPaneContext`, bridges the
 * port's downward messages into this document's window listeners, resolves the
 * context, and calls `onRelease` — exactly once. The port is entangled with THIS
 * document, so a successor document in the same iframe receives nothing.
 *
 * The channel and durable bank arrive ONLY over the port — never `window.name`,
 * which is a storage accelerator alone. A handshake that never completes leaves
 * the context `null` and `onRelease` uncalled (fail closed): the app is never
 * released with an empty channel and the empty-storage reload loop that follows.
 *
 * Returns `null` with `window.name` untouched when this is NOT a relay document
 * (a direct-loopback pane or the top-level dashboard) — native storage is never
 * replaced there.
 *
 * @param win        the window to bootstrap (injected so it is unit-testable)
 * @param onRelease  called once when the handshake authenticates (the runtime
 *                   releases the gated entry module here)
 * @param caps       storage bounds (defaults to the shared conservative caps)
 */
export function bootstrapRelayPane(
  win: Window & typeof globalThis,
  onRelease: () => void = () => {},
  caps: RelayStorageCaps = DEFAULT_RELAY_STORAGE_CAPS,
): RelayPaneBootstrap | null {
  const env = parseBootstrapEnvelope(win.name)
  const capMatch = RELAY_DOC_PATH_RE.exec(win.location?.pathname ?? '')
  // Not a relay document: no first-load envelope AND not under a capability path.
  // Leave native storage untouched — direct mode / the top-level dashboard.
  if (!env && !capMatch) return null

  // Clear the first-load envelope out of browser-visible state before any app
  // code runs; the channel/bank come over the port, so nothing is lost.
  if (env) {
    try {
      win.name = ''
    } catch {
      /* some engines guard window.name; the shims below still install */
    }
  }

  // The authoritative channel/origin the shims report under and the downstream
  // listener validates by. Empty until the port reply; no app code runs before
  // release, so the sink is never exercised while it is empty.
  const ctxHolder = { channel: '', parentOrigin: '', protocol: 1 }
  const sink = (area: RelayStorageArea) => (mutation: RelayStorageMutation) => {
    if (!ctxHolder.channel) return
    try {
      win.parent.postMessage(
        { type: RELAY_STORAGE_MESSAGE, area, mutation, [PANE_CHANNEL_FIELD]: ctxHolder.channel },
        ctxHolder.parentOrigin,
      )
    } catch {
      /* parent unreachable / mid-navigation — the value is already applied locally */
    }
  }
  const localStorage = createRelayStorage({}, caps, sink('local'))
  const sessionStorage = createRelayStorage({}, caps, sink('session'))
  const define = (key: 'localStorage' | 'sessionStorage', value: RelayStorage) => {
    Object.defineProperty(win, key, { configurable: true, enumerable: true, value })
  }
  define('localStorage', localStorage)
  define('sessionStorage', sessionStorage)
  // The downstream (parent→child) storage listener, fed by the port bridge below
  // (source=win.parent). A document that never authenticated has no port and so
  // is never bridged anything here.
  win.addEventListener('message', (ev: MessageEvent) => {
    if (ev.source !== win.parent) return
    const m = ev.data as { type?: unknown; area?: unknown; mutation?: unknown; [k: string]: unknown }
    if (!m || typeof m !== 'object' || m.type !== RELAY_STORAGE_UPDATE) return
    if (m[PANE_CHANNEL_FIELD] !== ctxHolder.channel) return
    const s = m.area === 'session' ? sessionStorage : m.area === 'local' ? localStorage : null
    if (s && m.mutation && typeof m.mutation === 'object') s.applyDownstream(m.mutation as RelayStorageMutation)
  })
  // First-load storage pre-seed (superseded by the port reply bank).
  if (env) {
    localStorage.reseed(env.storage.local)
    sessionStorage.reseed(env.storage.session)
  }

  let ctx: RelayPaneContext | null = null
  let settled = false
  const timers: ReturnType<typeof setTimeout>[] = []
  const cancel = () => {
    for (const t of timers) clearTimeout(t)
    timers.length = 0
  }
  const handle: RelayPaneBootstrap = { localStorage, sessionStorage, context: () => ctx, cancel }

  // Off-capability / malformed path: fail CLOSED. Shims are installed (a stray
  // access won't throw) but the app is never released with an empty channel and
  // no context resolves.
  if (!capMatch) return handle
  const docPath = capMatch[1]

  const finalize = (
    channel: string,
    parentOrigin: string,
    protocol: number,
    storage: { local: Record<string, string>; session: Record<string, string> } | null,
    port: MessagePort,
  ) => {
    if (settled) return
    settled = true
    cancel()
    ctxHolder.channel = channel
    ctxHolder.parentOrigin = parentOrigin
    ctxHolder.protocol = protocol
    if (storage) {
      localStorage.reseed(storage.local)
      sessionStorage.reseed(storage.session)
    }
    ;(win as unknown as { __kcRelayPaneContext?: unknown }).__kcRelayPaneContext = {
      channel,
      parentOrigin,
      protocol,
    }
    port.onmessage = (ev: MessageEvent) => {
      try {
        win.dispatchEvent(new MessageEvent('message', { data: ev.data, source: win.parent, origin: parentOrigin }))
      } catch {
        /* engine without a settable MessageEvent.source — the bridge no-ops */
      }
    }
    try {
      port.start()
    } catch {
      /* already started */
    }
    ctx = { channel, parentOrigin, protocol, localStorage, sessionStorage }
    onRelease()
  }

  // ONE MessageChannel retained for the document's life: the child keeps port1
  // (its downstream inbox) and TRANSFERS port2 to the parent on the FIRST ask.
  // Bounded retries re-ask WITHOUT a port (it cannot be transferred twice), so a
  // parent slow to reply re-replies on the port it already holds — no port thrash,
  // and the reply always lands on the one inbox this document is listening on.
  const mc = new MessageChannel()
  const nonce = _bootstrapNonce(win)
  mc.port1.onmessage = (ev: MessageEvent) => {
    if (settled) return
    const r = ev.data as {
      type?: unknown
      nonce?: unknown
      channel?: unknown
      parentOrigin?: unknown
      protocol?: unknown
      storage?: unknown
    }
    if (!r || typeof r !== 'object' || r.type !== RELAY_BOOTSTRAP_REPLY) return
    if (r.nonce !== nonce) return
    if (typeof r.channel !== 'string' || !r.channel) return
    if (typeof r.parentOrigin !== 'string' || !r.parentOrigin) return
    if (typeof r.protocol !== 'number') return
    const storage =
      r.storage && typeof r.storage === 'object'
        ? (r.storage as { local: Record<string, string>; session: Record<string, string> })
        : null
    finalize(r.channel, r.parentOrigin, r.protocol, storage, mc.port1)
  }
  try {
    mc.port1.start()
  } catch {
    /* already started */
  }
  const ask = (transfer?: Transferable[]) => {
    try {
      win.parent.postMessage(
        { type: RELAY_BOOTSTRAP_REQUEST, documentPath: docPath, nonce, v: RELAY_ENVELOPE_VERSION },
        '*',
        transfer,
      )
    } catch {
      /* parent unreachable this tick — a bounded retry re-asks */
    }
  }
  ask([mc.port2]) // first ask transfers the port
  let elapsed = 0
  const tick = () => {
    const t = setTimeout(() => {
      if (settled) return
      elapsed += RELAY_HANDSHAKE_RETRY_MS
      ask() // re-ask without a port; the parent re-replies on the bound one
      if (elapsed < RELAY_HANDSHAKE_MAX_MS) tick()
    }, RELAY_HANDSHAKE_RETRY_MS)
    timers.push(t)
  }
  tick()
  return handle
}
