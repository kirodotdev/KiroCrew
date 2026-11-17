/**
 * The text of a user row, read from the row itself: the body the bubble draws
 * from, and what the Pins tab's Put in composer puts back in the composer.
 */
import { parseSessionSeed } from '../../lib/crewSeed'
import { readRelay, withoutRelayEnvelope } from './sessionRelay'
import { type PasteBlock, expandAll as expandPasteTokens } from '../../utils/pasteTokens'

/** The row's collapsed pastes; anything on `meta.pastes` that is not a well-formed block is ignored. */
export function pasteBlocksOf(meta: Record<string, unknown> | undefined): PasteBlock[] {
  return (Array.isArray(meta?.pastes) ? meta.pastes : []).filter((b): b is PasteBlock =>
    !!b && typeof b === 'object' && typeof (b as PasteBlock).seq === 'number' && typeof (b as PasteBlock).content === 'string')
}

/**
 * The row's text without another session's delivery line (the bubble names the
 * sender instead; a person's own row keeps its text as typed). A carried
 * quote's `>` block stays: the bubble strips it to draw the quote card.
 */
export function unseededUserText(content: string, meta: Record<string, unknown> | undefined): string {
  const seed = parseSessionSeed(content, meta)
  return (!meta?.human ? withoutRelayEnvelope(content, readRelay(meta)) : null) ?? (seed ? seed.body : content)
}

/** The message as the person sent it, ready to send again: collapsed pastes expanded, a carried quote kept. */
export function userResendText(content: string, meta: Record<string, unknown> | undefined): string {
  const text = unseededUserText(content, meta)
  const pastes = pasteBlocksOf(meta)
  return pastes.length ? expandPasteTokens(text, pastes) : text
}
