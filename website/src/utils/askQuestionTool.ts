/**
 * The blocking `ask_question` tool returns the user's answers as its tool
 * RESULT, never as a chat message. That result is the transcript's only record
 * of what was answered, so the tool row renders it as a readable card.
 *
 * Title shapes mirror `waitToolTitle.ts` (direct, pooled `server___tool`,
 * suffixed `(mcp)`) and are matched as a strict allowlist for the same reason:
 * the card claims "these are your answers", so it must never render on some
 * other tool whose output happens to look similar.
 */
const ASK_TITLE_RE = /^(?:[a-z0-9][a-z0-9._-]*___)?ask_question(?:\s*\([a-z0-9 ._-]+\))?$/

export function isAskQuestionToolName(name: string): boolean {
  return ASK_TITLE_RE.test((name || '').trim().toLowerCase())
}

/** The only MCP server whose `ask_question` returns the user's answers. */
export const ASK_QUESTION_SERVER = 'kirocrew-core'

/** The header line `_format_ask_outcome` (mcp_tools/control.py) writes first. */
export const ASK_ANSWERED_HEADER = 'User has answered your questions:'

/**
 * Mirrors `_ASK_PAIR_SEPARATOR` (validation.py): joins the two JSON strings of an
 * answered pair. Neither `=` nor `:`, because the transport's key=value credential
 * scrub accepts a quote and either of those after a key name and would match
 * across a question and its answer.
 */
export const ASK_PAIR_SEPARATOR = ' -> '

/** Maximum answered tool-result size mirrored from session_directive.py. */
export const MAX_TOOL_RESULT_CHARS = 8000

/**
 * Mirrors `_ASK_MAX_ANSWER_LEN` (validation.py): the longest custom answer the
 * server accepts, derived there so four max-length questions and answers fit
 * one tool result. `askQuestionTool.test.ts` recomputes it from the Python
 * source, so this literal cannot drift from the server's bound.
 */
export const ASK_MAX_ANSWER_LEN = 1482

// Stateless cards keep the legacy custom-answer limit from main.
export const NATIVE_MAX_ANSWER_LEN = 2000
