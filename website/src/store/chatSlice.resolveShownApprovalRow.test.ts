import { describe, it, expect } from 'vitest'
import reducer, { resolveShownApprovalRow } from './chatSlice'
import type { ChatState } from './chat/state'

const perm = (clientTs: string, resolved?: string) => ({
  role: 'permission', content: 'Running: ls', cls: '', meta: { approval_id: 'ap-1', clientTs, ...(resolved ? { resolved } : {}) },
})

const withMessages = (messages: unknown[]): ChatState =>
  ({ ...reducer(undefined, { type: '@@init' }), activeSlot: 'slot-1', messages }) as unknown as ChatState

describe('resolveShownApprovalRow', () => {
  it('settles the row at its index, not the first row under the id', () => {
    const next = reducer(withMessages([perm('k-old', 'approved'), perm('k-new')]),
      resolveShownApprovalRow({ slot: 'slot-1', index: 1, key: 'k-new', id: 'ap-1', decision: 'stale' }))
    expect(next.messages.map(m => m.meta?.resolved)).toEqual(['approved', 'stale'])
  })

  it('finds the row by its key when the index moved', () => {
    const next = reducer(withMessages([{ role: 'user', content: 'x', cls: '' }, perm('k-old', 'approved'), perm('k-new')]),
      resolveShownApprovalRow({ slot: 'slot-1', index: 1, key: 'k-new', id: 'ap-1', decision: 'stale' }))
    expect(next.messages.map(m => m.meta?.resolved)).toEqual([undefined, 'approved', 'stale'])
  })

  it('settles nothing when the index moved and the row has no key', () => {
    const rows = [perm('k-old', 'approved'), { role: 'permission', content: '', cls: '', meta: { approval_id: 'ap-1' } }]
    const next = reducer(withMessages(rows),
      resolveShownApprovalRow({ slot: 'slot-1', index: 0, id: 'ap-1', decision: 'stale' }))
    expect(next.messages.map(m => m.meta?.resolved)).toEqual(['approved', undefined])
  })

  it('never overwrites an already-decided row at the index with a stale settle', () => {
    const next = reducer(withMessages([perm('k', 'approved')]),
      resolveShownApprovalRow({ slot: 'slot-1', index: 0, key: 'k', id: 'ap-1', decision: 'stale' }))
    expect(next.messages[0].meta?.resolved).toBe('approved')
  })

  it('settles a background slot in its own list, not the active mirror', () => {
    const state = { ...withMessages([perm('k')]), slotMessages: { 'slot-2': [perm('k')] } } as unknown as ChatState
    const next = reducer(state,
      resolveShownApprovalRow({ slot: 'slot-2', index: 0, key: 'k', id: 'ap-1', decision: 'stale' }))
    expect(next.messages[0].meta?.resolved).toBeUndefined()
    expect(next.slotMessages['slot-2'][0].meta?.resolved).toBe('stale')
  })

  it('never settles a different id at the index', () => {
    const other = { ...perm('k'), meta: { approval_id: 'ap-2', clientTs: 'k' } }
    const next = reducer(withMessages([other]),
      resolveShownApprovalRow({ slot: 'slot-1', index: 0, key: 'k', id: 'ap-1', decision: 'stale' }))
    expect(next.messages[0].meta?.resolved).toBeUndefined()
  })

  it('leaves the input state unchanged', () => {
    const before = withMessages([perm('k')])
    const snap = JSON.stringify(before)
    reducer(before, resolveShownApprovalRow({ slot: 'slot-1', index: 0, key: 'k', id: 'ap-1', decision: 'stale' }))
    expect(JSON.stringify(before)).toBe(snap)
  })
})
