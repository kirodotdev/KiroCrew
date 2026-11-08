import { afterEach, describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { clearCrewSteerNotice, noteCrewSteerReceipt, useCrewSteerNotice } from './crewSteerNotice'

// Hooks are still mounted here: the clear is a store update they see.
afterEach(() => act(() => { clearCrewSteerNotice('w1'); clearCrewSteerNotice('w2') }))

describe('crewSteerNotice', () => {
  it('shows a steer that was delivered but not recorded, per slot, until a recorded one lands', () => {
    const { result } = renderHook(() => useCrewSteerNotice('w1'))
    const other = renderHook(() => useCrewSteerNotice('w2'))
    expect(result.current).toBe('')

    act(() => noteCrewSteerReceipt('w1', { ledger_error: 'ledger offline' }))
    expect(result.current).toBe('ledger offline')
    // Another slot's note is its own.
    expect(other.result.current).toBe('')

    // The same reason again is not a change.
    act(() => noteCrewSteerReceipt('w1', { ledger_error: 'ledger offline' }))
    expect(result.current).toBe('ledger offline')

    // A recorded steer clears it.
    act(() => noteCrewSteerReceipt('w1', { ledger_event: 'ev-1' }))
    expect(result.current).toBe('')
  })

  it('a steer still owed to the ledger clears the note too', () => {
    const { result } = renderHook(() => useCrewSteerNotice('w1'))
    act(() => noteCrewSteerReceipt('w1', { ledger_error: 'boom' }))
    expect(result.current).toBe('boom')
    act(() => noteCrewSteerReceipt('w1', { ledger_pending: true }))
    expect(result.current).toBe('')
  })

  it('a plain send receipt, with neither field, leaves the note alone', () => {
    const { result } = renderHook(() => useCrewSteerNotice('w1'))
    act(() => noteCrewSteerReceipt('w1', { ledger_error: 'boom' }))
    act(() => noteCrewSteerReceipt('w1', { ok: true }))
    expect(result.current).toBe('boom')
    // Nothing a receipt could be is read as one.
    act(() => { noteCrewSteerReceipt('w1', null); noteCrewSteerReceipt('w1', 'text'); noteCrewSteerReceipt('', { ledger_event: 'x' }) })
    expect(result.current).toBe('boom')
    // A non-string or empty ledger_error is not a reason.
    act(() => noteCrewSteerReceipt('w2', { ledger_error: '' }))
    expect(renderHook(() => useCrewSteerNotice('w2')).result.current).toBe('')
  })

  it('the person can dismiss it', () => {
    const { result } = renderHook(() => useCrewSteerNotice('w1'))
    act(() => noteCrewSteerReceipt('w1', { ledger_error: 'boom' }))
    act(() => clearCrewSteerNotice('w1'))
    expect(result.current).toBe('')
    // Clearing an absent note is a no-op.
    act(() => clearCrewSteerNotice('w1'))
    expect(result.current).toBe('')
  })
})
