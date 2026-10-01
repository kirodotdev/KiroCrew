/**
 * The R3 pane messaging + endpoint authority.
 *
 * One module decides everything about how a Remote Crew pane is addressed and
 * how its `postMessage` traffic is bound, across the two transports the owner's
 * browser can reach the hub on:
 *
 *   - **direct-loopback** — plain http on a loopback host (desktop / localhost).
 *     A pane is `http://<parent-host>:<forward-port>/?token=…`; the exact
 *     loopback origin both addresses a downward post and validates an inbound
 *     one. Byte-identical to the pre-R3 `paneEmbedding`/`tunnelOrigin` behaviour.
 *   - **same-origin-relay** — a published HTTPS parent whose only exposed origin
 *     is the hub's. The pane is a SANDBOXED, opaque-origin iframe served by the
 *     hub at a capability `documentPath` (`/instance-pane/<cap>/`); no remote
 *     token and no loopback port ever enter its URL or browser state. Because the
 *     frame is opaque its `event.origin` is the string `'null'`, which is NEVER
 *     used as authentication: a message is bound to BOTH the exact
 *     `contentWindow` and the random per-pane `channel` the issuer minted. A
 *     downward post is addressed to the exact frame with target `'*'` (an opaque
 *     origin can never match a concrete origin string) and is channel-stamped.
 *
 * The discriminated `PaneEndpoint` makes the two impossible to confuse: a relay
 * endpoint has no port/token, a direct endpoint has no capability/channel, and a
 * mode/endpoint mismatch resolves to `null` rather than a wrong address.
 *
 * The remote pane still controls the parent through exactly the existing action
 * protocol (unread counts, native-notify, chained-crew adoption, switch, pins,
 * focus, cursor-away, drag gaps); this module only changes how those messages
 * are ADDRESSED and ATTRIBUTED — the pane gains no parent DOM, cookie, storage,
 * or owner-API access, in either mode.
 */
import { resolveTunnelOrigin } from './tunnelOrigin'
import { isLoopbackHostname } from './paneEmbedding'
import { PANE_CHANNEL_FIELD } from './relayPaneBootstrap'

/** The relay capability prefix a same-origin-relay `documentPath` must be under. */
const RELAY_DOC_PREFIX = '/instance-pane/'

/** Narrow an `unknown` to an indexable object, so field reads need no cast. */
function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null
}

/** How the parent dashboard is reached, which fixes the pane transport. */
export type PaneMode =
  | { readonly kind: 'direct-loopback'; readonly protocol: string; readonly hostname: string }
  | { readonly kind: 'same-origin-relay'; readonly origin: string }

/**
 * A resolved per-pane endpoint (the issuer's discriminated response, typed). A
 * relay endpoint carries a capability `documentPath` + `channel` and NO
 * port/token; a direct endpoint carries the loopback `port` + `token` and NO
 * capability.
 */
export type PaneEndpoint =
  | { readonly kind: 'direct-loopback'; readonly port: number; readonly token: string }
  | {
      readonly kind: 'same-origin-relay'
      readonly documentPath: string
      readonly channel: string
      readonly protocol: number
      /**
       * Wall-clock epoch-ms deadline of the capability lease behind this
       * endpoint. After it, every request under the capability (API, asset,
       * reload, socket reconnect) gets the relay's uniform 404. The viewport
       * REISSUES before this — see `relayRenewDelayMs` — rotating the whole
       * endpoint (documentPath + channel), the iframe, the readiness, and the
       * storage seed together onto a fresh lease. Direct endpoints have no lease
       * and carry no deadline.
       */
      readonly leaseExpiresAtEpochMs: number
    }

/**
 * The longest a relay pane waits before its lease deadline to reissue. The
 * effective margin is capped at a third of the remaining lease, so a short lease
 * (a test, or a hub that shortens the TTL) renews proportionally early instead
 * of thrashing, while a normal 15-minute lease renews a bounded ~2 minutes out.
 */
