/**
 * The merge-back wire: a fork's summary drafted, then written into its parent.
 *
 * `POST /api/chat/slots/{slot}/merge-back/draft` returns a {@link MergeBackDraft}
 * and writes nothing; `POST /api/chat/slots/{slot}/merge-back` writes the
 * reviewed text into the parent and returns a {@link MergeBackResult}. The card
 * it leaves in the parent is a note row whose `meta.mergedFrom` is a
 * {@link MergedFromBlock}.
 */

export interface MergeBackDraft {
  ok: true
  /** The drafted note. Empty when the new messages hold no text to summarize. */
  summary: string
  /** Id of the last fork message the draft covers; sent back with the merge. */
  through: string
  /** Fingerprint of the fork messages the draft covers; sent back with the merge,
   *  which is refused when they changed in the meantime. */
  digest: string
  /** How many fork messages the draft covers. */
  messages: number
  /** How many fork messages the draft leaves for a later merge. */
  remaining: number
  /** The gateway cut the summary to the merge limit. A cut ends in `…`, but so
   *  can a summary the model wrote, so the text alone cannot say. */
  trimmed: boolean
  /** The parent's slot key. */
  parent: string
}

export interface MergeBackResult {
  ok: true
  parent: string
  messages: number
  /** The parent was mid-turn, so the card lands when that turn ends. */
  deferred: boolean
}

/** The `meta.mergedFrom` block a merge card carries in its parent. */
export interface MergedFromBlock {
  /** The fork's session key. */
  session: string
  /** The fork's slot key, for opening it. */
  slot: string
  /** The fork's title when it was merged. */
  title: string
  /** The fork transcript's creation identity, as the merge recorded it. */
  createdAt: string
  through: string
  messages: number
}

/** A row's merge block, or null when the row is not a well-formed merge card.
 *  The block comes from a transcript file, so its shape is checked before use. */
export function mergedFromOf(meta: unknown): MergedFromBlock | null {
  if (!meta || typeof meta !== 'object') return null
  const raw = (meta as Record<string, unknown>).mergedFrom
  if (!raw || typeof raw !== 'object') return null
  const block = raw as Record<string, unknown>
  const { session, slot, title, createdAt, through, messages } = block
  if (typeof session !== 'string' || !session) return null
  if (typeof slot !== 'string' || !slot) return null
  if (typeof title !== 'string') return null
  if (typeof createdAt !== 'string' || createdAt.length > 128) return null
  if (typeof through !== 'string' || !through) return null
  if (typeof messages !== 'number' || !Number.isInteger(messages) || messages < 1) return null
  return { session, slot, title, createdAt, through, messages }
}
