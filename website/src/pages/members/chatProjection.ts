/**
 * Chat-profile projection of a member DM transcript.
 *
 * A member thread is read like a conversation with a colleague, not like an
 * engineering transcript: the reader wants the member's replies and anything
 * that needs a decision, and only optionally the tool steps behind a reply.
 * This module is a PURE function from the stored transcript to the rows the
 * chat profile draws. It never mutates the input; the rows it hides are kept
 * on the reply they belong to (`meta.chat_process`) so a disclosure can show
 * them without a second data source.
 *
 * Rules (see the Members chat-profile contract):
 * - Turn openers are `user`, `nudge`, `subagent`, `inject`.
 * - Per turn, only the FINAL assistant row is a reply; earlier assistant rows,
 *   tool rows, notices, reasoning (`thinking`) and the non-user openers are
 *   that reply's process.
 * - Consecutive nudge-opened turns whose reply is quiet fold into one
 *   `chat_fold` row ("N check-ins with nothing new").
 * - Rows a person must see (`escalation`, `permission`, `mcp_oauth`, `error`)
 *   and a member's sent files (`file`) are always kept; `streaming` is kept
 *   only on the trailing turn while the session runs.
 */
import type { ChatMessage } from '../../types'

export const TURN_OPENER_ROLES: readonly string[] = Object.freeze(['user', 'nudge', 'subagent', 'inject'])

/**
 * Rows that stay visible in the chat profile regardless of the turn shape.
 * `file` is a member's `file_send` (download / media card): a deliverable the
 * reader came for, not a process step.
 */
const ALWAYS_KEPT_ROLES: readonly string[] = Object.freeze(['user', 'escalation', 'permission', 'mcp_oauth', 'error', 'file'])

/** Hidden rows that become the reply's process trail (tool / notice / reasoning / openers). */
const PROCESS_ROLES: readonly string[] = Object.freeze(['tool', 'notice', 'thinking', 'subagent', 'nudge', 'inject'])

/** A reply shorter than this (after trimming) counts as "nothing new" for a nudge round. */
export const QUIET_REPLY_MAX_CHARS = 160

export interface Turn {
  /** The opener row, or null for rows preceding the first opener. */
  opener: ChatMessage | null
  /** Index of the opener in the source transcript (-1 when none). */
  openerIndex: number
  /** Every row of the turn INCLUDING the opener, in transcript order. */
  rows: ChatMessage[]
  /** Source index of `rows[0]`. */
  start: number
}

/** Split a transcript into turns at each opener row. */
export function segmentTurns(messages: readonly ChatMessage[]): Turn[] {
  const turns: Turn[] = []
  let current: Turn | null = null
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    if (TURN_OPENER_ROLES.includes(m.role)) {
      current = { opener: m, openerIndex: i, rows: [m], start: i }
      turns.push(current)
      continue
    }
    if (!current) {
      current = { opener: null, openerIndex: -1, rows: [], start: i }
      turns.push(current)
    }
    current.rows.push(m)
  }
  return turns
}

/** Strip whitespace and zero-width spaces (a quiet monitor cycle's "nothing"). */
function visibleText(content: string | undefined): string {
  return (content ?? '').replace(/[\s\u200B]+/g, ' ').trim()
}

/**
 * True when a reply says nothing worth a row of its own: no assistant row,
 * only whitespace / U+200B, or fewer than QUIET_REPLY_MAX_CHARS visible chars.
 */
export function isQuietReply(reply: ChatMessage | null | undefined): boolean {
  if (!reply) return true
  const text = visibleText(reply.content)
  return text.length < QUIET_REPLY_MAX_CHARS
}

function midOf(m: ChatMessage | null | undefined): string | undefined {
  const mid = m?.meta?.mid
  return typeof mid === 'string' && mid ? mid : undefined
}

interface ProjectedTurn {
  turn: Turn
  /** Rows the chat profile draws for this turn, in order. */
  out: ChatMessage[]
  /** Rows folded away if this turn joins a silent-rounds fold. */
  all: ChatMessage[]
  /** The turn is a nudge round with nothing new and nothing that needs a person. */
  quiet: boolean
}

function projectTurn(turn: Turn, isTrailing: boolean, running: boolean): ProjectedTurn {
  const { rows } = turn
  let finalAssistantIdx = -1
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].role === 'assistant') { finalAssistantIdx = i; break }
  }
  // Pass 1: the reply's process trail — every hidden row of the turn, wherever
  // it sits relative to the final reply (a tool call after the last text is
  // still part of how the turn was worked).
  const process: ChatMessage[] = []
  let toolCount = 0
  for (let i = 0; i < rows.length; i++) {
    const m = rows[i]
    if (m.role === 'assistant' && i !== finalAssistantIdx) { process.push(m); continue }
    if (PROCESS_ROLES.includes(m.role)) {
      if (m.role === 'tool') toolCount++
      process.push(m)
    }
    // system / queued / done / chunk and anything unknown: machine-facing, not
    // part of the conversation nor of its visible process.
  }

  // Pass 2: the rows the reader sees, in transcript order.
  const out: ChatMessage[] = []
  let keptNonReply = false
  const keepStreaming = isTrailing && running
  for (let i = 0; i < rows.length; i++) {
    const m = rows[i]
    if (ALWAYS_KEPT_ROLES.includes(m.role)) {
      out.push(m)
      if (m.role !== 'user') keptNonReply = true
      continue
    }
    if (m.role === 'streaming' && keepStreaming) {
      out.push(m)
      keptNonReply = true
      continue
    }
    if (m.role === 'assistant' && i === finalAssistantIdx) {
      out.push({
        ...m,
        meta: { ...(m.meta ?? {}), chat_process: process, chat_process_count: toolCount },
      })
    }
  }

  const reply = finalAssistantIdx >= 0 ? rows[finalAssistantIdx] : null
  const quiet =
    turn.opener?.role === 'nudge'
    && !keptNonReply
    && isQuietReply(reply)
    && !(isTrailing && running)
  return { turn, out, all: rows, quiet }
}

function foldRow(group: ProjectedTurn[], fallbackIndex: number): ChatMessage {
  const rows = group.flatMap(p => p.all)
  const last = rows[rows.length - 1]
  const firstMid = midOf(group[0].turn.opener) ?? midOf(rows[0])
  return {
    role: 'chat_fold',
    content: '',
    cls: 'msg msg-chat-fold',
    ts: last?.ts,
    meta: {
      kind: 'silent_rounds',
      count: group.length,
      rows,
      mid: 'fold-' + (firstMid ?? String(fallbackIndex)),
    },
  }
}

/**
 * Project a transcript into the chat profile's rows.
 *
 * `running` decides whether the trailing turn is still being written: its
 * `streaming` row is kept and it is never folded into a silent-rounds row.
 */
export function projectChatView(
  messages: readonly ChatMessage[],
  { running }: { running: boolean },
): ChatMessage[] {
  const turns = segmentTurns(messages)
  const out: ChatMessage[] = []
  let fold: ProjectedTurn[] = []
  const flushFold = () => {
    if (!fold.length) return
    out.push(foldRow(fold, fold[0].turn.start))
    fold = []
  }
  for (let i = 0; i < turns.length; i++) {
    const projected = projectTurn(turns[i], i === turns.length - 1, running)
    if (projected.quiet) { fold.push(projected); continue }
    flushFold()
    out.push(...projected.out)
  }
  flushFold()
  return out
}
