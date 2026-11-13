import type { ChatMessage } from '../../types'
import { type PasteBlock, expandAll as expandPasteTokens } from '../../utils/pasteTokens'
import { withoutSessionSeed } from '../../lib/crewSeed'
import { isPromptMessage } from '../../utils/pinnedPrompt'

/**
 * The session's initial prompt (#25977): the first message the person sent,
 * read from the START of the transcript.
 *
 * The open pane holds only the newest page of a long transcript, so the store
 * cannot answer this; the slot-detail route's `before` index pages from the
 * oldest row instead. Pages are read oldest first until a prompt turns up or
 * the transcript ends, at most `INITIAL_PROMPT_MAX_PAGES` of them, so a session
 * whose first rows are all machine rows costs a bounded number of reads.
 */
export const INITIAL_PROMPT_PAGE = 50
export const INITIAL_PROMPT_MAX_PAGES = 4

/**
 * The text a prompt row stands for: the same text the row's own Copy copies
 * (UserMessage): a delivered message's `[sent by session ...]` seed line
 * removed, collapsed pastes expanded. Empty for a row that is not a prompt the
 * person typed: a subagent completion persisted under `user` in older
 * scrollback, or a steer they took back (the pinned-prompt rule,
 * `isPromptMessage`).
 */
export function promptRowText(msg: ChatMessage): string {
  if (!isPromptMessage(msg)) return ''
  const meta = msg.meta as Record<string, unknown> | undefined
  const body = withoutSessionSeed(msg.content || '', meta)
  const pastes = (Array.isArray(meta?.pastes) ? meta.pastes : []).filter((b): b is PasteBlock =>
    !!b && typeof b === 'object' && typeof (b as PasteBlock).seq === 'number' && typeof (b as PasteBlock).content === 'string')
  const text = pastes.length ? expandPasteTokens(body, pastes) : body
  return text.trim() ? text : ''
}

/** The first prompt among `messages` (oldest first), or '' when there is none. */
export function firstPromptText(messages: readonly ChatMessage[]): string {
  for (const msg of messages) {
    const text = promptRowText(msg)
    if (text) return text
  }
  return ''
}

type FetchPage = (
  slot: string,
  limit: number,
  before: number,
) => Promise<{ messages?: ChatMessage[]; total?: number; next_before?: number }>

/**
 * The rows of one window, `[end - INITIAL_PROMPT_PAGE, end)`, oldest first.
 *
 * The server may cut a page's OLDEST rows to stay under its byte budget (a few
 * very large rows), and says so with a `next_before` above the window's start.
 * Those rows are the ones this reader came for, so the cut prefix is read before
 * the window is judged. A response without `next_before`, or one that does not
 * move older, ends the walk (one read per row at most).
 */
async function readWindow(slotKey: string, fetchPage: FetchPage, end: number): Promise<{ rows: ChatMessage[]; total: number | undefined }> {
  const start = Math.max(0, end - INITIAL_PROMPT_PAGE)
  let rows: ChatMessage[] = []
  let total: number | undefined
  let cursor = end
  for (let reads = 0; reads < INITIAL_PROMPT_PAGE && cursor > start; reads++) {
    const res = await fetchPage(slotKey, cursor - start, cursor)
    rows = [...(res.messages || []), ...rows]
    total ??= res.total
    const next = res.next_before
    if (typeof next !== 'number' || next >= cursor || next <= start) break
    cursor = next
  }
  return { rows, total }
}

/** Read the session's initial prompt, oldest page first. '' = no prompt yet. */
export async function loadInitialPrompt(slotKey: string, fetchPage: FetchPage): Promise<string> {
  for (let page = 1; page <= INITIAL_PROMPT_MAX_PAGES; page++) {
    const end = page * INITIAL_PROMPT_PAGE
    const { rows, total } = await readWindow(slotKey, fetchPage, end)
    const text = firstPromptText(rows)
    if (text) return text
    if (end >= (total ?? 0)) return ''
  }
  return ''
}
