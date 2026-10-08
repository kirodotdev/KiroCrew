import type { ChatMessage } from '../types'
import { ASK_ANSWERED_HEADER, ASK_PAIR_SEPARATOR, ASK_QUESTION_SERVER, isAskQuestionToolName } from './askQuestionTool'

export { ASK_ANSWERED_HEADER, ASK_PAIR_SEPARATOR, isAskQuestionToolName } from './askQuestionTool'

export interface AskAnswer {
  question: string
  answer: string
}

/** True when `output` is an `answered` ask_question result (header line first). */
export function isAskAnsweredOutput(output: string): boolean {
  return (output || '').trimStart().startsWith(ASK_ANSWERED_HEADER)
}

/** True for a tool row whose meta carries the user's ask_question answers, the same identity gate ToolCallLine applies before rendering the chip. */
export function isAskAnsweredToolMessage(m: ChatMessage): boolean {
  if (m.role !== 'tool' || !m.content?.startsWith('🔧')) return false
  const meta = m.meta as Record<string, unknown> | undefined
  return meta?.mcp_server === ASK_QUESTION_SERVER
    && isAskQuestionToolName(typeof meta.tool_name === 'string' ? meta.tool_name : '')
    && isAskAnsweredOutput(typeof meta.output === 'string' ? meta.output : '')
}

/**
 * Parse an `answered` tool result into question/answer pairs, or `null` when the
 * output is not one (dismissed, expired, a directive, an error, a truncated log).
 *
 * Each pair line is two JSON strings joined by ASK_PAIR_SEPARATOR, so an escaped
 * quote or newline inside either side cannot be mistaken for a separator.
 */
export function parseAskAnswers(output: string): AskAnswer[] | null {
  const lines = (output || '').replace(/\r\n/g, '\n').split('\n')
  if (lines[0]?.trim() !== ASK_ANSWERED_HEADER) return null
  const pairs: AskAnswer[] = []
  for (const raw of lines.slice(1)) {
    const line = raw.trim()
    if (!line) continue
    const cut = jsonStringEnd(line)
    if (cut < 0 || !line.startsWith(ASK_PAIR_SEPARATOR, cut)) return null
    try {
      const question: unknown = JSON.parse(line.slice(0, cut))
      const answer: unknown = JSON.parse(line.slice(cut + ASK_PAIR_SEPARATOR.length))
      if (typeof question !== 'string' || typeof answer !== 'string') return null
      pairs.push({ question, answer })
    } catch {
      return null
    }
  }
  return pairs.length ? pairs : null
}

/** Index just past the JSON string literal that starts `line`, or -1. */
function jsonStringEnd(line: string): number {
  if (line[0] !== '"') return -1
  for (let i = 1; i < line.length; i++) {
    if (line[i] === '\\') i++
    else if (line[i] === '"') return i + 1
  }
  return -1
}
