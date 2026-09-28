/**
 * Reaching this dashboard from another device: the mobile sign-in link,
 * the connect-your-phone methods, and the tailnet origin with its mobile
 * configure, publish, unpublish and QR flow.
 */

import type { ClientTransport } from './transport'

/**
 * GET /api/tailnet/status — whether this machine's Tailscale MagicDNS name is in
 * the dashboard's Origin allow-list.
 *
 * `state` is derived SERVER-SIDE and the UI must render off it directly rather
 * than recomputing it from the other fields: one owner for the state machine
 * means the two layers cannot disagree about what "active" means. Precedence is
 * `pinned` > `off` > `unresolved` > `active`.
 *
 * `host` / `origin` / `resolved_at` describe the STARTUP resolution — the value
 * that actually went into `build_allowed_origins` — not a fresh probe. A live
 * probe could report a name the running origin set does not contain (daemon came
 * up after the gateway), and rendering that as trusted is the same
 * checked-but-never-ran defect the posture registry guards against.
 */
export interface TailnetStatusData {
  /** `dashboard.tailscale.enabled` as actually loaded, post-hydration. */
  enabled: boolean
  /** `capabilities.tailnet_origin` pinned off at the POLICY layer. */
  governance_pinned: boolean
  /** MagicDNS name resolved at startup; `''` when none was. */
  host: string
  /** `https://<host>`; `''` when `host` is `''`. */
  origin: string
  /** Epoch seconds of that startup resolution; `0` when it never resolved. */
  resolved_at: number
  state: 'pinned' | 'off' | 'unresolved' | 'active'
}

/** The single next action for tailnet mobile access.
 *
 * Ordered by what blocks what, and derived SERVER-side (see
 * `handlers/tailnet_mobile._derive_step`) so this list is rendered, never
 * re-computed here — one owner for the state machine.
 *
 * - `pinned` — an administrator's policy forbids tailnet access. Dead end.
 * - `install` / `start_daemon` / `sign_in` / `enable_magicdns` — the four ways
 *   there is no usable tailnet name, kept apart because each is a different
 *   errand for the operator.
 * - `enable_https` — the tailnet has not granted certificate provisioning for
 *   that name; this requires one-time tailnet administrator consent.
 * - `trust_off` — a name exists but the gateway will not accept it as an origin
 *   yet, so publishing would yield a reachable dashboard answering 403.
 * - `restart_gateway` — configured and resolvable NOW, but this server did not
 *   trust that exact name at startup. The one-click flow restarts and resumes.
 * - `occupied` — serve holds the mount for something that is not this dashboard,
 *   or its state is undeterminable; publishing would REPLACE it.
 * - `publish` — everything in place, one action left.
 * - `ready` — published and trusted.
 */
export type TailnetMobileStep =
  | 'pinned'
  | 'install'
  | 'start_daemon'
  | 'sign_in'
  | 'enable_magicdns'
  | 'enable_https'
  | 'trust_off'
  | 'restart_gateway'
  | 'occupied'
  | 'publish'
  | 'ready'

/** Live readiness for tailnet mobile access (`GET /api/tailnet/mobile`).
 *
 * Unlike `TailnetStatusData` this IS a live daemon probe: it answers "what can
 * this machine do next", where the other answers "what does the running server
 * already trust". Both are needed and they are not interchangeable. */
export interface TailnetMobileData {
  /** Per-process marker used only to prove a requested restart completed. */
  boot_id: string
  step: TailnetMobileStep
  origin: string
  /** Other devices on this tailnet. `0` means there is nothing to reach this
   *  dashboard FROM — publishing and the QR both still succeed, so this is the
   *  only signal that the scan is going to fail. */
  peer_count: number
  /** How many of those are online right now. */
  peers_online: number
  keep_awake: boolean
  /** Verbatim daemon/serve text. Shown as-is; never rephrased client-side. */
  detail: string
  download_url: string
}

