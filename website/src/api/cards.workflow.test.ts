/**
 * A workflow card that saved into the library refreshes the Workflows tab's list,
 * which is cached without expiry, wherever its `card_update` frame lands.
 */
import { describe, it, expect, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { applyCardUpdate, WORKFLOW_DEFINITIONS_QUERY_KEY, wroteWorkflowLibrary, type Card } from './cards'

const card = (kind: string, status: Card['status']): Card => ({
  id: 'c1', slot_key: 'chat-1', kind, revision: 1, status, risk: 'code_exec', title: 't',
  changes: [], editable: [], params: {}, plan: { apply: [], undo: null },
  created_at: 0, expires_at: 0,
})

describe('workflow library refresh', () => {
  it('counts only applied or partial workflow cards as a library write', () => {
    expect(wroteWorkflowLibrary(card('workflow.save', 'applied'))).toBe(true)
    expect(wroteWorkflowLibrary(card('workflow.update', 'partial'))).toBe(true)
    expect(wroteWorkflowLibrary(card('workflow.save', 'pending'))).toBe(false)
    expect(wroteWorkflowLibrary(card('workflow.save', 'cancelled'))).toBe(false)
    expect(wroteWorkflowLibrary(card('setting.change', 'applied'))).toBe(false)
  })

  it('re-reads the library list when an applied workflow card arrives', () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    applyCardUpdate(qc, card('setting.change', 'applied'))
    applyCardUpdate(qc, card('workflow.save', 'pending'))
    expect(spy).not.toHaveBeenCalled()
    applyCardUpdate(qc, card('workflow.save', 'applied'))
    expect(spy).toHaveBeenCalledWith({ queryKey: WORKFLOW_DEFINITIONS_QUERY_KEY })
  })
})
