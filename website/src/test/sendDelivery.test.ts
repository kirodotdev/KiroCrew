/**
 * Only an immediate dispatch is a receipt for the optimistic bubble.
 *
 * A `queued` response cannot stand in for one, in two independent ways. The
 * chat handler's busy branch queues only a NON-EMPTY message
 * (`if message: slot.queue_append(...)`) while answering `{ok: true, queued:
 * true}` either way, so a file-only send that races into it is dropped behind a
 * success-shaped body. And when it does queue, it broadcasts `queue_push` -- that
 * card is the server-owned representation of the message, so the bubble becomes a
 * duplicate whose fate diverges from the row's: cancelling the queued message
 * removes the card and leaves the bubble.
 *
 * Either way "confirmed" would be a claim about a message that never ran, which
 * is precisely what the 30s indicator is there to question.
 */
import { describe, it, expect } from 'vitest'
import { confirmedDelivered, sendChatReceipt } from '../utils/sendDelivery'

describe('confirmedDelivered', () => {
  it('accepts an immediate dispatch', () => {
    expect(confirmedDelivered({ ok: true })).toBe(true)
  })

  it('REFUSES a queued acceptance (nothing queued for an empty message; cancellable when it is)', () => {
    // The busy branch sets BOTH flags, so a predicate that reads `ok` alone
    // calls this delivered.
    expect(confirmedDelivered({ ok: true, queued: true })).toBe(false)
    expect(confirmedDelivered({ queued: true })).toBe(false)
  })

  it('refuses a rejection', () => {
    expect(confirmedDelivered({ ok: false })).toBe(false)
    expect(confirmedDelivered({})).toBe(false)
  })
})

describe('sendChatReceipt', () => {
  it('normalizes immediate and queued acceptances', async () => {
    await expect(sendChatReceipt(new Response(JSON.stringify({ ok: true })))).resolves.toMatchObject({
      accepted: true, ok: true, queued: false, refused: false, readable: true,
    })
    await expect(sendChatReceipt(new Response(JSON.stringify({ ok: true, queued: true })))).resolves.toMatchObject({
      accepted: true, ok: true, queued: true, refused: false, readable: true,
    })
  })

  it('normalizes an explicit refusal and its error', async () => {
    await expect(sendChatReceipt(new Response(JSON.stringify({ ok: false, error: 'expired' })))).resolves.toMatchObject({
      accepted: false, refused: true, readable: true, error: 'expired',
    })
  })

  it('does not turn an unreadable receipt into an explicit refusal', async () => {
    await expect(sendChatReceipt(new Response('{'))).resolves.toMatchObject({
      accepted: false, refused: false, readable: false,
    })
  })
})
