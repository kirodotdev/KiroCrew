/**
 * Native (OS) notification posting that works from an embedded instance pane.
 *
 * An instance pane is the full dashboard SPA inside a cross-origin <iframe>
 * (`InstancesViewport.srcFor`). In that frame `Notification.permission` reads
 * `'denied'`: the desktop's permission handler grants `notifications` to the
 * main frame only (`website/electron/permission-handler.js`,
 * MAIN_FRAME_ONLY_PERMISSIONS), and a browser tab denies it to a cross-origin
 * iframe on its own. So a page-context `new Notification(...)` in a pane is a
 * silent no-op, and a remote crew's approvals and finished turns never reach
 * the OS banner.
 *
 * The frame that DOES hold the grant is the parent. This module relays the note
 * there instead of widening the permission: an embedded pane posts an
 * `mc-native-notify` envelope to `window.parent`, and the parent
 * (`InstancesViewport` onMessage) validates the sender's origin against its
 * currently-warm tunnel ports (`resolveTunnelOrigin`) before constructing the
 * `Notification` itself. The pane keeps every one of its own guards (mute rule,
 * `document.hidden`, `silent`) -- only a note the pane would have shown is
 * relayed, so no policy is duplicated here. The parent is the gate.
 *
 * Two call sites: `hooks/useNativeNotification.ts` (bell notes, which is also
 * how an approval reaches the OS) and `hooks/useWebSocket.ts` (chat finished).
 */
import { isEmbeddedPane } from './embedded'
import { parseLoopbackOriginPort } from './tunnelOrigin'

export const NATIVE_NOTIFY_TYPE = 'mc-native-notify'
export const NATIVE_NOTIFY_VERSION = 1

/** Bounds on the relayed strings; a banner never needs more. */
export const NATIVE_NOTIFY_MAX_TITLE = 200
export const NATIVE_NOTIFY_MAX_BODY = 1000
export const NATIVE_NOTIFY_MAX_TAG = 200

export interface NativeNotifyOptions {
  body?: string
  tag?: string
  /** OS sound. Defaults to true: WebAudio is the single source of sound. */
  silent?: boolean
  /** Pane-local URL; not relayed (the parent shows its own app icon). */
  icon?: string
}

/** Wire shape a pane posts to its parent. */
export interface NativeNotifyEnvelope {
  type: typeof NATIVE_NOTIFY_TYPE
  v: typeof NATIVE_NOTIFY_VERSION
  title: string
  body: string
  tag: string
  silent: boolean
}

/**
 * The exact origin to relay to, or null when this frame must not relay.
 *
 * A note body is user content (an approval's tool name, a bell note's text),
 * so the relay is narrower than "any iframe": the embedding page must be a
 * loopback http origin -- the only shape an Instances hub parent can have
 * (`InstancesViewport.srcFor` loads panes from the hub's own loopback host, and
 * `tunnelOrigin.ts` is the parent-side mirror of this rule) -- and this frame
 * must be the full dashboard, not a `/embed/*` document, which the operator may
 * have authorised a different host to DISPLAY (`frame-ancestors`) but not to
 * receive notifications from. An iframe's `document.referrer` is its embedding
 * page's origin under the dashboard's `strict-origin-when-cross-origin` policy;
 * when the browser withholds it there is no target and nothing is sent. Never
 * `'*'`.
 */
export function relayTargetOrigin(): string | null {
  if (!isEmbeddedPane()) return null
  try {
    if (window.location.pathname.startsWith('/embed/')) return null
    if (!document.referrer) return null
    const origin = new URL(document.referrer).origin
    return parseLoopbackOriginPort(origin) === null ? null : origin
  } catch {
    return null
  }
}

/**
 * Whether a call site may proceed to post a native notification.
 *
 * Embedded: true only when there is a relay target -- the pane's own permission
 * is irrelevant (it is denied by design) and the parent applies its own
 * `Notification.permission` check before posting. An embedded frame with no
 * relay target posts nothing (it could not anyway). Top-level: the usual
 * granted check.
 */
export function nativeNotificationPermitted(): boolean {
  if (isEmbeddedPane()) return relayTargetOrigin() !== null
  return typeof Notification !== 'undefined' && Notification.permission === 'granted'
}

/**
 * Post a native notification, or relay it to the parent when embedded.
 *
 * Never throws: Android Chrome throws "Illegal constructor" for page-context
 * Notification even with permission granted, and an uncaught throw on the
 * WebSocket message path kills the rest of the handler.
 */
export function postNativeNotification(title: string, options: NativeNotifyOptions = {}): void {
  const silent = options.silent ?? true
  if (isEmbeddedPane()) {
    const target = relayTargetOrigin()
    if (target === null) return
    try {
      const envelope: NativeNotifyEnvelope = {
        type: NATIVE_NOTIFY_TYPE,
        v: NATIVE_NOTIFY_VERSION,
        title: String(title).slice(0, NATIVE_NOTIFY_MAX_TITLE),
        body: String(options.body ?? '').slice(0, NATIVE_NOTIFY_MAX_BODY),
        tag: String(options.tag ?? '').slice(0, NATIVE_NOTIFY_MAX_TAG),
        silent,
      }
      window.parent?.postMessage(envelope, target)
    } catch {
      /* never let the relay break the caller */
    }
    return
  }
  if (typeof Notification === 'undefined') return
  try {
    new Notification(title, { ...options, silent })
  } catch {
    /* unsupported platform */
  }
}

/**
 * Parse an untrusted `postMessage` payload as a native-notify envelope.
 * Returns null unless every field has exactly the expected type. Origin is NOT
 * checked here -- the caller (`InstancesViewport` onMessage) has already
 * resolved `event.origin` to a warm tunnel before it reaches this.
 */
export function parseNativeNotifyEnvelope(data: unknown): NativeNotifyEnvelope | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  if (d.type !== NATIVE_NOTIFY_TYPE || d.v !== NATIVE_NOTIFY_VERSION) return null
  if (typeof d.title !== 'string' || typeof d.body !== 'string' || typeof d.tag !== 'string') return null
  if (typeof d.silent !== 'boolean') return null
  return {
    type: NATIVE_NOTIFY_TYPE,
    v: NATIVE_NOTIFY_VERSION,
    title: d.title.slice(0, NATIVE_NOTIFY_MAX_TITLE),
    body: d.body.slice(0, NATIVE_NOTIFY_MAX_BODY),
    tag: d.tag.slice(0, NATIVE_NOTIFY_MAX_TAG),
    silent: d.silent,
  }
}

/**
 * Parent side: post the banner for a note relayed by a warm instance pane.
 * The title carries the instance's name so a user with several crews can tell
 * them apart, and the tag is namespaced per instance so two crews' notes never
 * collapse onto one. Only posts when this (main) frame holds the grant; it
 * never prompts -- prompting belongs to a user gesture in Settings. `onClick`
 * runs when the user clicks the banner, so the caller can bring that
 * instance's tab forward: a banner that names a crew and then lands on
 * whichever tab was last active would not keep its own promise.
 */
export function postRelayedNativeNotification(
  instanceName: string,
  instanceId: string,
  note: NativeNotifyEnvelope,
  onClick?: () => void,
): boolean {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return false
  try {
    const n = new Notification(`${instanceName}: ${note.title}`, {
      body: note.body,
      tag: `${instanceId}:${note.tag}`,
      silent: note.silent,
    })
    if (onClick) n.onclick = onClick
    return true
  } catch {
    return false
  }
}
