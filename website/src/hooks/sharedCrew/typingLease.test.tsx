import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE } from '../../api/sharedCrewUi'
import { useComposerTypingLock } from '../../pages/chat/shared-crew/useComposerTypingLock'
import type { PublicPrincipal, TypingLease } from '../../api/sharedCrew'
import { isComposerLocked, leaseLocks, noteComposerLocked, refusedLeaseFor, resetTypingLeaseForTests } from '../../api/typingLease'
import { applySharedCrewFrame, setSharedCrewSender, sharedCrewKeys } from './sharedCrewSocket'
import { MIN_SPACING_MS, usePresenceReporter } from './usePresenceReporter'

const alice: PublicPrincipal = { id: 'p_alice', kind: 'human', display: { name: 'Alice' } } as PublicPrincipal
const bob: PublicPrincipal = { id: 'p_bob', kind: 'human', display: { name: 'Bob' } } as PublicPrincipal

function lease(holder: PublicPrincipal | null, extra: Partial<TypingLease> = {}): TypingLease {
  return {
    schema_version: 1,
    session_key: 's1',
    holder,
    holder_ids: holder ? [holder.id] : [],
    expires_at: holder ? 1_000 : null,
    idle_seconds: 10,
    ended: '',
    ...extra,
  }
}

describe('typing lease state', () => {
  beforeEach(() => resetTypingLeaseForTests())

  it('locks everyone but the holder, and fails open without a viewer', () => {
    expect(leaseLocks(lease(alice), bob)).toBe(true)
    expect(leaseLocks(lease(alice), alice)).toBe(false)
    expect(leaseLocks(lease(null), bob)).toBe(false)
    expect(leaseLocks(lease(alice), null)).toBe(false)
    // One operator's other local sign-in is the same holder.
    expect(leaseLocks(lease(alice, { holder_ids: ['p_alice', 'p_alice_local'] }), { ...alice, id: 'p_alice_local' })).toBe(false)
  })

  it('folds a lease frame into the cache as a whole value', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(alice) })
    expect(qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))?.holder?.id).toBe('p_alice')
    applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(null, { ended: 'idle' }) })
    const after = qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))
    expect(after?.holder).toBeNull()
    expect(after?.ended).toBe('idle')
  })

  it('recognizes and records a composer_locked refusal', () => {
    const body = { error: 'x', code: 'composer_locked', session_key: 's1', holder: alice, lease: lease(alice) }
    expect(isComposerLocked(body)).toBe(true)
    expect(isComposerLocked({ code: 'send_race' })).toBe(false)
    noteComposerLocked(body as never)
    expect(refusedLeaseFor('s1')?.lease.holder?.id).toBe('p_alice')
  })
})

describe('the reporter drives the lease', () => {
  let sent: Array<Record<string, unknown>>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-18T00:00:00Z'))
    sent = []
    setSharedCrewSender(p => sent.push(p as Record<string, unknown>))
  })

  afterEach(() => {
    setSharedCrewSender(null)
    vi.useRealTimers()
  })

  it('renews typing once per spacing while keystrokes continue', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', true))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    for (let i = 0; i < 8; i++) {
      act(() => { result.current.noteTyping(false); vi.advanceTimersByTime(1_000) })
    }
    const typing = sent.filter(f => f.activity === 'typing').length
    // 8 s of keystrokes at one frame per 2.5 s spacing, plus the trailing one.
    expect(typing).toBeGreaterThanOrEqual(3)
    expect(typing).toBeLessThanOrEqual(4)
  })

  it('reports an emptied draft at once with the content-free flag', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', true))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS); result.current.noteTyping(false) })
    act(() => { vi.advanceTimersByTime(100); result.current.noteTyping(true) })
    const last = sent[sent.length - 1]
    expect(last).toEqual({ type: 'shared_crew.activity', session_key: 's1', activity: 'viewing', draft: 'empty' })
    // An empty edit from a tab that never drafted reports nothing.
    const n = sent.length
    act(() => { result.current.noteTyping(true) })
    expect(sent.length).toBe(n)
  })
})

