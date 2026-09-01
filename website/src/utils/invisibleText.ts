/**
 * Invisible-only transcript rows.
 *
 * Unicode format characters (category Cf — the zero-width space/joiners, word
 * joiner, BOM, bidi controls, soft hyphen) render as nothing but are truthy in
 * string guards. Quiet monitor-loop cycles post a bare U+200B as their
 * say-nothing assistant reply, so a long-running session's transcript can hold
 * runs of assistant rows whose content is invisible-only; each would draw as an
 * empty bubble. Both transcript render paths (ChatPage's inline chain and the
 * app-sdk renderer registry) consult these helpers so they cannot disagree
 * about which rows draw. Mirrors the backend's `preview_text` handling, where
 * the same class of message must yield an empty sidebar preview.
 */
import type { ChatMessage } from '../types'

const FORMAT_CHARS_RE = /\p{Cf}/gu

/** True when `text` renders as nothing: only Cf format chars and whitespace. */
export function isInvisibleOnly(text: string): boolean {
  return text.replace(FORMAT_CHARS_RE, '').trim() === ''
}

/**
 * True for an assistant transcript row that must not be drawn: its content is
 * invisible-only, no regeneration variant holds visible content (hiding the
 * row would strand the variant switcher and the visible predecessor), and it
 * carries no file-change chips (chips are real content even when the text is
 * not). Streaming rows are exempt — a live turn's text arrives incrementally
 * and the row hosts the typing indicator. A hidden row's `turn_stats` are
 * deliberately dropped with it: a say-nothing cycle's footer stats are noise,
 * and cost accounting lives in the usage panels, not the transcript.
 */
export function isHiddenInvisibleAssistantRow(
  m: Pick<ChatMessage, 'role' | 'content' | 'meta' | 'variants'>,
): boolean {
  if (m.role !== 'assistant') return false
  const changes = m.meta?.file_changes
  if (Array.isArray(changes) && changes.length > 0) return false
  if (m.variants?.some(v => !isInvisibleOnly(v.content))) return false
  return isInvisibleOnly(m.content)
}
