/**
 * Cross-session message plumbing for the transcript.
 *
 * The RECEIVER's row: a `session_send` / `session_broadcast` delivery lands a
 * `user` row whose gateway-set `meta.relay` names the sender and records where
 * its provenance line sits. `UserMessage` names the sender from that trusted
 * field and drops the line by those positions: a peer could type the envelope
 * into its own composer, but only the delivery path writes `meta.relay`.
 *
 * The SENDER's own transcript: a `session_send` tool call that delivered renders
 * as the OUTGOING card, its `@tag` naming the slot the gateway resolved, both
 * read from the receipt the gateway stamps on the call's row
 * (`meta.sessionSend`). Neither side reads anything out of message text.
 */
import type { ChatMessage } from '../../types'

export interface RelaySender {
  senderKey: string
  via: string
  /** Where the gateway's provenance line(s) sit in the row's text, as
   *  `[start, end)` spans in UTF-16 code units (`dashboard/relay_spans.py`):
   *  one per send. Absent on a row written before the field existed. */
  envelope?: Array<[number, number]>
}

/** The spans of a `meta.relay.envelope`, when every one is a well-formed,
 *  ascending, non-overlapping `[start, end)` pair; else undefined. */
function readEnvelope(value: unknown): Array<[number, number]> | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined
  const spans: Array<[number, number]> = []
  let floor = 0
  for (const span of value) {
    if (!Array.isArray(span) || span.length !== 2) return undefined
    const [start, end] = span as unknown[]
    if (!Number.isInteger(start) || !Number.isInteger(end)) return undefined
    if ((start as number) < floor || (end as number) <= (start as number)) return undefined
    spans.push([start as number, end as number])
    floor = end as number
  }
  return spans
}

/** The gateway-set `meta.relay` on a received cross-session row, or null. */
export function readRelay(meta: ChatMessage['meta']): RelaySender | null {
  const relay = (meta as { relay?: unknown } | undefined)?.relay
  if (!relay || typeof relay !== 'object') return null
  const key = (relay as { senderKey?: unknown }).senderKey
  const via = (relay as { via?: unknown }).via
  if (typeof key !== 'string' || !key) return null
  const envelope = readEnvelope((relay as { envelope?: unknown }).envelope)
  return { senderKey: key, via: typeof via === 'string' ? via : 'session_send', ...(envelope ? { envelope } : {}) }
}

/** *content* with the relay's envelope spans removed, or null when the relay
 *  records none or a span does not fit the text (the caller then shows the row
 *  as it would without the field). Removed by position only: the text is never
 *  searched for the envelope. */
export function withoutRelayEnvelope(content: string, relay: RelaySender | null): string | null {
  const spans = relay?.envelope
  if (!spans || spans[spans.length - 1][1] > content.length) return null
  let out = ''
  let cursor = 0
  for (const [start, end] of spans) {
    out += content.slice(cursor, start)
    cursor = end
  }
  return out + content.slice(cursor)
}

export interface SessionSendCall {
  /** The target as the call named it (a key, a title or a stem). */
  target: string
  message: string
  /** The slot key the gateway resolved and delivered to, from the receipt the
   *  gateway stamped on the call's row (`meta.sessionSend`). */
  delivered: string
}

/** What the gateway says a `session_send` call did: it started a turn on the
 *  message, took it into the running turn, or queued it. */
export type SessionSendOutcome = 'delivered' | 'steered' | 'queued'
const SESSION_SEND_OUTCOMES: ReadonlySet<string> = new Set(['delivered', 'steered', 'queued'])

/** The receipt the gateway stamps on its own `session_send` row once the call
 *  delivered (`meta.sessionSend`, `dashboard/session_send_receipts.py`): typed
 *  fields, read by exact key. Null for a row with none (still running, refused,
 *  or written before the field existed) or a malformed one. */
export function readSessionSendResult(message: ChatMessage): { outcome: SessionSendOutcome; target: string } | null {
  const receipt = (message.meta as { sessionSend?: unknown } | undefined)?.sessionSend
  if (!receipt || typeof receipt !== 'object') return null
  const outcome = (receipt as { outcome?: unknown }).outcome
  const target = (receipt as { target?: unknown }).target
  if (typeof outcome !== 'string' || !SESSION_SEND_OUTCOMES.has(outcome)) return null
  if (typeof target !== 'string' || !target) return null
  return { outcome: outcome as SessionSendOutcome, target }
}

/** The MCP server that serves the gateway's own `session_send`
 *  (`SERVER_NAME` in `mcp_dashboard_caller_scope.py`). */
export const SESSION_SEND_SERVER = 'kirocrew-dashboard'

/** True when a tool row's meta names the gateway's own `session_send`. */
export function isGatewaySessionSendMeta(meta: Record<string, unknown> | undefined): boolean {
  return meta?.tool_name === 'session_send' && meta.mcp_server === SESSION_SEND_SERVER
}

/** True when a tool row is a single-target `session_send` call that FINISHED
 *  and DELIVERED — the one the OUTGOING card draws. A call still running, one
 *  the person rejected or a policy denied (it never ran, so it has no receipt)
 *  and one the gateway refused all stay the generic tool line, which
 *  shows that state; the card's "Sent to" is only ever said of a delivery.
 *  `session_broadcast` (many targets, no single `@tag`) is deliberately left to
 *  the generic tool line. */
export function isSessionSendTool(message: ChatMessage): boolean {
  return parseSessionSend(message) !== null
}

/** A `session_send` call's own arguments from the row's JSON input. Some
 *  backends record a wrapper: Codex `{server, tool, arguments}`, a Tool Search
 *  deferred call `{tool_id: "<server>::<tool>", arguments}`. Unwrapped only when
 *  it names the gateway's own `session_send`, as the gateway's
 *  `session_send_receipts.call_arguments` does. */
function sessionSendArguments(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const obj = value as Record<string, unknown>
  const inner = obj.arguments
  if (inner && typeof inner === 'object' && !Array.isArray(inner) && (
    (obj.server === SESSION_SEND_SERVER && obj.tool === 'session_send')
    || obj.tool_id === `${SESSION_SEND_SERVER}::session_send`
  )) return inner as Record<string, unknown>
  return obj
}

/** The `{target, message, delivered}` of a delivered `session_send` call: the
 *  args from its JSON input (`meta.input`), the resolved slot key from the
 *  gateway's receipt. Null for any other tool row, when the args are absent or
 *  malformed, or when the row carries no receipt.
 *
 *  Only the gateway's OWN `session_send` qualifies: any connected MCP server can
 *  name a tool `session_send`, and the card presents the call as a delivery the
 *  gateway made. A row naming no
 *  server, or another one, stays the generic tool line. */
export function parseSessionSend(message: ChatMessage): SessionSendCall | null {
  if (message.role !== 'tool') return null
  if (!isGatewaySessionSendMeta(message.meta as Record<string, unknown> | undefined)) return null
  const raw = (message.meta as { input?: unknown } | undefined)?.input
  if (typeof raw !== 'string' || !raw.trimStart().startsWith('{')) return null
  try {
    const obj = sessionSendArguments(JSON.parse(raw))
    if (!obj) return null
    const target = obj.target
    const msg = obj.message ?? obj.text
    if (typeof target !== 'string' || !target) return null
    if (typeof msg !== 'string' || !msg) return null
    const receipt = readSessionSendResult(message)
    if (!receipt) return null
    return { target, message: msg, delivered: receipt.target }
  } catch {
    return null
  }
}