export const RELAY_LEASE_RENEW_MARGIN_MS = 2 * 60 * 1000

/**
 * Delay between bounded renewal RETRIES after a failed or declined reissue. A
 * single transient issuer failure (a gateway restart, a network blip, a
 * protocol read that could not complete in the margin) must not strand the pane
 * on its old capability until it 404s: the viewport retries on this cadence, but
 * only while another attempt can still complete before the lease deadline. Once
 * no attempt fits, the pane's timed-out state is surfaced instead of a silent
 * expiry. Short relative to the ~2-minute margin so several retries fit a normal
 * lease, and a constant so a fake-timer test can advance by it deterministically.
 */
export const RELAY_LEASE_RENEW_RETRY_MS = 15 * 1000

/**
 * Milliseconds to wait before reissuing a relay lease that expires at
 * `leaseExpiresAtEpochMs`. Pure and total: renews immediately (`0`) once the
 * deadline is within the margin (or already past), and otherwise schedules the
 * reissue `min(maxMargin, remaining/3)` before the deadline. Unit-testable
 * without a clock — the caller passes `nowMs`.
 */
export function relayRenewDelayMs(
  nowMs: number,
  leaseExpiresAtEpochMs: number,
  maxMarginMs: number = RELAY_LEASE_RENEW_MARGIN_MS,
): number {
  const remaining = leaseExpiresAtEpochMs - nowMs
  if (remaining <= 0) return 0
  const margin = Math.min(maxMarginMs, remaining / 3)
  return Math.max(0, remaining - margin)
}

/** A resolved pane address: the iframe `src` and the reference origin. */
export interface PaneAddress {
  readonly src: string
  readonly origin: string
}

/**
 * True when two endpoints address the SAME pane load — same transport and same
 * load-identifying fields. Used by the warm store to decide whether a re-issue
 * changes the iframe `src` (and so invalidates a recorded readiness). Direct
 * loads are identified by (port, token); relay loads by (documentPath, channel)
 * — a new capability or channel is a new load even if the id is unchanged.
 */
export function sameEndpoint(a: PaneEndpoint | undefined, b: PaneEndpoint | undefined): boolean {
  if (!a || !b) return a === b
  if (a.kind === 'direct-loopback' && b.kind === 'direct-loopback') {
    return a.port === b.port && a.token === b.token
  }
  if (a.kind === 'same-origin-relay' && b.kind === 'same-origin-relay') {
    return a.documentPath === b.documentPath && a.channel === b.channel
  }
  return false
}

/** What attribution needs at message time (built by the viewport from live state). */
export interface PaneMessageContext {
  /** direct mode: loopback port → instance id, for the currently-warm tunnels. */
  readonly portToId?: Map<number, string>
  /** relay mode: instance id → its live endpoint (carrying the current channel). */
  readonly endpoints?: ReadonlyMap<string, PaneEndpoint>
  /** relay mode: instance id → its live iframe `contentWindow`, for the exact-source check. */
  readonly frames?: ReadonlyMap<string, Window>
}

/** The untrusted inbound message, reduced to the fields attribution reads. */
export interface PaneInboundEvent {
  readonly source: MessageEventSource | null
  readonly origin: string
  readonly data: unknown
}

/**
 * Decide the pane transport for the connection the parent arrived on. Direct
 * loopback needs BOTH plain http AND a loopback host; anything else (notably a
 * published HTTPS host) is `same-origin-relay` and carries the parent's own
 * origin so the opaque child can address it.
 */
export function resolvePaneMode(
  loc: Pick<Location, 'protocol' | 'hostname' | 'origin'>,
): PaneMode {
  if (loc.protocol === 'http:' && isLoopbackHostname(loc.hostname)) {
    return { kind: 'direct-loopback', protocol: loc.protocol, hostname: loc.hostname }
  }
  return { kind: 'same-origin-relay', origin: loc.origin }
}

