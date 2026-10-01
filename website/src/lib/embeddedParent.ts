/**
 * The embedded-pane → parent messaging boundary, for the same-origin-relay
 * transport.
 *
 * A relay pane is a sandboxed, opaque-origin iframe: its `event.origin` is the
 * string `'null'`, so the parent can NOT authenticate the pane's upward messages
 * by origin the way it does for a direct-loopback pane. Instead the parent binds
 * each relay message on BOTH the exact sender frame AND a random per-pane
 * `channel` it minted at issue time (see `lib/paneChannel` `resolvePaneMessage`).
 *
 * The pre-module bootstrap the hub injects into the relay entry document
 * (`instance_pane_relay._RELAY_PANE_BOOTSTRAP_SCRIPT`, mirroring
 * `relayPaneBootstrap.ts`) reads that channel out of the seed envelope and
 * publishes it on `window.__kcRelayPaneContext` before any app module runs. This
 * module is the single reader of that context, and `stampPaneChannel` is the one
 * helper every embedded upward `postMessage` routes its payload through so the
 * parent can attribute it.
 *
 * In a direct-loopback pane (and in the top-level dashboard) the context is
 * absent: `stampPaneChannel` returns the message UNCHANGED, so the parent's
 * exact-loopback-origin validation path is byte-identical to before the relay.
 */
import { PANE_CHANNEL_FIELD } from './relayPaneBootstrap'

/**
 * The pre-module relay bootstrap installs this global on `window` before any app
 * module runs (see `instance_pane_relay._RELAY_PANE_BOOTSTRAP_SCRIPT`). Declared
 * as `unknown` — its shape is validated at runtime by `relayPaneContext` — so
 * reading it needs no cast; it is absent in a direct pane and the top-level
 * dashboard.
 */
declare global {
  interface Window {
    __kcRelayPaneContext?: unknown
  }
}

/** The relay-pane context the pre-module bootstrap installs on `window`. Present
 *  only inside a same-origin-relay pane (opaque origin). */
export interface RelayPaneWindowContext {
  readonly channel: string
  readonly parentOrigin: string
  readonly protocol: number
}

/** Narrow an `unknown` to an indexable object, so field reads need no cast. */
function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null
}

/**
 * Read the relay-pane context the bootstrap installed, or `null` when this is
 * not a relay pane (direct-loopback pane, or the top-level dashboard). Strict on
 * every field so a malformed/partial global never half-enables relay behaviour.
 */
export function relayPaneContext(): RelayPaneWindowContext | null {
  if (typeof window === 'undefined') return null
  const raw = window.__kcRelayPaneContext
  if (!isRecord(raw)) return null
  const o = raw
  if (typeof o.channel !== 'string' || !o.channel) return null
  if (typeof o.parentOrigin !== 'string' || !o.parentOrigin) return null
  if (typeof o.protocol !== 'number') return null
  return { channel: o.channel, parentOrigin: o.parentOrigin, protocol: o.protocol }
}

/**
 * Stamp an embedded pane's upward message with the per-pane relay channel when
 * this pane is a same-origin-relay pane. The parent binds the message on this
 * channel plus the exact sender frame. In a direct-loopback pane the message is
 * returned unchanged (the parent validates the exact loopback origin instead),
 * so direct-mode wire traffic is byte-identical.
 */
export function stampPaneChannel<T extends object>(message: T): T {
  const ctx = relayPaneContext()
  if (!ctx) return message
  // The channel is an extra field the parent reads and every receiver ignores.
  // `T & Record<field, string>` is assignable to `T` (an intersection widens to
  // either operand), so no cast is needed to return the caller's declared shape.
  return { ...message, [PANE_CHANNEL_FIELD]: ctx.channel }
}
