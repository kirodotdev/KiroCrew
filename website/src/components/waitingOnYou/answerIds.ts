/** One `answer_id` per click, reused when that click is retried.
 *
 *  The answer route keeps a tombstone per `answer_id`, so a retry that reuses
 *  the id gets the first outcome back and sends nothing. A retry is the same
 *  choice on the same ask, from any surface (the composer stack, the sidebar
 *  card, the Questions tab), so the ids live here rather than in one component.
 *  A settled click forgets its id; so does an `uncertain` refusal, because the
 *  route answers that id with `uncertain` forever and only a fresh id can send. */

const MAX_REMEMBERED = 128
const ids = new Map<string, string>()

const slotFor = (askId: string, choice: string) => [askId, choice].join('\u0000')

function mintAnswerId(): string {
  const c = globalThis.crypto as Crypto | undefined
  const hex = c && typeof c.randomUUID === 'function'
    ? c.randomUUID().replace(/-/g, '')
    : Date.now().toString(36) + Math.random().toString(36).slice(2, 12)
  return `wa${hex}`
}

export function answerIdFor(askId: string, choice: string): string {
  const k = slotFor(askId, choice)
  const known = ids.get(k)
  if (known) return known
  if (ids.size >= MAX_REMEMBERED) {
    const oldest = ids.keys().next().value
    if (oldest !== undefined) ids.delete(oldest)
  }
  const minted = mintAnswerId()
  ids.set(k, minted)
  return minted
}

export function forgetAnswerIds(askId: string): void {
  const prefix = slotFor(askId, '')
  for (const k of [...ids.keys()]) if (k.startsWith(prefix)) ids.delete(k)
}

export function __resetAnswerIdsForTests(): void {
  ids.clear()
}
