/**
 * Cross-session message plumbing for the transcript.
 *
 * The RECEIVER's row: a `session_send` / `session_broadcast` delivery lands a
 * `user` row whose gateway-set `meta.relay` names the sender. `UserMessage`
 * names the sender from that trusted field, and the seed line is parsed in one
 * place (`lib/crewSeed.ts`): a peer could type the envelope into its own
 * composer, but only the delivery path writes `meta.relay`.
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

/** The slot key a `session_send` result *output* says it delivered to, or null
 *  for anything that is not a success result. */
export function sessionSendDeliveredTo(output: string): string | null {
  if (!output) return null
  const hit = SEND_RESULT_RE.exec(resultText(output))
  return hit ? hit[1].trim() || null : null
}

/** The slot key a FINISHED, SUCCESSFUL `session_send` delivered to, from its
 *  result (`meta.output`), or null when the call has no success result. */
export function readSessionSendResult(message: ChatMessage): string | null {
  const output = (message.meta as { output?: unknown } | undefined)?.output
  return typeof output === 'string' ? sessionSendDeliveredTo(output) : null
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
 *  the person rejected or a policy denied (it never ran, so it has no success
 *  result) and one the gateway refused all stay the generic tool line, which
 *  shows that state; the card's "Sent to" is only ever said of a delivery.
 *  `session_broadcast` (many targets, no single `@tag`) is deliberately left to
 *  the generic tool line. */
export function isSessionSendTool(message: ChatMessage): boolean {
  return parseSessionSend(message) !== null
}

/** The `{target, message, delivered}` of a delivered `session_send` call: the
 *  args from its JSON input (`meta.input`), the resolved slot key from its
 *  success result. Null for any other tool row, when the args are absent or
 *  malformed, or when the call has no success result.
 *
 *  Only the gateway's OWN `session_send` qualifies: any connected MCP server can
 *  name a tool `session_send` and answer with the same success text, and the
 *  card presents the result as a delivery the gateway made. A row naming no
 *  server, or another one, stays the generic tool line. */
export function parseSessionSend(message: ChatMessage): SessionSendCall | null {
  if (message.role !== 'tool') return null
  if (!isGatewaySessionSendMeta(message.meta as Record<string, unknown> | undefined)) return null
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
