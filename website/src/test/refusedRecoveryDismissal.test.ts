/** A duplicate warning dismissed (or typed into) must stay dismissed, even when the durable write
 *  was REFUSED.
 *
 *  Both lookups that record the user's answer read the durable store only, so a quota-refused record
 *  answered `undefined` and the answer was never written. The record survives in the session
 *  fallback, so after a reload the warning came back on a send the user had already settled.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  setPaneRecoveryFor, loadPaneRecoveryById, loadRefusedRecovery, PANE_RECOVERY_KEY,
} from '../utils/chatPaneRecovery'

const SLOT = 'slot-refused-dismiss'
const SEND = 's-refused-dismiss'

/** The pane's own settlement lookup: durable first, then the refused fallback. */
const settlementRecord = (slot: string, id?: string) =>
  id ? (loadPaneRecoveryById(slot, id) ?? loadRefusedRecovery(slot, id)) : undefined

/** The defective form both sites used before the fix. */
const durableOnly = (slot: string, id?: string) =>
  id ? loadPaneRecoveryById(slot, id) : undefined

describe('a refused recovery still answers the lookups that record a dismissal', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => vi.restoreAllMocks())

  const parkRefused = () => {
    const realSet = localStorage.setItem.bind(localStorage)
    vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k.startsWith(PANE_RECOVERY_KEY)) throw new Error('QuotaExceededError')
      realSet(k, v)
    })
    const durable = setPaneRecoveryFor(SLOT, {
      text: 'the unconfirmed send', files: [], sendId: SEND, mayDuplicate: true,
    })
    vi.restoreAllMocks()
    return durable
  }

  it('finds the refused record the durable-only lookup misses', () => {
    expect(parkRefused(), 'premise: the durable write was refused').toBe(false)
    expect(loadRefusedRecovery(SLOT, SEND)?.text,
      'premise: the payload survives in the session fallback').toBe('the unconfirmed send')

    expect(durableOnly(SLOT, SEND),
      'premise: this is what the two sites saw -- nothing to record an answer against').toBeUndefined()

    expect(settlementRecord(SLOT, SEND)?.sendId,
      'the dismissal and the edit-carry both need this record, or the warning returns on reload')
      .toBe(SEND)
  })

  it('still prefers the DURABLE record when the write landed', () => {
    // Positive control: reading the fallback first would serve a stale copy after a successful write.
    expect(setPaneRecoveryFor(SLOT, { text: 'durably parked', files: [], sendId: SEND }),
      'premise: this write is durable').toBe(true)

    expect(settlementRecord(SLOT, SEND)?.text,
      'the durable store is authoritative whenever it holds the record').toBe('durably parked')
  })

  it('routes the dismissal and edit-carry SITES through the fallback, not the durable store alone', () => {
    // The behavioural tests above exercise the helper, which already worked -- the defect was two CALL
    // SITES not using it, so only reading them can tell the fixed tree from the unfixed one.
    const src = readFileSync(resolve(__dirname, '../components/ChatPane.tsx'), 'utf8')
    const dismissal = src.match(/^.*strandedSends\.current\.get\(slot\) \?\?.*$/m)?.[0] ?? ''
    const editCarry = src.match(/^.*armed\.sendId \?.*$/m)?.[0] ?? ''

    expect(dismissal, 'premise: the dismissal lookup was located').toContain('??')
    expect(editCarry, 'premise: the edit-carry lookup was located').toContain('armed.sendId')

    expect(dismissal,
      'a dismissal recorded against a refused record must still find it').toContain('settlementRecord')
    expect(editCarry,
      'and the edit must carry the refused record\u2019s generation forward').toContain('settlementRecord')
  })

  it('answers undefined for a send nothing was parked for', () => {
    // Second positive control: a lookup that returned any record would satisfy the assertions above.
    parkRefused()

    expect(settlementRecord(SLOT, 's-never-parked'),
      'an unrelated send must not inherit another send\u2019s record').toBeUndefined()
  })
})
