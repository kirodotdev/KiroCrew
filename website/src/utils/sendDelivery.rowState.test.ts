/**
 * `deliveryInDoubt` is the one place a row's delivery markers are resolved, so
 * these tests pin the precedence rather than any one caller's rendering.
 *
 * The case that matters is the ILLEGAL-LOOKING one: nothing clears
 * `deliveryUnknown` when a send later confirms, so a row can
 * carry confirmation AND doubt at once. Confirmation has to win, or a delivered
 * send renders muted while the composer caption beside it reads "delivered".
 */
import { describe, it, expect } from 'vitest'
import { sendDelivered, deliveryInDoubt } from './sendDelivery'

describe('deliveryInDoubt', () => {
  it('reports nothing for a row carrying no markers', () => {
    expect(deliveryInDoubt(undefined)).toBe(false)
    expect(deliveryInDoubt({})).toBe(false)
  })

  it('reports live doubt for an unreadable receipt', () => {
    expect(deliveryInDoubt({ deliveryUnknown: true })).toBe(true)
  })

  it('resolves a LEGACY spent marker to no doubt, so there are two states only', () => {
    // The spent-doubt state was removed: a row still carrying the retired marker must read as an
    // ordinary prompt rather than reviving a third state for one caption.
    expect(deliveryInDoubt({ deliveryUnresolved: true })).toBe(false)
  })

  it('lets confirmation beat a doubt marker nothing cleared', () => {
    expect(deliveryInDoubt({ deliveryUnknown: true, deliveryConfirmed: true })).toBe(false)
  })

  it('ignores a non-true truthy value, matching the strict marker contract', () => {
    expect(deliveryInDoubt({ deliveryUnknown: 'yes' })).toBe(false)
    expect(deliveryInDoubt({ deliveryConfirmed: 1, deliveryUnknown: true })).toBe(true)
  })
})

describe('sendDelivered — a drained merge names the send in the array, not the scalar', () => {
  it('recognises a sendId carried only in sendIds', () => {
    // The server merges rows and the scalar keeps the LAST writer, so a caption armed on the
    // earlier send stays up and invites a duplicate turn unless the array is read too.
    const merged = { deliveryConfirmed: true, sendId: 'B', sendIds: ['A', 'B'] }
    expect(sendDelivered([{ role: 'user', meta: merged }], 'A'), 'the merged-away send is still confirmed').toBe(true)
    expect(sendDelivered([{ role: 'user', meta: merged }], 'B')).toBe(true)
    expect(sendDelivered([{ role: 'user', meta: merged }], 'C')).toBe(false)
  })

  it('still requires the row to claim confirmation at all', () => {
    expect(sendDelivered([{ role: 'user', meta: { sendIds: ['A'] } }], 'A')).toBe(false)
  })
})


describe('sendDelivered — a server-fetched row is proof a reload can still read', () => {
  it('accepts a fetched row naming the send, without the client-only flag', () => {
    // `deliveryConfirmed` lives only in this tab's store, so after a reload the caption reverted
    // to the resend hedge over a message the fetched transcript itself shows delivered.
    const fetched = { mid: 'm-server-1', sendId: 's-1' }
    expect(sendDelivered([{ role: 'user', meta: fetched }], 's-1'), 'a server row is its own proof').toBe(true)
  })

  it('still requires the row to NAME this send', () => {
    expect(sendDelivered([{ role: 'user', meta: { mid: 'm-server-1', sendId: 's-other' } }], 's-1')).toBe(false)
    expect(sendDelivered([{ role: 'user', meta: { mid: 'm-server-1' } }], 's-1')).toBe(false)
  })

  it('keeps accepting the client flag when there is no server id yet', () => {
    expect(sendDelivered([{ role: 'user', meta: { deliveryConfirmed: true, sendId: 's-1' } }], 's-1')).toBe(true)
    expect(sendDelivered([{ role: 'user', meta: { deliveryConfirmed: true, sendIds: ['s-1', 's-2'] } }], 's-1')).toBe(true)
  })

  it('does not accept an unconfirmed CLIENT row, which carries no mid', () => {
    expect(sendDelivered([{ role: 'user', meta: { sendId: 's-1', optimistic: true } }], 's-1')).toBe(false)
    expect(sendDelivered([{ role: 'user', meta: { sendId: 's-1', mid: '' } }], 's-1')).toBe(false)
  })
})
