/**
 * `rowDeliveryState` is the one place a row's delivery markers are resolved, so
 * these tests pin the precedence rather than any one caller's rendering.
 *
 * The case that matters is the ILLEGAL-LOOKING one: nothing clears
 * `deliveryUnknown`/`deliveryUnresolved` when a send later confirms, so a row can
 * carry confirmation AND doubt at once. Confirmation has to win, or a delivered
 * send renders muted while the composer caption beside it reads "delivered".
 */
import { describe, it, expect } from 'vitest'
import { confirmsSend, rowDeliveryState } from './sendDelivery'

describe('rowDeliveryState', () => {
  it('reports nothing for a row carrying no markers', () => {
    expect(rowDeliveryState(undefined)).toBe('none')
    expect(rowDeliveryState({})).toBe('none')
  })

  it('reports live doubt for an unreadable receipt', () => {
    expect(rowDeliveryState({ deliveryUnknown: true })).toBe('unknown')
  })

  it('reports spent doubt for a demoted send', () => {
    expect(rowDeliveryState({ deliveryUnresolved: true })).toBe('spent')
  })

  it('lets confirmation beat a doubt marker nothing cleared', () => {
    expect(rowDeliveryState({ deliveryUnknown: true, deliveryConfirmed: true })).toBe('none')
    expect(rowDeliveryState({ deliveryUnresolved: true, deliveryConfirmed: true })).toBe('none')
    expect(rowDeliveryState({ deliveryUnknown: true, deliveryUnresolved: true, deliveryConfirmed: true })).toBe('none')
  })

  it('prefers LIVE doubt over spent when both are set', () => {
    // A row demoted and then re-doubted must read as live: the past-tense caption
    // would understate a send that can still duplicate a turn.
    expect(rowDeliveryState({ deliveryUnknown: true, deliveryUnresolved: true })).toBe('unknown')
  })

  it('ignores a non-true truthy value, matching the strict marker contract', () => {
    expect(rowDeliveryState({ deliveryUnknown: 'yes' })).toBe('none')
    expect(rowDeliveryState({ deliveryConfirmed: 1, deliveryUnknown: true })).toBe('unknown')
  })
})

describe('confirmsSend — a drained merge names the send in the array, not the scalar', () => {
  it('recognises a sendId carried only in sendIds', () => {
    // The server merges rows and the scalar keeps the LAST writer, so a caption armed on the
    // earlier send stays up and invites a duplicate turn unless the array is read too.
    const merged = { deliveryConfirmed: true, sendId: 'B', sendIds: ['A', 'B'] }
    expect(confirmsSend(merged, 'A'), 'the merged-away send is still confirmed').toBe(true)
    expect(confirmsSend(merged, 'B')).toBe(true)
    expect(confirmsSend(merged, 'C')).toBe(false)
  })

  it('still requires the row to claim confirmation at all', () => {
    expect(confirmsSend({ sendIds: ['A'] }, 'A')).toBe(false)
  })
})


describe('confirmsSend — a server-fetched row is proof a reload can still read', () => {
  it('accepts a fetched row naming the send, without the client-only flag', () => {
    // `deliveryConfirmed` lives only in this tab's store, so after a reload the caption reverted
    // to the resend hedge over a message the fetched transcript itself shows delivered.
    const fetched = { mid: 'm-server-1', sendId: 's-1' }
    expect(confirmsSend(fetched, 's-1'), 'a server row is its own proof').toBe(true)
  })

  it('still requires the row to NAME this send', () => {
    expect(confirmsSend({ mid: 'm-server-1', sendId: 's-other' }, 's-1')).toBe(false)
    expect(confirmsSend({ mid: 'm-server-1' }, 's-1')).toBe(false)
  })

  it('keeps accepting the client flag when there is no server id yet', () => {
    expect(confirmsSend({ deliveryConfirmed: true, sendId: 's-1' }, 's-1')).toBe(true)
    expect(confirmsSend({ deliveryConfirmed: true, sendIds: ['s-1', 's-2'] }, 's-1')).toBe(true)
  })

  it('does not accept an unconfirmed CLIENT row, which carries no mid', () => {
    expect(confirmsSend({ sendId: 's-1', optimistic: true }, 's-1')).toBe(false)
    expect(confirmsSend({ sendId: 's-1', mid: '' }, 's-1')).toBe(false)
  })
})
