/** The data side of the sidebar's needs-me hover card (`NeedsMeHoverCard`):
 *  which rows earn a card, what each ask says, and the recap line.
 *
 *  Pure on purpose. The card reads three sources that already exist -- the
 *  slot row (`pending_approval`, `needs_input`, `has_options`, `last_message`),
 *  the slot's question card in `chat.pendingQuestions`, and the slot's own
 *  transcript window (`chatSlotDetail`) for the permission and elicitation rows
 *  -- and this module only shapes them, so every rule here has a unit test. */
import type { ChatMessage, ChatSlot } from '../../types'
import type { SessionSummary } from '../../types/sessionSummary'
import type { ElicitationField, ElicitationMeta } from '../chat/ElicitationCard'

/** One question of a question card, as `chat.pendingQuestions` stores it. */
export interface CardQuestion {
  question: string
  header?: string
  options: Array<{ label: string; description?: string }>
  multiSelect?: boolean
}

export type NeedsMeAsk =
  | {
      kind: 'approval'
      /** The request id the strict route names, plus the row it was raised on. */
      requestId: string
      /** Absent when the row carries no delivery identity: then the strict route
       *  cannot name it, and the card shows the ask without buttons. */
      requestMid?: string
      tool: string
      command: string
      /** The row was already settled when it was read (another tab, the session). */
      resolved?: string
    }
  | { kind: 'question'; questions: CardQuestion[] }
  | { kind: 'form'; asker: string; message: string; fields: ElicitationField[] }
  | { kind: 'reply'; text: string }

/** Whether a row is in a state the person owes: an approval, an open question
 *  card, or an unanswered question from the session. A blocked row (a safety
 *  rule refused a call, or the session waits on other work) is not one, and a
 *  row whose only pending approval turns out to be a safety-rule refusal gets no
 *  card either: `collectAsks` drops that ask, and the card shows only when at
 *  least one ask remains. */
export function slotNeedsMe(slot: Pick<ChatSlot, 'pending_approval' | 'needs_input'>, hasQuestion: boolean): boolean {
  return !!slot.pending_approval || !!slot.needs_input || hasQuestion
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/** The tool a permission row asks to run, as the person would name it. */
function approvalTool(m: ChatMessage, meta: Record<string, unknown>): string {
  const server = str(meta.mcp_server)
  const name = str(meta.tool_name)
  if (server && name) return `${server} · ${name}`
  if (name) return name
  // The row's content is the card's title ("🔧 [source] Title"): strip the marks.
  return str(m.content).replace(/^🔧\s*/, '').replace(/^\[[a-z_]+\]\s*/, '').trim()
}

/** The exact input the person approves: the shell command when there is one,
 *  else the tool's raw arguments. Never a summary. */
function approvalCommand(meta: Record<string, unknown>): string {
  const full = str(meta.full_command)
  if (full) return full
  const input = meta.tool_input
  if (typeof input === 'string') return input
  if (input && typeof input === 'object') {
    try { return JSON.stringify(input, null, 2) } catch { return '' }
  }
  return ''
}

/** Every ask a slot holds, oldest first, from its transcript rows and its
 *  question card. `settled` keeps a row the card itself just decided on screen,
 *  so the in-place result survives the refetch that reports it resolved. */
export function collectAsks(
  messages: readonly ChatMessage[],
  questions: readonly CardQuestion[] | undefined,
  opts: { settled?: ReadonlySet<string>; lastReply?: string } = {},
): NeedsMeAsk[] {
  const asks: NeedsMeAsk[] = []
  for (const m of messages) {
    const meta = (m.meta ?? {}) as Record<string, unknown>
    if (m.role === 'permission') {
      const requestId = str(meta.approval_id)
      if (!requestId) continue
      // A safety-rule refusal is not something the person approves here.
      if (str(meta.deny_override_rule)) continue
      const requestMid = str(meta.mid) || undefined
      const resolved = str(meta.resolved)
      const key = approvalKey(requestId, requestMid)
      if (resolved && !opts.settled?.has(key)) continue
      asks.push({ kind: 'approval', requestId, requestMid, tool: approvalTool(m, meta), command: approvalCommand(meta), ...(resolved ? { resolved } : {}) })
    } else if (m.role === 'elicitation') {
      const em = meta as ElicitationMeta
      if (em.resolved) continue
      asks.push({
        kind: 'form',
        asker: [em.mcp_server, em.tool_name].filter(Boolean).join(' · '),
        message: em.message || str(m.content),
        fields: Array.isArray(em.fields) ? em.fields : [],
      })
    }
  }
  if (questions && questions.length > 0) asks.push({ kind: 'question', questions: [...questions] })
  if (opts.lastReply) asks.push({ kind: 'reply', text: opts.lastReply })
  return asks
}

/** One identity per approval row: a request id can recur, the row id cannot. */
export function approvalKey(requestId: string, requestMid: string | undefined): string {
  return `${requestId}\u0000${requestMid ?? ''}`
}

/** The recap: the newest live intent's goal and what is true now. Falls back to
 *  the session's last reply line when there is no summary (the feature is off,
 *  or the session is too short), and to nothing when there is neither. */
export function pickRecap(summary: SessionSummary | undefined, lastMessage: string | undefined): { text: string; fromSummary: boolean } | null {
  const intents = (summary?.enabled ? summary.intents : []) ?? []
  const live = [...intents]
    .filter(i => i.state !== 'dropped')
    .sort((a, b) => b.last_touched_turn - a.last_touched_turn)[0]
  if (live) {
    const progress = (live.progress ?? []).filter(Boolean).join(' ')
    const text = [live.title, progress].filter(Boolean).join('. ')
    if (text) return { text, fromSummary: true }
  }
  const last = (lastMessage ?? '').trim()
  return last ? { text: last, fromSummary: false } : null
}
