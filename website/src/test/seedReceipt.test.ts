/**
 * `settleSeedReceipt` -- the one seed-receipt policy both app seeders call.
 *
 * Pins the mapping and, for the two no-receipt statuses, the bounded slot
 * probe: an empty slot is re-read `SEED_PROBE_ATTEMPTS` times with
 * `SEED_PROBE_GAP_MS` between reads so a POST that reaches the gateway after
 * the transport's deadline still gets counted, and only sustained emptiness
 * rules the seed never-landed. A gone slot short-circuits (nothing can be
 * running in it); a probe the server cannot answer reads as "may be running".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SendReceipt } from '../chat-core/transport/sendTurn'
import { i18nT } from '../i18n/t'

const { chatSlotDetail } = vi.hoisted(() => ({ chatSlotDetail: vi.fn() }))
vi.mock('../api/client', () => ({ api: { chatSlotDetail } }))

import { SEED_PROBE_ATTEMPTS, SEED_PROBE_GAP_MS, settleSeedReceipt } from '../utils/seedReceipt'

const receipt = (status: SendReceipt['status'], reason?: string): SendReceipt =>
  ({ status, body: {}, ...(reason ? { reason } : {}) })

const couldNotStart = String(i18nT('pages.chatPage.could_not_start_a_new_session'))

beforeEach(() => {
  chatSlotDetail.mockReset()
  vi.useFakeTimers()
})
afterEach(() => vi.useRealTimers())

describe('settleSeedReceipt', () => {
  it.each(['dispatched', 'queued', 'unknown'] as const)('`%s` ran, without touching the slot', async (status) => {
    await expect(settleSeedReceipt(receipt(status), 'slot-1')).resolves.toEqual({ ran: true })
    expect(chatSlotDetail).not.toHaveBeenCalled()
  })

  it('`refused` never ran, carrying the server reason', async () => {
    await expect(settleSeedReceipt(receipt('refused', 'slot is stopping'), 'slot-1')).resolves.toEqual({
      ran: false, reason: `${couldNotStart} (slot is stopping)`,
    })
    expect(chatSlotDetail).not.toHaveBeenCalled()
  })

  it('`refused` with no reason falls back to the core copy alone -- never a raw status', async () => {
    await expect(settleSeedReceipt(receipt('refused'), 'slot-1')).resolves.toEqual({ ran: false, reason: couldNotStart })
  })

  describe.each(['transport-error', 'response-late'] as const)('`%s` asks the slot', (status) => {
    it('ran when the slot already holds the seed', async () => {
      chatSlotDetail.mockResolvedValue({ total: 1, messages: [{ role: 'user' }] })
      await expect(settleSeedReceipt(receipt(status), 'slot-1')).resolves.toEqual({ ran: true })
      expect(chatSlotDetail).toHaveBeenCalledTimes(1)
      expect(chatSlotDetail).toHaveBeenCalledWith('slot-1', 1)
    })

    it('ran when the probe itself cannot be answered -- never cancel or orphan what may be running', async () => {
      chatSlotDetail.mockRejectedValue(new Error('HTTP 503'))
      await expect(settleSeedReceipt(receipt(status), 'slot-1')).resolves.toEqual({ ran: true })
      expect(chatSlotDetail).toHaveBeenCalledTimes(1)
    })

    it('never ran, at once, when the slot is gone', async () => {
      chatSlotDetail.mockRejectedValue({ status: 404, message: 'not found' })
      await expect(settleSeedReceipt(receipt(status), 'slot-1')).resolves.toEqual({ ran: false, reason: couldNotStart })
      expect(chatSlotDetail).toHaveBeenCalledTimes(1)
    })

    it('re-reads an empty slot a bounded number of times before ruling never-ran', async () => {
      chatSlotDetail.mockResolvedValue({ total: 0, messages: [] })
      const settled = settleSeedReceipt(receipt(status), 'slot-1')
      await vi.advanceTimersByTimeAsync(SEED_PROBE_GAP_MS * SEED_PROBE_ATTEMPTS)
      await expect(settled).resolves.toEqual({ ran: false, reason: couldNotStart })
      expect(chatSlotDetail).toHaveBeenCalledTimes(SEED_PROBE_ATTEMPTS)
    })

    it('counts a seed that lands during the re-reads as ran', async () => {
      chatSlotDetail
        .mockResolvedValueOnce({ total: 0, messages: [] })
        .mockResolvedValueOnce({ total: 1, messages: [{ role: 'user' }] })
      const settled = settleSeedReceipt(receipt(status), 'slot-1')
      await vi.advanceTimersByTimeAsync(SEED_PROBE_GAP_MS)
      await expect(settled).resolves.toEqual({ ran: true })
      expect(chatSlotDetail).toHaveBeenCalledTimes(2)
    })

    it('reads message rows when the detail carries no total', async () => {
      chatSlotDetail.mockResolvedValue({ messages: [{ role: 'user' }] })
      await expect(settleSeedReceipt(receipt(status), 'slot-1')).resolves.toEqual({ ran: true })
    })
  })
})