/** The `access` mode to request from the issuer for this parent. */
export function paneAccessFor(mode: PaneMode): 'direct-loopback' | 'same-origin-relay' {
  return mode.kind
}

/**
 * Validate the issuer's JSON response into exactly one typed `PaneEndpoint`, or
 * `null`. Strict: a relay endpoint's `documentPath` must be under the relay
 * prefix, and a direct endpoint must carry a positive port AND a token.
 */
export function parsePaneEndpoint(raw: unknown): PaneEndpoint | null {
  if (!isRecord(raw)) return null
  const o = raw
  if (o.kind === 'direct-loopback') {
    const port = o.local_port
    const token = o.token
    if (typeof port !== 'number' || !Number.isInteger(port) || port <= 0) return null
    if (typeof token !== 'string' || !token) return null
    return { kind: 'direct-loopback', port, token }
  }
  if (o.kind === 'same-origin-relay') {
    const documentPath = o.documentPath
    const channel = o.channel
    const protocol = o.protocol
    const leaseExpiresAtEpochMs = o.leaseExpiresAtEpochMs
    if (typeof documentPath !== 'string' || !documentPath.startsWith(RELAY_DOC_PREFIX)) return null
    if (typeof channel !== 'string' || !channel) return null
    if (typeof protocol !== 'number') return null
    if (typeof leaseExpiresAtEpochMs !== 'number' || !Number.isFinite(leaseExpiresAtEpochMs)) return null
    return { kind: 'same-origin-relay', documentPath, channel, protocol, leaseExpiresAtEpochMs }
  }
  return null
}

/**
 * The pane's iframe `src` + reference origin, or `null` when there is no
 * endpoint or the endpoint does not match the mode. Relay `src` is the
 * same-origin `documentPath` — never a `host:port` URL and never the token.
 */
export function paneEndpointSrc(mode: PaneMode, endpoint: PaneEndpoint | undefined): PaneAddress | null {
  if (!endpoint) return null
  if (endpoint.kind === 'direct-loopback' && mode.kind === 'direct-loopback') {
    return {
      src: `http://${mode.hostname}:${endpoint.port}/?token=${encodeURIComponent(endpoint.token)}`,
      origin: `${mode.protocol}//${mode.hostname}:${endpoint.port}`,
    }
  }
  if (endpoint.kind === 'same-origin-relay' && mode.kind === 'same-origin-relay') {
    // A same-origin, root-relative capability path. The iframe loads it on the
    // hub origin and the sandbox (no allow-same-origin) makes the document
    // opaque; nothing about the remote (port, token) is in the URL.
    return { src: endpoint.documentPath, origin: mode.origin }
  }
  return null
}

/** A downward post, addressed for the endpoint's transport, or `null`. */
export interface PaneMessageEnvelope {
  readonly message: Record<string, unknown>
  readonly targetOrigin: string
}

/**
 * Build the `{message, targetOrigin}` for a downward post. Direct targets the
 * exact loopback origin and leaves the message untouched. Relay stamps the
 * channel and targets `'*'` — required because an opaque frame's origin never
 * matches a concrete string; the exact-`contentWindow` send site plus the
 * channel are what bind it, not the target origin.
 */
export function paneMessageEnvelope(
  mode: PaneMode,
  endpoint: PaneEndpoint | undefined,
  message: Record<string, unknown>,
): PaneMessageEnvelope | null {
  if (!endpoint) return null
  if (endpoint.kind === 'direct-loopback' && mode.kind === 'direct-loopback') {
    const addr = paneEndpointSrc(mode, endpoint)
    return addr ? { message, targetOrigin: addr.origin } : null
  }
  if (endpoint.kind === 'same-origin-relay' && mode.kind === 'same-origin-relay') {
    return {
      message: { ...message, [PANE_CHANNEL_FIELD]: endpoint.channel },
      targetOrigin: '*',
    }
  }
  return null
}

