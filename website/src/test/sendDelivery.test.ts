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
import { describe, it, expect, vi } from 'vitest'
import { confirmedDelivered, readSendReceipt } from '../utils/sendDelivery'

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

/**
 * A receipt that could not be READ is not a receipt that said no (#4217).
 *
 * Every send path used to fold an unparsed body into `{}` and then test it for
 * the acceptance flags, so a truncated reply to an ACCEPTED post answered the
 * same as an explicit refusal — the user was told the send failed and handed the
 * payload back to retry, duplicating a turn that had gone out. The status line
 * is what survives a mangled response, so it decides: a non-2xx is a refusal
 * with or without a body, and a 2xx that will not parse is `unknown`.
 */
describe('readSendReceipt', () => {
  const res = (ok: boolean, json: () => Promise<unknown>) => ({ ok, json })

  it('reads an accepted receipt, immediate or queued', async () => {
    expect(await readSendReceipt(res(true, async () => ({ ok: true })))).toEqual({
      body: { ok: true }, outcome: 'accepted',
    })
    expect((await readSendReceipt(res(true, async () => ({ ok: true, queued: true })))).outcome).toBe('accepted')
    expect((await readSendReceipt(res(true, async () => ({ queued: true })))).outcome).toBe('accepted')
  })

  it('reads an explicit refusal, and keeps the reason the server sent', async () => {
    const receipt = await readSendReceipt(res(false, async () => ({ ok: false, error: 'slot agent mismatch' })))
    expect(receipt.outcome).toBe('refused')
    expect(receipt.body.error).toBe('slot agent mismatch')
  })

  it('refuses a 2xx body that parsed but claims neither flag', async () => {
    // The server answered, and what it said was no. Nothing was sent, so the
    // payload is safe to hand back.
    expect((await readSendReceipt(res(true, async () => ({})))).outcome).toBe('refused')
    expect((await readSendReceipt(res(true, async () => ({ ok: false })))).outcome).toBe('refused')
  })

  it('refuses a NON-2xx with no readable body at all', async () => {
    // An unhandled backend 500 answers in HTML and a proxy 502 in its own error
    // page; neither parses. The status is the whole receipt, and it says no —
    // which is what these paths have always reported for it.
    expect((await readSendReceipt(res(false, () => Promise.reject(new Error('not json'))))).outcome).toBe('refused')
    expect((await readSendReceipt(res(false, async () => '<html>502</html>'))).outcome).toBe('refused')
  })

  it('calls a 2xx with an unreadable body UNKNOWN, never refused', async () => {
    // The defect this exists to prevent: the request WAS accepted and only its
    // answer is truncated, so the message may well have been delivered.
    expect((await readSendReceipt(res(true, () => Promise.reject(new Error('unexpected end of JSON'))))).outcome).toBe('unknown')
  })

  it('leaves a diagnostic trail on the branch that shows the user nothing', async () => {
    // `unknown` is deliberately silent on screen, so the console line is the
    // only thing that makes a receipt-mangling proxy discoverable at all.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await readSendReceipt(res(true, () => Promise.reject(new Error('unexpected end of JSON'))))
      expect(warn).toHaveBeenCalledTimes(1)
      // ...and NOT on the outcomes the user can already see.
      warn.mockClear()
      await readSendReceipt(res(true, async () => ({ ok: true })))
      await readSendReceipt(res(false, async () => ({ ok: false })))
      expect(warn).not.toHaveBeenCalled()
    } finally {
      warn.mockRestore()
    }
  })

  it('treats a non-object JSON body as unreadable rather than as absent flags', async () => {
    // `null`, an array and a bare string carry no receipt either. Reading the
    // acceptance flags off one would call a 200 a refusal for the same wrong
    // reason a truncated body did.
    for (const value of [null, [1, 2], 'accepted', 7] as unknown[]) {
      const receipt = await readSendReceipt(res(true, async () => value))
      expect(receipt.outcome).toBe('unknown')
      expect(receipt.body).toEqual({})
    }
  })
})