describe('composer lock reconciliation', () => {
  beforeEach(() => {
    resetTypingLeaseForTests()
    vi.spyOn(Date, 'now').mockReturnValue(200)
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks(); resetTypingLeaseForTests() })

  function mount(presence = true) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    qc.setQueryData(sharedCrewKeys.capabilities, { ...ALL_UNAVAILABLE, presence: presence ? 'available' : 'unavailable' })
    qc.setQueryData(sharedCrewKeys.viewer, bob)
    qc.setQueryData(sharedCrewKeys.typingLease('s1'), lease(null), { updatedAt: 100 })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    return { qc, ...renderHook(() => useComposerTypingLock('s1'), { wrapper }) }
  }

  function refuse() {
    noteComposerLocked({ error: 'locked', code: 'composer_locked', session_key: 's1', holder: alice, lease: lease(alice) })
  }

  it('keeps a released composer unlocked when an older refusal arrives late', async () => {
    let resolve!: (value: TypingLease) => void
    const read = vi.spyOn(api, 'sharedCrewTypingLease').mockReturnValue(new Promise(r => { resolve = r }))
    const { qc, result } = mount()
    act(() => applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(null, { ended: 'empty' }) }))
    vi.mocked(Date.now).mockReturnValue(300)
    act(() => refuse())
    expect(result.current).toBeNull()
    await waitFor(() => expect(read).toHaveBeenCalledWith('s1'))
    await act(async () => resolve(lease(null, { ended: 'empty' })))
    await waitFor(() => expect(qc.getQueryState(sharedCrewKeys.typingLease('s1'))?.fetchStatus).toBe('idle'))
    expect(result.current).toBeNull()
  })

  it('learns an active lock from a refusal-triggered read and unlocks on the release frame', async () => {
    const read = vi.spyOn(api, 'sharedCrewTypingLease').mockResolvedValue(lease(alice))
    const { qc, result } = mount()
    act(() => refuse())
    await waitFor(() => expect(result.current?.holderId).toBe(alice.id))
    expect(read).toHaveBeenCalledWith('s1')
    act(() => applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(null, { ended: 'empty' }) }))
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('does not read or lock a personal Crew after a refusal', async () => {
    const read = vi.spyOn(api, 'sharedCrewTypingLease')
    const { result } = mount(false)
    await act(async () => refuse())
    expect(result.current).toBeNull()
    expect(read).not.toHaveBeenCalled()
  })
})

describe('typing lease takeover', () => {
  beforeEach(() => resetTypingLeaseForTests())
  afterEach(() => { cleanup(); vi.restoreAllMocks(); resetTypingLeaseForTests() })

  function mount(viewer: PublicPrincipal, held: TypingLease, preview: boolean) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    qc.setQueryData(sharedCrewKeys.capabilities, { ...ALL_UNAVAILABLE, presence: 'available', shared_crew_preview: preview })
    qc.setQueryData(sharedCrewKeys.viewer, viewer)
    qc.setQueryData(sharedCrewKeys.typingLease('s1'), held)
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    return { qc, ...renderHook(() => useComposerTypingLock('s1'), { wrapper }) }
  }

  const takenFromAlice = lease(bob, { ended: 'taken_over', taken_over_from: alice, taken_over_from_ids: [alice.id] })

  it('offers no takeover without the preview', () => {
    const { result } = mount(bob, lease(alice), false)
    expect(result.current?.holderId).toBe(alice.id)
    expect(result.current?.onTakeOver).toBeUndefined()
  })

  it('takes the lease over and unlocks the taker once the lease is re-read', async () => {
    const take = vi.spyOn(api, 'sharedCrewTakeOverTypingLease').mockResolvedValue(takenFromAlice)
    vi.spyOn(api, 'sharedCrewTypingLease').mockResolvedValue(takenFromAlice)
    const { qc, result } = mount(bob, lease(alice), true)
    expect(result.current?.onTakeOver).toBeTypeOf('function')
    await act(async () => { result.current?.onTakeOver?.() })
    expect(take).toHaveBeenCalledWith('s1')
    await waitFor(() => expect(result.current).toBeNull())
    expect(qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))?.holder?.id).toBe(bob.id)
  })

  it('keeps a newer lease frame that arrives before the takeover answer', async () => {
    let answer: (v: TypingLease) => void = () => {}
    vi.spyOn(api, 'sharedCrewTakeOverTypingLease').mockReturnValue(new Promise<TypingLease>(r => { answer = r }))
    const retaken = lease(alice, { ended: 'taken_over', taken_over_from: bob, taken_over_from_ids: [bob.id] })
    vi.spyOn(api, 'sharedCrewTypingLease').mockResolvedValue(retaken)
    const { qc, result } = mount(bob, lease(alice), true)
    act(() => { result.current?.onTakeOver?.() })
    // Alice takes it back; her frame lands before Bob's takeover answer.
    act(() => applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...retaken }))
    await act(async () => { answer(takenFromAlice) })
    await waitFor(() => expect(result.current?.takeOverPending).toBe(false))
    expect(result.current?.holderId).toBe(alice.id)
    expect(result.current?.tookOver).toBe(true)
  })

  it('shows a failed takeover and stays locked', async () => {
    vi.spyOn(api, 'sharedCrewTakeOverTypingLease').mockRejectedValue(new Error('HTTP 500'))
    const { result } = mount(bob, lease(alice), true)
    await act(async () => { result.current?.onTakeOver?.() })
    await waitFor(() => expect(result.current?.takeOverError).toBe('HTTP 500'))
    expect(result.current?.holderId).toBe(alice.id)
  })

  it('tells the previous holder who took over, from the takeover frame', async () => {
    const { qc, result } = mount(alice, lease(alice), true)
    expect(result.current).toBeNull()
    act(() => applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...takenFromAlice }))
    await waitFor(() => expect(result.current?.holderId).toBe(bob.id))
    expect(result.current?.tookOver).toBe(true)
    // The next frame without the takeover field is a plain lock again.
    act(() => applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(bob) }))
    await waitFor(() => expect(result.current?.tookOver).toBe(false))
  })

  it('keeps the takeover fields only when a frame carries them', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(alice) })
    expect(qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))).not.toHaveProperty('taken_over_from')
    applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...takenFromAlice })
    expect(qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))?.taken_over_from_ids).toEqual([alice.id])
  })
})
