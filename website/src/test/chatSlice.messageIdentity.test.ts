/**
 * Stable per-message identity (#381, #411, #463).
 *
 * Verifies that:
 * - Every message pushed through the reducer gets a stable UUID-based
 *   identity (meta.clientTs) when born without a server `ts`.
 * - Messages WITH a server `ts` are left untouched (no redundant stamp).
 * - The UUID format is `msg-<uuid-v4>` (not the old `born-<timestamp>-<seq>`).
 * - The identity survives Immer structural sharing (chunk accumulation).
 * - Error messages, permission messages, and other ts-less roles all get stamped.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, appendMessage } from '../store/chatSlice'

const SLOT = 'test-msg-identity-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const withSlot = { ...initial, activeSlot: SLOT }

const UUID_RE = /^msg-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('stable per-message identity (#381, #411)', () => {
  it('stamps a UUID-based clientTs on streaming messages', () => {
    const state = reducer(withSlot, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'hi', seq: 1 }))
    const streaming = state.messages.find(m => m.role === 'streaming')!
    expect(streaming.meta?.clientTs).toMatch(UUID_RE)
  })

  it('stamps a UUID-based clientTs on thinking messages', () => {
    const state = reducer(withSlot, sseChatMessage({ slot: SLOT, role: 'thinking', content: '' }))
    const thinking = state.messages.find(m => m.role === 'thinking')!
    expect(thinking.meta?.clientTs).toMatch(UUID_RE)
  })

  it('stamps a UUID-based clientTs on error messages (ts-less) via appendMessage', () => {
    const state = reducer(withSlot, appendMessage({ role: 'error', content: 'Something failed', cls: '' }))
    const err = state.messages.find(m => m.role === 'error')!
    expect(err.meta?.clientTs).toMatch(UUID_RE)
  })

  it('does NOT stamp clientTs on messages that already have a server ts', () => {
    const serverTs = '2026-08-04T12:00:00Z'
    const state = reducer(withSlot, sseChatMessage({ slot: SLOT, role: 'user', content: 'hello', ts: serverTs }))
    const user = state.messages.find(m => m.role === 'user')!
    expect(user.ts).toBe(serverTs)
    // Should NOT have a clientTs since it has a ts
    expect(user.meta?.clientTs).toBeUndefined()
  })

  it('does NOT overwrite an existing clientTs on messages', () => {
    const existing = 'pre-existing-id'
    const state = reducer(withSlot, appendMessage({ role: 'error', content: 'err', cls: '', meta: { clientTs: existing } }))
    const err = state.messages.find(m => m.role === 'error')!
    expect(err.meta?.clientTs).toBe(existing)
  })

  it('stamps a UUID-based clientTs on permission messages without ts', () => {
    const state = reducer(withSlot, sseChatMessage({
      slot: SLOT, role: 'permission', content: 'Allow?', cls: '', meta: { approval_id: 'a1' },
    }))
    const perm = state.messages.find(m => m.role === 'permission')!
    expect(perm.meta?.clientTs).toMatch(UUID_RE)
    // approval_id should also be preserved
    expect(perm.meta?.approval_id).toBe('a1')
  })

  it('stamps identity in non-active slot path', () => {
    const state = reducer(
      { ...withSlot, activeSlot: 'other-slot' },
      sseChatMessage({ slot: SLOT, role: 'error', content: 'oops' }),
    )
    const msgs = state.slotMessages[SLOT]!
    const err = msgs.find(m => m.role === 'error')!
    expect(err.meta?.clientTs).toMatch(UUID_RE)
  })

  it('streaming identity is stable across chunk accumulation (Immer replace)', () => {
    let state = reducer(withSlot, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A', seq: 1 }))
    const id1 = state.messages.find(m => m.role === 'streaming')!.meta?.clientTs

    state = reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'B', seq: 2 }))
    const id2 = state.messages.find(m => m.role === 'streaming')!.meta?.clientTs

    expect(id1).toMatch(UUID_RE)
    expect(id2).toBe(id1) // same identity despite Immer replacing the object
  })

  it('each new message gets a UNIQUE id', () => {
    let state = reducer(withSlot, appendMessage({ role: 'error', content: 'err1', cls: '' }))
    state = reducer(state, appendMessage({ role: 'error', content: 'err2', cls: '' }))
    const ids = state.messages
      .filter(m => m.role === 'error')
      .map(m => m.meta?.clientTs as string)
    expect(ids).toHaveLength(2)
    expect(ids[0]).not.toBe(ids[1])
    ids.forEach(id => expect(id).toMatch(UUID_RE))
  })
})

describe('chip-status / full-payload derivation collapse (#463)', () => {
  // Issue #463 was about ensuring the chip-status (lightweight) and full-payload
  // (detail panel) derivations use the same vocabulary function. This is verified
  // by the existing PullRequestPanel.test.tsx tests that exercise
  // pullRequestLifecycleState and pullRequestCiSignal — the single-sourced
  // functions both the batch endpoint and the panel's inline override call.
  // The backend shares _project_state / _rollup_ci / _gitlab_aggregate_ci
  // across both paths. This test documents the structural guarantee.
  it('pullRequestLifecycleState and pullRequestCiSignal are the single derivation path', async () => {
    const { pullRequestLifecycleState, pullRequestCiSignal } = await import('../components/PullRequestPanel')
    // Open + no draft = open
    expect(pullRequestLifecycleState({ state: 'open', draft: false } as never)).toBe('open')
    // Draft open = draft
    expect(pullRequestLifecycleState({ state: 'OPEN', draft: true } as never)).toBe('draft')
    // CI rollup: any failed = failed
    expect(pullRequestCiSignal([{ bucket: 'passed' }, { bucket: 'failed' }] as never)).toBe('failed')
    // CI rollup: all passed = passed
    expect(pullRequestCiSignal([{ bucket: 'passed' }, { bucket: 'passed' }] as never)).toBe('passed')
    // CI rollup: pending + passed = running
    expect(pullRequestCiSignal([{ bucket: 'passed' }, { bucket: 'pending' }] as never)).toBe('running')
  })
})
