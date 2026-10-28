/**
 * Cross-session message plumbing for the transcript (round 10, Surface 3).
 *
 * The RECEIVER's bubble: a `session_send` / `session_broadcast` delivery lands a
 * `user` row whose gateway-set `meta.relay` names the sender. The card is drawn
 * from that trusted field, never by parsing the `[sent by session …]` prefix —
 * a peer could type the envelope into its own composer, but only the delivery
 * path writes `meta.relay`. The prefix is still stripped from the body so the
 * card shows the message text alone.
 *
 * The SENDER's own transcript: the `session_send` tool call renders as the
 * OUTGOING card, with the target as the body's `@tag`.
 */
import type { ChatMessage } from '../../types'

export interface RelaySender {
  senderKey: string
  via: string
}

/** The gateway-set `meta.relay` on a received cross-session row, or null. */
export function readRelay(meta: ChatMessage['meta']): RelaySender | null {
  const relay = (meta as { relay?: unknown } | undefined)?.relay
  if (!relay || typeof relay !== 'object') return null
  const key = (relay as { senderKey?: unknown }).senderKey
  const via = (relay as { via?: unknown }).via
  if (typeof key !== 'string' || !key) return null
  return { senderKey: key, via: typeof via === 'string' ? via : 'session_send' }
}

/** Strip the gateway-written `[sent by session … via …]` provenance opener from
 *  a delivered body, so the card shows the message text alone. Only ever applied
 *  to a row already proven a genuine relay by {@link readRelay}, so the opener is
 *  the gateway's and not a line the sender wrote. */
export function stripRelayPrefix(content: string): string {
  return content.replace(/^\[sent by session [^\n\]]*\]\n\n/, '')
}

export interface SessionSendCall {
  target: string
  message: string
}

/** Trusted programmatic names of the cross-session send tools. */
const SEND_TOOL_NAMES = new Set(['session_send', 'session_broadcast'])

/** True when a tool row is a single-target `session_send` call — the one the
 *  OUTGOING card draws. `session_broadcast` (many targets, no single `@tag`) is
 *  deliberately left to the generic tool line. */
export function isSessionSendTool(message: ChatMessage): boolean {
  if (message.role !== 'tool') return false
  const name = (message.meta as { tool_name?: unknown } | undefined)?.tool_name
  if (name !== 'session_send') return false
  return parseSessionSend(message) !== null
}

/** Whether a tool row is EITHER cross-session send tool (for grouping / folding). */
export function isAnySessionSendTool(message: ChatMessage): boolean {
  if (message.role !== 'tool') return false
  const name = (message.meta as { tool_name?: unknown } | undefined)?.tool_name
  return typeof name === 'string' && SEND_TOOL_NAMES.has(name)
}

/** The `{target, message}` of a `session_send` tool call, parsed from its JSON
 *  args (`meta.input`), or null when they are absent or malformed. */
export function parseSessionSend(message: ChatMessage): SessionSendCall | null {
  const raw = (message.meta as { input?: unknown } | undefined)?.input
  if (typeof raw !== 'string' || !raw.trimStart().startsWith('{')) return null
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>
    const target = obj.target
    const msg = obj.message ?? obj.text
    if (typeof target !== 'string' || !target) return null
    if (typeof msg !== 'string' || !msg) return null
    return { target, message: msg }
  } catch {
    return null
  }
}
