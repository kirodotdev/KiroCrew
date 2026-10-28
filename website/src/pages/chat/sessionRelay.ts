/**
 * Cross-session message plumbing for the transcript.
 *
 * The RECEIVER's bubble: a `session_send` / `session_broadcast` delivery lands a
 * `user` row whose gateway-set `meta.relay` names the sender. The card is drawn
 * from that trusted field, never by parsing the `[sent by session …]` prefix —
 * a peer could type the envelope into its own composer, but only the delivery
 * path writes `meta.relay`. The prefix is still stripped from the body so the
 * card shows the message text alone.
 *
 * The SENDER's own transcript: a `session_send` tool call that delivered renders
 * as the OUTGOING card, its `@tag` naming the slot the gateway resolved.
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

/** Steer states the ordinary user bubble draws a status for (`UserMessage`):
 *  still waiting for the turn ("Steering…"), handed back to the queue, or taken
 *  back. A relay row in one of them is NOT yet a delivered message, so it keeps
 *  that bubble and its status instead of the card; once consumed it draws as the
 *  card. */
const UNSETTLED_STEER_STATES = new Set(['written', 'requeued', 'retracted'])

/** True when a row is a steer whose delivery is not settled (see
 *  {@link UNSETTLED_STEER_STATES}). */
export function isUnsettledSteer(meta: ChatMessage['meta']): boolean {
  const m = meta as { steer?: unknown; steerState?: unknown } | undefined
  return !!m?.steer && typeof m.steerState === 'string' && UNSETTLED_STEER_STATES.has(m.steerState)
}

/** Strip the gateway-written `[sent by session … via …]` provenance opener from
 *  a delivered body, so the card shows the message text alone. Only ever applied
 *  to a row already proven a genuine relay by {@link readRelay}, so the opener is
 *  the gateway's and not a line the sender wrote. */
export function stripRelayPrefix(content: string): string {
  // Every line-start opener, not only the first: a drain that merged several
  // queued sends from the SAME sender (it keeps `relay` only then) writes
  // "[N queued messages merged]" and then each send with its own opener. A peer
  // body cannot carry a lookalike, because `session_send` neutralises any
  // opening bracket of the envelope in the text it delivers.
  return content.replace(/^\[sent by session [^\n\]]*\]\n\n/gm, '')
}

export interface SessionSendCall {
  /** The target as the call named it (a key, a title or a stem). */
  target: string
  message: string
  /** The slot key the gateway resolved and delivered to, read from the call's
   *  own success result. */
  delivered: string
}

/** The success line `_run_session_send` opens its result with
 *  (`mcp_dashboard_session_runs.py`): steered, started or queued, each naming the
 *  slot key the gateway RESOLVED. Anything else (an `Error: …` refusal, a denied
 *  or rejected call that never ran, a call still running with no result yet) does
 *  not match, so the row stays the generic tool line with its own status. */
const SEND_RESULT_RE = /^\s*\u{1F4E8}\s*(?:Steered|Delivered to|Queued for)\s+`([^`\n]+)`/u

/** The text of a tool result: an MCP `{content: [{text}]}` envelope unwrapped,
 *  anything else as it is. */
function resultText(output: string): string {
  const trimmed = output.trimStart()
  if (!trimmed.startsWith('{')) return output
  try {
    const content = (JSON.parse(trimmed) as { content?: unknown } | null)?.content
    if (!Array.isArray(content)) return output
    return content
      .map(part => (part as { text?: unknown } | null)?.text)
      .filter((t): t is string => typeof t === 'string')
      .join('\n')
  } catch {
    return output
  }
}

/** The slot key a FINISHED, SUCCESSFUL `session_send` delivered to, from its
 *  result (`meta.output`), or null when the call has no success result. */
export function readSessionSendResult(message: ChatMessage): string | null {
  const output = (message.meta as { output?: unknown } | undefined)?.output
  if (typeof output !== 'string' || !output) return null
  const hit = SEND_RESULT_RE.exec(resultText(output))
  return hit ? hit[1].trim() || null : null
}

/** True when a tool row is a single-target `session_send` call that FINISHED
 *  and DELIVERED — the one the OUTGOING card draws. A call still running, one
 *  the person rejected or a policy denied (it never ran, so it has no success
 *  result) and one the gateway refused all stay the generic tool line, which
 *  shows that state; the card's "Sent to" is only ever said of a delivery.
 *  `session_broadcast` (many targets, no single `@tag`) is deliberately left to
 *  the generic tool line. */
export function isSessionSendTool(message: ChatMessage): boolean {
  if (message.role !== 'tool') return false
  const name = (message.meta as { tool_name?: unknown } | undefined)?.tool_name
  if (name !== 'session_send') return false
  return parseSessionSend(message) !== null
}

/** The `{target, message, delivered}` of a delivered `session_send` call: the
 *  args from its JSON input (`meta.input`), the resolved slot key from its
 *  success result. Null when the args are absent or malformed, or the call has
 *  no success result. */
export function parseSessionSend(message: ChatMessage): SessionSendCall | null {
  const raw = (message.meta as { input?: unknown } | undefined)?.input
  if (typeof raw !== 'string' || !raw.trimStart().startsWith('{')) return null
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>
    const target = obj.target
    const msg = obj.message ?? obj.text
    if (typeof target !== 'string' || !target) return null
    if (typeof msg !== 'string' || !msg) return null
    const delivered = readSessionSendResult(message)
    if (!delivered) return null
    return { target, message: msg, delivered }
  } catch {
    return null
  }
}