/** Result of a publish/unpublish attempt. `detail` carries the daemon's own
 *  words, which is the only part guaranteed to stay correct if Tailscale
 *  rewords its errors. */
export interface TailnetMobileMutation {
  ok: boolean
  detail: string
}

/** Phone-connection methods surviving the governance filter. `kind` names the
 *  renderer; the dashboard skips a kind it does not recognise, so an edition's
 *  new method degrades to absent on an older frontend, never to a broken panel. */
export interface MobileConnectMethodsData {
  methods: { id: string; kind: string }[]
}

/** Durable config established by the explicit mobile setup action. */
export interface TailnetMobileConfigure {
  /** Trust/origin settings are snapshotted by middleware at gateway boot. */
  restart_required: boolean
}

/** A minted mobile-access QR. Carries a LIVE session token in both fields, so
 *  it is fetched only on explicit user action and never cached. */
export interface TailnetMobileQr {
  /** `https://<host>/?token=<token>` — treat as a credential. */
  url: string
  /** PNG data URI, rendered server-side (no client QR library). */
  image: string
  /** Lifetime of the session the link opens. */
  ttl_secs: number
  /** Window in which the LINK must be opened — much shorter than `ttl_secs`,
   *  and the part that surprises people. */
  link_window_secs: number
}

export function createRemoteAccessEndpoints({ get, post, j }: ClientTransport) {
  const mobileAccess = {
    // `sessionKey` MUST carry the active slot's key (`dashboard:<slot>`) when one
    // is active: the server's restricted-session guard reads X-Session-Key, and
    // the shared `dashboard:ui` default answers "not restricted" — which would
    // let an incognito/temporary slot mint a durable any-device credential. Same
    // cooperative-honesty contract as the tailnet mobile surface.
    mobileLoginLink: (sessionKey?: string) =>
      post('/api/auth/mobile-link', undefined, sessionKey).then(j) as Promise<{
      url: string
      expires_in: number
    }>,
    // Phone-connection methods available on this deployment under the current
    // governance ceiling (CPP mobile_connect seam). Descriptor-only: minting the
    // credential stays on each method's own endpoint above/below. An empty list
    // hides the sidebar "Connect your phone" entry entirely.
    mobileConnectMethods: () =>
      get('/api/mobile-connect/methods').then(j) as Promise<MobileConnectMethodsData>,
    // Tailnet origin (Settings → Security). READ ONLY here: the toggle writes
    // `dashboard.tailscale.enabled` through the generic config PATCH, because the
    // setting IS a config value and the status endpoint reports what the running
    // server resolved from it at startup.
    tailnetStatus: () => get('/api/tailnet/status').then(j) as Promise<TailnetStatusData>,
    // Mobile access. `tailnetMobile` is a LIVE probe (two daemon round trips
    // server-side), so poll it gently; the mutations below are user-driven.
    tailnetMobile: () => get('/api/tailnet/mobile').then(j) as Promise<TailnetMobileData>,
    tailnetMobileConfigure: () =>
      post('/api/tailnet/mobile/configure', {}).then(j) as Promise<TailnetMobileConfigure>,
    tailnetMobilePublish: () =>
      post('/api/tailnet/mobile/publish', {}).then(j) as Promise<TailnetMobileMutation>,
    tailnetMobileUnpublish: () =>
      post('/api/tailnet/mobile/unpublish', {}).then(j) as Promise<TailnetMobileMutation>,
    // Mints a session token. Called ONLY from an explicit user action — never on
    // render — because the response is a live credential. `sessionKey` carries
    // the caller's REAL slot key so the server's restricted-session guard sees
    // it instead of the shared `dashboard:ui` placeholder.
    tailnetMobileQr: (sessionKey?: string) =>
      post('/api/tailnet/mobile/qr', {}, sessionKey).then(j) as Promise<TailnetMobileQr>,
  }

  return { mobileAccess }
}