/**
 * Attribute an untrusted inbound message to one warm instance id, or `null`.
 *
 * Direct mode delegates to `resolveTunnelOrigin` verbatim — validation by exact
 * loopback origin + a currently-owned port, unchanged. Relay mode binds on the
 * exact `contentWindow` AND the current per-pane channel, and NEVER consults
 * `event.origin` (which is the opaque `'null'`): a stale channel (retry/rebuild
 * minted a new one), a foreign frame, an unmounted frame, or a channel-less
 * payload all resolve to `null`.
 */
export function resolvePaneMessage(
  ev: PaneInboundEvent,
  mode: PaneMode,
  ctx: PaneMessageContext,
): string | null {
  if (mode.kind === 'direct-loopback') {
    return resolveTunnelOrigin(ev.origin, ctx.portToId ?? new Map())
  }
  // same-origin-relay
  const data = ev.data
  if (!isRecord(data)) return null
  const channel = data[PANE_CHANNEL_FIELD]
  if (typeof channel !== 'string' || !channel) return null
  const endpoints = ctx.endpoints
  if (!endpoints) return null
  let matchedId: string | null = null
  for (const [id, ep] of endpoints) {
    if (ep.kind === 'same-origin-relay' && ep.channel === channel) {
      matchedId = id
      break
    }
  }
  if (matchedId === null) return null // stale / unknown channel
  const frame = ctx.frames?.get(matchedId)
  if (!frame || frame !== ev.source) return null // foreign / stale / unmounted frame
  return matchedId
}

/** A subsequent-document bootstrap request, reduced to the fields the decision reads. */
export interface PaneBootstrapRequest {
  /** The exact sender window (`MessageEvent.source`). */
  readonly source: MessageEventSource | null
  /** The capability prefix the child derived from its own `location.pathname`. */
  readonly documentPath: unknown
  /** The per-document nonce the child generated, echoed back in the reply. */
  readonly nonce: unknown
}

/** What the parent must send back to reseed a subsequent relay document. */
export interface PaneBootstrapGrant {
  readonly id: string
  readonly channel: string
  readonly protocol: number
  readonly nonce: string
}

/**
 * Decide whether to answer a subsequent-document bootstrap request, or `null` to
 * refuse. A relay document that navigated within its capability lost its
 * `window.name` envelope and asks the parent to reseed; this binds that request
 * on TWO facts the parent controls — the exact sender frame (`ctx.frames`) AND
 * the capability `documentPath` the parent itself issued for that pane
 * (`endpoint.documentPath`) — and NEVER on `event.origin`, which is the opaque
 * `'null'`. The `documentPath` check is what stops a document navigated to a
 * FOREIGN origin inside the same frame from harvesting the reply: the frame
 * identity (a `WindowProxy`) survives cross-origin navigation, but an off-capability
 * document cannot supply the 256-bit capability prefix (its `window.name` was
 * cleared and every relay response is served `Referrer-Policy: no-referrer`, so
 * it never learns the path). The `nonce` is echoed so the child can pair the
 * reply to its request; it is length-bounded to keep a hostile frame from
 * reflecting an unbounded string back through the parent.
 */
export function resolvePaneBootstrapRequest(
  req: PaneBootstrapRequest,
  ctx: PaneMessageContext,
): PaneBootstrapGrant | null {
  const { source, documentPath, nonce } = req
  if (!source) return null
  if (typeof documentPath !== 'string' || !documentPath) return null
  if (typeof nonce !== 'string' || !nonce || nonce.length > 256) return null
  const endpoints = ctx.endpoints
  const frames = ctx.frames
  if (!endpoints || !frames) return null
  for (const [id, ep] of endpoints) {
    if (ep.kind !== 'same-origin-relay') continue
    if (ep.documentPath !== documentPath) continue // capability (issued prefix) mismatch
    if (frames.get(id) !== source) continue // foreign / stale / unmounted frame
    return { id, channel: ep.channel, protocol: ep.protocol, nonce }
  }
  return null
}
