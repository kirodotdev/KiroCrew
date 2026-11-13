/**
 * `rowPhase` answers, from the per-row approval marks together, where a listed
 * row stands: still decidable, retired, or decided with a failed DELETE.
 */
import { describe, it, expect } from 'vitest'
import reducer, { approvalDecisionBegan, rowPhase, type ApprovalRowPhase } from './notificationsSlice'

type State = ReturnType<typeof reducer>

describe('rowPhase', () => {
  it.each<[string, Partial<State>, ApprovalRowPhase]>([
    ['no mark, as in state rehydrated without the maps', {}, { phase: 'live', endedFailure: null, dismissFailed: false }],
    ['a mark on another row only', { retiredApprovals: { other: true }, dismissFailed: { other: 'decided' } },
      { phase: 'live', endedFailure: null, dismissFailed: false }],
    ['refused as no longer pending', { retiredApprovals: { r: true } },
      { phase: 'retired', endedFailure: null, dismissFailed: false }],
    ['failed after a frame ended its request', { retiredApprovals: { r: { failed: 'gateway busy' } } },
      { phase: 'retired', endedFailure: 'gateway busy', dismissFailed: false }],
    ['failed after its request ended, with nothing to quote', { retiredApprovals: { r: { failed: '' } } },
      { phase: 'retired', endedFailure: '', dismissFailed: false }],
    ['decided, its DELETE refused', { dismissFailed: { r: 'decided' } },
      { phase: 'decided', endedFailure: null, dismissFailed: true }],
    ['retired, and a later DELETE refused', { retiredApprovals: { r: true }, dismissFailed: { r: 'decided' } },
      { phase: 'retired', endedFailure: null, dismissFailed: true }],
  ])('%s', (_name, marks, expected) => {
    expect(rowPhase(marks, 'r')).toEqual(expected)
  })

  it('a row with a decision in flight is still live', () => {
    const state = reducer({ items: [{ ts: 'r', kind: 'approval', title: 'Approve?', body: '' }], clearSeq: 0 }, approvalDecisionBegan('r'))
    expect(state.approvalDecisions?.r?.inFlight).toBe(1)
    expect(rowPhase(state, 'r')).toEqual({ phase: 'live', endedFailure: null, dismissFailed: false })
  })
})
