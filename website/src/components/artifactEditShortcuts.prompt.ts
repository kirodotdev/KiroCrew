/**
 * The one-click edit shortcuts of a document artifact: which kinds offer them,
 * the preset instruction each one sends, and the passage it applies to. A
 * `*.prompt.ts` module, so the instruction text stays English by design: the
 * agent reads it, and the visible menu labels live in the i18n catalogs.
 *
 * Each shortcut is one turn in the artifact's companion chat: this instruction
 * travels as background context, and the chat shows only a short visible line
 * in the reader's language. The agent changes the artifact through its normal
 * `artifact_update`, which keeps the version history, so nothing here writes to
 * the artifact itself.
 */
import { artifactReferencePrompt } from './artifactReference.prompt'
import type { Artifact } from '../types'

export type EditShortcutId = 'shorter' | 'longer' | 'simpler' | 'proofread' | 'tone_formal' | 'tone_casual'

/** The artifact kinds that read as a document. Widgets, apps, images, JSON and
 *  SVG are not prose, so a rewrite shortcut would be the wrong offer there. */
const EDIT_SHORTCUT_KINDS: ReadonlyArray<Artifact['kind']> = ['markdown', 'text', 'html']

/** A longer selection is sent as the whole document, so one message stays small. */
const MAX_QUOTE_LENGTH = 4000

const INSTRUCTIONS: Record<EditShortcutId, (target: string) => string> = {
  shorter: (t) => `Make ${t} shorter. Keep every key point and cut repetition and filler.`,
  longer: (t) => `Make ${t} longer. Expand the thin points with useful detail and add nothing off topic.`,
  simpler: (t) => `Rewrite ${t} in simpler wording. Use short sentences and everyday words, and keep the meaning.`,
  proofread: (t) => `Proofread ${t}. Fix spelling, grammar and punctuation only, and keep the wording and meaning.`,
  tone_formal: (t) => `Rewrite ${t} in a more formal tone, and keep the meaning.`,
  tone_casual: (t) => `Rewrite ${t} in a more casual, conversational tone, and keep the meaning.`,
}

export function supportsEditShortcuts(kind: Artifact['kind'] | undefined): boolean {
  return kind !== undefined && EDIT_SHORTCUT_KINDS.includes(kind)
}

/** The selected text a shortcut applies to, or '' for the whole document: blank
 *  and over-long selections are dropped, never refused. */
export function usableEditQuote(text: string | undefined): string {
  const quote = (text ?? '').trim()
  return quote.length > 0 && quote.length <= MAX_QUOTE_LENGTH ? quote : ''
}

/** The longest stretch of the passage the menu quotes back to the reader. */
const PREVIEW_LENGTH = 60

/** The start of the passage on one line, for the menu's scope label: whitespace runs
 *  (line breaks included) become single spaces, the cut falls between words, and a
 *  cut ends in an ellipsis. A first word longer than the limit is cut inside it. */
export function editQuotePreview(quote: string, limit: number = PREVIEW_LENGTH): string {
  const words: string[] = []
  let word = ''
  for (const ch of quote) {
    if (ch.trim() === '') {
      if (word) { words.push(word); word = '' }
    } else {
      word += ch
    }
  }
  if (word) words.push(word)
  let out = ''
  for (let i = 0; i < words.length; i += 1) {
    const next = out ? `${out} ${words[i]}` : words[i]
    if (Array.from(next).length <= limit) { out = next; continue }
    if (!out) out = Array.from(words[i]).slice(0, limit).join('')
    return `${out}…`
  }
  return out
}

/** The hidden instruction a shortcut hands the companion chat as background
 *  context; its first sentence ties it to the short visible line sent with it. */
export function editShortcutPrompt(id: EditShortcutId, name: string, slug: string, quote: string): string {
  const target = quote ? 'the selected passage' : 'the whole document'
  const scope = quote
    ? ` The selected passage, as a JSON string: ${JSON.stringify(quote)}. Change only that passage and leave the rest as it is.`
    : ''
  return "The user's next message is a Quick edit picked on the artifact page."
    + ` ${artifactReferencePrompt(name, slug)} ${INSTRUCTIONS[id](target)}${scope}`
    + ' Read the current version with artifact_get, apply the change, and save it with artifact_update.'
    + ' Do not add commentary to the document.'
}
