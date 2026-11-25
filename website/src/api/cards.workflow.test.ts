/**
 * A workflow card that saved into the library refreshes the Workflows tab's list,
 * which is cached without expiry, wherever its `card_update` frame lands.
 */
import { describe, it, expect, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { applyCardUpdate, cardsQueryKey, WORKFLOW_DEFINITIONS_QUERY_KEY, wroteWorkflowLibrary, type Card } from './cards'
import { MEMORY_QUERY_PREFIXES } from './memoryQueryKeys'

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

describe('memory refresh', () => {
  it.each(['applied', 'partial'] as const)('invalidates memory queries when a %s memory card arrives', status => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
    const recordsKey = ['memory-records', 'scout', '', 0]
    const factsKey = ['member-memory', 'scout', 'facts', '']
    qc.setQueryData(recordsKey, { entries: [{ id: 'old-record' }] })
    qc.setQueryData(factsKey, { pages: [{ entries: [{ id: 'old-fact' }] }] })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const applied = { ...card('crewmate.memory', status), params: { store: 'scout' } }
    try {
      applyCardUpdate(qc, applied)
      expect(spy).toHaveBeenCalledWith({ queryKey: ['memory-records', 'scout'] })
      expect(spy).toHaveBeenCalledWith({ queryKey: ['member-memory', 'scout'] })
      for (const prefix of MEMORY_QUERY_PREFIXES) expect(spy).toHaveBeenCalledWith({ queryKey: prefix })
      expect(qc.getQueryState(recordsKey)?.isInvalidated).toBe(true)
      expect(qc.getQueryState(factsKey)?.isInvalidated).toBe(true)
      expect(qc.getQueryData(cardsQueryKey(applied.slot_key))).toEqual([applied])
      expect(spy).not.toHaveBeenCalledWith({ queryKey: WORKFLOW_DEFINITIONS_QUERY_KEY })
    } finally {
      qc.clear()
    }
  })

  it.each(['pending', 'applying', 'failed', 'cancelled', 'expired', 'undone'] as const)('does not invalidate memory for a %s card', status => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    try {
      applyCardUpdate(qc, { ...card('crewmate.memory', status), params: { store: 'scout' } })
      expect(spy).not.toHaveBeenCalled()
    } finally {
      qc.clear()
    }
  })

  it('does not invalidate memory for another card kind or a missing store name', () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    try {
      applyCardUpdate(qc, { ...card('setting.change', 'applied'), params: { store: 'scout' } })
      applyCardUpdate(qc, card('crewmate.memory', 'applied'))
      applyCardUpdate(qc, { ...card('crewmate.memory', 'applied'), params: { store: 42 } })
      expect(spy).not.toHaveBeenCalled()
    } finally {
      qc.clear()
    }
  })
})
