/** `sideClosedElsewhere`: the mark that another tab closed this slot's side
 *  conversation lives exactly as long as the slot stays closed after that close. */
import { describe, expect, it } from 'vitest'
import reducer, { sideClose, sideClosedElsewhere, sideOptimisticAppend, sideSendStatus, sseSideResult } from '../chatSlice'

const SLOT = 'mark-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const closedByAnotherTab = reducer(reducer(initial, sideClose(SLOT)), sideClosedElsewhere(SLOT))

describe('sideClosedElsewhere', () => {
  it('marks a slot sideClose closed', () => {
    expect(closedByAnotherTab.slotSideClosedElsewhere?.[SLOT]).toBe(true)
  })

  it('marks nothing on a slot that is not closed', () => {
    const open = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))

    expect(reducer(open, sideClosedElsewhere(SLOT)).slotSideClosedElsewhere?.[SLOT]).toBeUndefined()
  })

  it('is cleared by this tab closing the slot itself', () => {
    expect(reducer(closedByAnotherTab, sideClose(SLOT)).slotSideClosedElsewhere?.[SLOT]).toBeUndefined()
  })

  it('is cleared when a question from any tab opens a new conversation', () => {
    const reopened = reducer(closedByAnotherTab, sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'q2', message_id: 'u2' }))

    expect(reopened.slotSideClosed[SLOT]).toBeUndefined()
    expect(reopened.slotSideClosedElsewhere?.[SLOT]).toBeUndefined()
  })

  it('is cleared when this tab sends into the slot', () => {
    const sent = reducer(closedByAnotherTab, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'q2', ts: '2026-10-26T00:00:00.000Z' },
    }))

    expect(sent.slotSideClosedElsewhere?.[SLOT]).toBeUndefined()
  })

  it('gives way to a send status that names this slot', () => {
    const failed = reducer(closedByAnotherTab, sideSendStatus({ slot: SLOT, error: 'the send failed' }))

    expect(failed.slotSide[SLOT]?.sendStatus?.error).toBe('the send failed')
    expect(failed.slotSideClosedElsewhere?.[SLOT]).toBeUndefined()
  })

  it('stays through a late answer from the closed conversation', () => {
    const late = reducer(closedByAnotherTab, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'late', final: true }))

    expect(late.slotSide[SLOT]).toBeUndefined()
    expect(late.slotSideClosedElsewhere?.[SLOT]).toBe(true)
  })
})
