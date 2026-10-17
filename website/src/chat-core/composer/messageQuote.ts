/**
 * Quote a WHOLE message (not a selection) into the next send.
 *
 * The quote travels two ways at once, and both come from ONE record:
 *
 * - As TEXT, for the agent: the quoted message is prepended to the typed text
 *   as a markdown blockquote with an attribution line (`quoteBlock`). That is
 *   the only form every consumer of the transcript sees — the model, a channel
 *   relay, a raw export — so the quote is never something only the dashboard
 *   knows about.
 * - As META, for the dashboard: `meta.quote` carries the same record, and the
 *   user bubble draws it as a card (author · time · excerpt, click = jump to the
 *   quoted message) INSTEAD of the raw `>` lines, which `stripQuoteBlock`
 *   removes from the rendered body. Same pattern as collapsed pastes
 *   (`[ Paste #N ]` tokens + `meta.pastes`) and staged files.
 *
 * Because the block is a pure function of the record, the strip is exact: a
 * row whose content does not begin with `quoteBlock(meta.quote)` (edited by
 * hand, a foreign client) renders its content untouched and keeps the card —
 * the card is additive, never a reason to hide text.
 *
 * One quote per message. Quoting again replaces the staged quote rather than
 * stacking; stacking is what the selection quote (`quoteIntoDraft`) is for.
 */

import { quoteAttribution } from './messageQuote.prompt'

export type MessageQuoteRole = 'user' | 'assistant'

export interface MessageQuote {
  /** Who wrote the quoted message. Drives the card's author label. */
  role: MessageQuoteRole
  /** The quoted text, trimmed and capped (`QUOTE_TEXT_MAX`). */
  text: string
  /** Server ts of the quoted message; the jump target. Absent for a row with none. */
  ts?: string
  /** Stable message id, preferred over `ts` when jumping (a same-ts pair). */
  mid?: string
  /** The speaker's display name when the surface has one (a crewmate DM
   *  labels its replies with the crewmate's name); the card shows it instead
   *  of the generic role label, so the quote names the same speaker the
   *  transcript does. Absent on the main chat. */
  author?: string
}

/** Cap on the quoted text. Long enough to carry a whole ordinary reply to the
 *  agent verbatim; short enough that quoting a 20 KB dump does not double the
 *  turn. Past it the text is cut at a word and marked with an ellipsis. */
export const QUOTE_TEXT_MAX = 1500

/** Paragraph break between the block and the typed text: leaves the caret on
 *  its own line, and lets `stripQuoteBlock` find the boundary exactly. */
const BLANK_LINE = '\n\n'

function capText(text: string): string {
  const trimmed = text.trim()
  if (trimmed.length <= QUOTE_TEXT_MAX) return trimmed
  // Cut on a code-point boundary: a UTF-16 slice through an astral character
  // (an emoji) would leave a lone surrogate at the end of the sent text.
  let cut = trimmed.slice(0, QUOTE_TEXT_MAX)
  const last = cut.charCodeAt(cut.length - 1)
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1)
  const atWord = cut.lastIndexOf(' ')
  return (atWord > QUOTE_TEXT_MAX * 0.6 ? cut.slice(0, atWord) : cut).trimEnd() + '…'
}

/** Build the record for a transcript row. `null` for a row with nothing to quote. */
export function quoteFromMessage(role: MessageQuoteRole, content: string, ts?: string, mid?: string, author?: string): MessageQuote | null {
  const text = capText(content)
  if (!text) return null
  const q: MessageQuote = { role, text }
  if (ts) q.ts = ts
  if (mid) q.mid = mid
  if (author?.trim()) q.author = author.trim()
  return q
}

/** The card's one-line excerpt: the quoted markdown as plain prose. The card is
 *  a one- or two-line reference, not a rendering surface, so emphasis marks,
 *  list bullets, heading hashes, fences and link syntax are dropped rather
 *  than shown raw; lines join with a middle dot so a list still reads as
 *  items. The record's `text` keeps the markdown for the agent's blockquote. */
export function quoteExcerpt(text: string): string {
  return text
    .split('\n')
    .map(line => line
      .replace(/^\s*(```|~~~).*$/, '')
      .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/, '')
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/(\*\*|__)(.+?)\1/g, '$2')
      .replace(/(^|[^\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?=[^\w*]|$)/g, '$1$2')
      .replace(/`([^`]*)`/g, '$1')
      .trim())
    .filter(Boolean)
    .join(' · ')
}


/** The markdown blockquote the record serializes to. */
export function quoteBlock(q: MessageQuote): string {
  return [...q.text.split('\n'), quoteAttribution(q.role)].map(line => '> ' + line).join('\n')
}

/** The full message text for a send: block, blank line, what was typed. */
export function prependQuote(typed: string, q: MessageQuote): string {
  const body = typed.trim()
  return body ? quoteBlock(q) + BLANK_LINE + body : quoteBlock(q)
}

/** The body to RENDER for a row carrying `meta.quote`: the block removed when
 *  (and only when) the content begins with exactly that block. */
export function stripQuoteBlock(content: string, q: MessageQuote): string {
  const block = quoteBlock(q)
  if (!content.startsWith(block)) return content
  return content.slice(block.length).replace(/^\n+/, '')
}

/** Read `meta.quote` off a row, refusing any shape a card could not draw. */
export function readMessageQuote(meta: Record<string, unknown> | undefined): MessageQuote | null {
  const raw = meta?.quote
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if ((r.role !== 'user' && r.role !== 'assistant') || typeof r.text !== 'string' || !r.text.trim()) return null
  // Capped on READ as well as on write: a row's meta is attacker-writable, and
  // the excerpt regexes are only cheap on text of the size this module mints
  // (fork Opus review). A capped text no longer matches the row's block, so
  // `stripQuoteBlock` leaves such a row's text alone -- card plus full text,
  // never a loss.
  const q: MessageQuote = { role: r.role, text: capText(r.text) }
  if (typeof r.ts === 'string' && r.ts) q.ts = r.ts
  if (typeof r.mid === 'string' && r.mid) q.mid = r.mid
  if (typeof r.author === 'string' && r.author.trim()) q.author = r.author.trim()
  return q
}
