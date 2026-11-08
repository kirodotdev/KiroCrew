/**
 * A peer row has no local key, so the sidebar looks its workflow activity up
 * under the empty string. A run whose session key is a bare `dashboard_` (or
 * `dashboard:`) also normalizes to the empty string, and without a guard that
 * run would mark every peer row active.
 */
import { describe, expect, it } from 'vitest'
import type { RootState } from '../store'
import { selectSidebarWorkflowActive, selectSidebarWorkflowActiveKeys } from '../store/chatSlice'

function stateWith(runs: Record<string, { run_id: string; status: string; sessionKey?: string; name?: string }>): RootState {
  return { chat: { workflowRuns: runs } } as unknown as RootState
}

describe('selectSidebarWorkflowActive', () => {
  it('ignores a run whose session key normalizes to the empty key', () => {
    const state = stateWith({
      a: { run_id: 'a', status: 'running', sessionKey: 'dashboard_' },
      b: { run_id: 'b', status: 'running', sessionKey: 'dashboard:' },
    })
    expect(selectSidebarWorkflowActive(state)['']).toBeUndefined()
    expect(selectSidebarWorkflowActiveKeys(state)).toEqual([])
  })

  it('still indexes a real session key', () => {
    const state = stateWith({
      a: { run_id: 'a', status: 'running', sessionKey: 'dashboard:chat-1', name: 'Research' },
      b: { run_id: 'b', status: 'running', sessionKey: 'dashboard_' },
    })
    expect(selectSidebarWorkflowActive(state)['chat-1']).toEqual({ count: 1, name: 'Research', phase: '' })
    expect(selectSidebarWorkflowActiveKeys(state)).toEqual(['chat-1'])
  })
})
