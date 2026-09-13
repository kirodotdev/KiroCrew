import { describe, it, expect } from 'vitest'
import reducer, { setSessionCloseFailure, slotsSnapshotApplied } from '../store/chatSlice'

const withUnknownNotice = () =>
  reducer(undefined, setSessionCloseFailure({ kind: 'unknown', key: 'chat-9', title: 'Draft' }))

describe('an unknown close notice reports the end of the wait', () => {
  it('becomes a confirmation when the pop is confirmed, not silence', () => {
    const state = withUnknownNotice()
    const next = reducer(state, slotsSnapshotApplied([{ key: 'chat-other' }]))
    expect(next.sessionCloseFailure).not.toBeNull()
    expect(next.sessionCloseFailure?.kind).toBe('confirmed')
    expect(next.sessionCloseFailure?.title).toBe('Draft')
  })

  it('stays up while the row is still listed', () => {
    const state = withUnknownNotice()
    const next = reducer(state, slotsSnapshotApplied([{ key: 'chat-9' }]))
    expect(next.sessionCloseFailure?.kind).toBe('unknown')
  })
})
