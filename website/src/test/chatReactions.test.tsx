import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import {
  useChatReactions,
  reactionTogglePending,
  __resetReactionToggleLedgerForTests,
} from '../hooks/useChatReactions'
import { MessageReactionBar } from '../pages/chat/MessageReactionBar'
import { reactionLimitFromError } from '../hooks/reactionToggleLedger'
import type { ChatReaction } from '../api/reactions'

vi.mock('../api/reactions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/reactions')>()
  return {
    REACTION_EMOJI: actual.REACTION_EMOJI,
    reactionsApi: {
      list: vi.fn(),
      create: vi.fn(),
      remove: vi.fn(),
    },
  }
})

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) => {
    const base = key.split('.').pop() || key
    if (vars && 'emoji' in vars) return `${base} ${vars.emoji}`
    if (vars && 'limit' in vars) return `${base} ${vars.limit}`
    return base
  },
}))

import { reactionsApi } from '../api/reactions'

const mockReaction: ChatReaction = {
  id: 'r-1',
  slot_key: 'slot-abc',
  mid: 'm-mock-1234',
  emoji: '👍',
  reacted_at: '2026-08-01T12:00:00Z',
}

function createWrapper(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children)
}

describe('useChatReactions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction] })
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(mockReaction)
    ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
  })

  it('fetches reactions on mount when slotKey is provided', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))
    expect(reactionsApi.list).toHaveBeenCalledWith('slot-abc')
  })

  it('does not fetch when slotKey is undefined', () => {
    const { result } = renderHook(() => useChatReactions(undefined), { wrapper: createWrapper() })
    expect(result.current.reactions).toHaveLength(0)
    expect(reactionsApi.list).not.toHaveBeenCalled()
  })

  it('reactionsFor returns the set of emoji on a message', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))
    expect(result.current.reactionsFor('m-mock-1234').has('👍')).toBe(true)
    expect(result.current.reactionsFor('m-mock-1234').has('⭐')).toBe(false)
    expect(result.current.reactionsFor('unknown-mid').size).toBe(0)
  })

  it('reactionsFor keeps one Set per message until that message changes', async () => {
    // Reacted rows are memoized and a streaming reply renders per chunk, so an
    // unchanged message must get the same Set back, or it re-renders each time.
    const other: ChatReaction = { ...mockReaction, id: 'r-2', mid: 'm-other', emoji: '⭐' }
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction, other] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(2))
    const first = result.current.reactionsFor('m-mock-1234')
    expect(result.current.reactionsFor('m-mock-1234')).toBe(first)
    expect(result.current.reactionsFor('unknown')).toBe(result.current.reactionsFor('also-unknown'))

    const added: ChatReaction = { ...other, id: 'r-3', emoji: '👀' }
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(added)
    // The settled refetch reads the server, which now holds the new record.
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction, other, added] })
    await act(async () => { await result.current.toggleReaction('m-other', '👀') })
    await waitFor(() => expect(result.current.reactionsFor('m-other').has('👀')).toBe(true))
    // The message that changed gets a new Set; the one that did not keeps its own.
    expect(result.current.reactionsFor('m-mock-1234')).toBe(first)
  })

  it('toggleReaction adds a reaction not yet present', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    const newReaction: ChatReaction = { ...mockReaction, id: 'r-2', mid: 'm-new', emoji: '⭐' }
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(newReaction)
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction, newReaction] })

    await act(async () => { await result.current.toggleReaction('m-new', '⭐') })
    expect(reactionsApi.create).toHaveBeenCalledWith({
      slot_key: 'slot-abc', mid: 'm-new', emoji: '⭐',
    })
  })

  it('toggleReaction removes a reaction already present (same emoji, same message)', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    await act(async () => { await result.current.toggleReaction('m-mock-1234', '👍') })
    expect(reactionsApi.remove).toHaveBeenCalledWith('slot-abc', 'm-mock-1234', '👍')
    expect(reactionsApi.create).not.toHaveBeenCalled()
  })

  it('a failed create rolls the optimistic reaction back and sets a message-scoped error', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await act(async () => { await result.current.toggleReaction('m-new-fail', '👀').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-new-fail', emoji: '👀', kind: 'react' }))
    // Rolled back: the failed optimistic reaction is gone.
    expect(result.current.reactionsFor('m-new-fail').has('👀')).toBe(false)
  })

  it('a 409 on create reports the limit error kind', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(Object.assign(new Error('full'), { status: 409 }))
    await act(async () => { await result.current.toggleReaction('m-limit', '👀').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-limit', emoji: '👀', kind: 'limit' }))
  })

  it('a 409 carries the cap the server reported into the limit error', async () => {
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(Object.assign(new Error('full'), {
      status: 409, body: '{"error":"slot already has 500 reactions","code":"reaction_limit_reached","limit":500}',
    }))
    await act(async () => { await result.current.toggleReaction('m-limit', '👀').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-limit', emoji: '👀', kind: 'limit', limit: 500 }))
  })

  it('a failed create with NO prior cache drops the optimistic reaction (not left active)', async () => {
    // The initial GET fails, so onMutate captures no previous snapshot. A
    // failed create must still remove its own optimistic entry by id, or the
    // rejected reaction stays visibly active and a re-click would DELETE
    // instead of retrying the create (GPT useChatReactions:64).
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('list down'))
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.loadFailed).toBe(true))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await act(async () => { await result.current.toggleReaction('m-nocache', '👍').catch(() => {}) })
    await waitFor(() => expect(result.current.error).toEqual({ slot: 'slot-abc', mid: 'm-nocache', emoji: '👍', kind: 'react' }))
    // Not left active despite the absent initial cache.
    expect(result.current.reactionsFor('m-nocache').has('👍')).toBe(false)
  })

  it('a queued off-click after a failed create never fires a SECOND create (intent, not cache)', async () => {
    // Click an absent reaction ON (POST fails, optimistic entry rolled out of
    // the cache), then click it OFF. The queued off-click must act on the
    // user's INTENT (off) and never re-read the post-failure cache to fire a
    // second POST. (GPT 6.1 useChatReactions:237.) It DOES send a DELETE, which
    // is idempotent and 404-safe, so a server that persisted the lost POST is
    // still turned off.
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })

    await act(async () => {
      const on = result.current.toggleReaction('m-x', '👀').catch(() => {})
      const off = result.current.toggleReaction('m-x', '👀').catch(() => {})
      await Promise.all([on, off])
    })
    // Intent converged to OFF before the chain ran, so it sends one DELETE and
    // no POST at all — the reaction is never persisted ON behind the user's
    // final OFF, and a failed create can never strand it on.
    expect(reactionsApi.create).not.toHaveBeenCalled()
    expect(reactionsApi.remove).toHaveBeenCalledWith('slot-abc', 'm-x', '👀')
    expect(result.current.reactionsFor('m-x').has('👀')).toBe(false)
  })

  it('an off-click still sends DELETE even when the cache shows the reaction absent', async () => {
    // The lost-POST race (GPT 6.1 useChatReactions:205): add then remove while
    // the POST is pending; the POST persists server-side but its response is
    // lost, so onError drops the optimistic row and the cache shows absent. The
    // queued OFF must still send DELETE (idempotent) rather than trusting the
    // stale cache and skipping it, or the reaction stays ON.
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('response lost'))
    ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })

    await act(async () => {
      const on = result.current.toggleReaction('m-lost', '👀').catch(() => {})
      const off = result.current.toggleReaction('m-lost', '👀').catch(() => {})
      await Promise.all([on, off])
    })
    // The OFF intent sent DELETE despite the cache reading absent after the
    // failed create — the reaction does not silently stay ON.
    expect(reactionsApi.remove).toHaveBeenCalledWith('slot-abc', 'm-lost', '👀')
  })

  it('a DELETE that 404s (already absent) is treated as success, not an error', async () => {
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(1))

    ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockRejectedValue(Object.assign(new Error('gone'), { status: 404 }))
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    await act(async () => { await result.current.toggleReaction('m-mock-1234', '👍') })
    // No unreact error surfaced — an already-absent reaction is the desired end.
    expect(result.current.error).toBeNull()
  })

  it('a single on-click still sends one create', async () => {
    // Guard the converge-to-intent logic does not swallow a lone click.
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const created: ChatReaction = { ...mockReaction, id: 'r-one', mid: 'm-one', emoji: '👀' }
    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(created)
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    await act(async () => { await result.current.toggleReaction('m-one', '👀') })
    expect(reactionsApi.create).toHaveBeenCalledTimes(1)
    expect(reactionsApi.create).toHaveBeenCalledWith({ slot_key: 'slot-abc', mid: 'm-one', emoji: '👀' })
  })

  it('a reaction started in one slot settles against that slot after a switch', async () => {
    // Start a reaction in slot A, then re-render the hook for slot B before the
    // POST settles. The mutation's bookkeeping (ledger release + settled
    // refetch) must act on slot A, not the now-current slot B, or A's pending
    // count strands and A's cross-tab refreshes defer forever. (GPT 6.1
    // useChatReactions:121.)
    __resetReactionToggleLedgerForTests()
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    let resolveCreate: (r: ChatReaction) => void = () => {}
    const createStarted = new Promise<void>(start => {
      ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockImplementation(
        () => { start(); return new Promise<ChatReaction>(res => { resolveCreate = res }) })
    })
    const created: ChatReaction = { ...mockReaction, id: 'r-sw', slot_key: 'slot-A', mid: 'm-sw', emoji: '👍' }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useChatReactions(slot),
      { wrapper: createWrapper(qc), initialProps: { slot: 'slot-A' } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))

    let p: Promise<void> = Promise.resolve()
    await act(async () => {
      p = result.current.toggleReaction('m-sw', '👍')
      await createStarted // the POST is now in flight for slot A
    })
    expect(reactionTogglePending(qc, 'slot-A')).toBe(true)
    // Switch to slot B before the create resolves.
    rerender({ slot: 'slot-B' })
    await act(async () => { resolveCreate(created); await p })
    // Slot A drained despite the hook now pointing at B.
    expect(reactionTogglePending(qc, 'slot-A')).toBe(false)
    expect(reactionsApi.create).toHaveBeenCalledWith({ slot_key: 'slot-A', mid: 'm-sw', emoji: '👍' })
  })

  it('a queued second toggle on another identity is not reversed by the first settle', async () => {
    // Click 👍 on one message and ⭐ on another while both POSTs are pending.
    // The first settle's refetch must not drop the second's pending optimistic
    // entry and make its queued state read wrong (GPT useChatReactions:127).
    const r1: ChatReaction = { ...mockReaction, id: 'r-a', mid: 'm-a', emoji: '👍' }
    const r2: ChatReaction = { ...mockReaction, id: 'r-b', mid: 'm-b', emoji: '⭐' }
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    const { result } = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.reactions).toHaveLength(0))

    ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockImplementation((b: { mid: string }) =>
      Promise.resolve(b.mid === 'm-a' ? r1 : r2))
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [r1, r2] })

    await act(async () => {
      await Promise.all([
        result.current.toggleReaction('m-a', '👍'),
        result.current.toggleReaction('m-b', '⭐'),
      ])
    })
    // Both reactions persisted as ADDs; neither was flipped to a delete.
    expect(reactionsApi.create).toHaveBeenCalledTimes(2)
    expect(reactionsApi.remove).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(result.current.reactionsFor('m-a').has('👍')).toBe(true)
      expect(result.current.reactionsFor('m-b').has('⭐')).toBe(true)
    })
  })

  it('a remove after a remount waits for the add the previous mount left in flight', async () => {
    // GPT 6.1 useChatReactions:183. The embedded companion chat
    // (ArtifactChatPanel → ChatPage → useTranscriptReactions) remounts the hook
    // while the app-wide QueryClient — and so the optimistic row — survives.
    // Add a reaction with its POST still pending, unmount, remount on the SAME
    // client, then remove it. The DELETE must not run before the POST settles:
    // a DELETE that 404s first counts as success, then the late POST persists
    // the reaction and the settled refetch restores the marker the user removed.
    __resetReactionToggleLedgerForTests()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [] })
    let resolveCreate: (r: ChatReaction) => void = () => {}
    const createStarted = new Promise<void>(start => {
      ;(reactionsApi.create as ReturnType<typeof vi.fn>).mockImplementation(
        () => { start(); return new Promise<ChatReaction>(res => { resolveCreate = res }) })
    })
    const created: ChatReaction = { ...mockReaction, id: 'r-rm', mid: 'm-rm', emoji: '👍' }

    const first = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(first.result.current.loading).toBe(false))
    let add: Promise<void> = Promise.resolve()
    await act(async () => {
      add = first.result.current.toggleReaction('m-rm', '👍')
      await createStarted // the POST is in flight, the optimistic row is cached
    })
    first.unmount()

    const second = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper(qc) })
    expect(second.result.current.reactionsFor('m-rm').has('👍')).toBe(true)
    let remove: Promise<void> = Promise.resolve()
    await act(async () => {
      remove = second.result.current.toggleReaction('m-rm', '👍')
      await Promise.resolve()
    })
    // The pending POST has not settled, so the remount's DELETE must still wait.
    expect(reactionsApi.remove).not.toHaveBeenCalled()

    await act(async () => {
      resolveCreate(created)
      await add.catch(() => {})
      await remove
    })
    // Only now, after the add settled, does the remove go out — in that order.
    expect(reactionsApi.remove).toHaveBeenCalledWith('slot-abc', 'm-rm', '👍')
    const createOrder = (reactionsApi.create as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]
    const removeOrder = (reactionsApi.remove as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]
    expect(createOrder).toBeLessThan(removeOrder)
  })

  it('a remove that fails after a remount shows its notice on the new mount', async () => {
    // The companion chat remounts the hook while the QueryClient survives. A
    // DELETE started on the first mount fails only after that mount is gone;
    // the rollback puts the reaction back in the shared cache, and the mount
    // now on screen must say the remove failed rather than show it silently.
    __resetReactionToggleLedgerForTests()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    ;(reactionsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ reactions: [mockReaction] })
    let rejectRemove: (e: unknown) => void = () => {}
    const removeStarted = new Promise<void>(start => {
      ;(reactionsApi.remove as ReturnType<typeof vi.fn>).mockImplementation(
        () => { start(); return new Promise((_res, rej) => { rejectRemove = rej }) })
    })

    const first = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(first.result.current.reactions).toHaveLength(1))
    let remove: Promise<void> = Promise.resolve()
    await act(async () => {
      remove = first.result.current.toggleReaction('m-mock-1234', '👍')
      await removeStarted
    })
    first.unmount()

    const second = renderHook(() => useChatReactions('slot-abc'), { wrapper: createWrapper(qc) })
    expect(second.result.current.error).toBeNull()
    await act(async () => {
      rejectRemove(Object.assign(new Error('persist failed'), { status: 500 }))
      await remove.catch(() => {})
    })

    expect(second.result.current.error).toEqual(
      { slot: 'slot-abc', mid: 'm-mock-1234', emoji: '👍', kind: 'unreact' })
    expect(second.result.current.reactionsFor('m-mock-1234').has('👍')).toBe(true)
    act(() => second.result.current.clearError())
    expect(second.result.current.error).toBeNull()
  })
})

describe('MessageReactionBar', () => {
  it('shows only the add trigger when nothing is placed (one control, under the cap)', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('reaction-add-trigger')).toBeInTheDocument()
    expect(screen.queryAllByTestId('reaction-chip')).toHaveLength(0)
  })

  it('draws each placed emoji as its own visible marker, including an overflow emoji', () => {
    render(<MessageReactionBar active={new Set(['👍', '⭐'])} onToggle={() => {}} />)
    const chips = screen.getAllByTestId('reaction-chip')
    expect(chips.map(c => c.getAttribute('data-emoji'))).toEqual(['👍', '⭐'])
    // The ⭐ is drawn as its glyph, not hidden behind a highlighted + (the
    // regression the UX review caught).
    expect(chips.find(c => c.getAttribute('data-emoji') === '⭐')!).toHaveTextContent('⭐')
  })

  it('placed markers are display-only, not clickable action buttons (max-two cap)', () => {
    const onToggle = vi.fn()
    render(<MessageReactionBar active={new Set(['👍', '✅', '👀'])} onToggle={onToggle} />)
    // Three placed + the add trigger would be four buttons in one row; the
    // markers are spans, so only the single add trigger is a button.
    const chips = screen.getAllByTestId('reaction-chip')
    expect(chips).toHaveLength(3)
    for (const chip of chips) expect(chip.tagName).toBe('SPAN')
    fireEvent.click(chips[0])
    expect(onToggle).not.toHaveBeenCalled() // a marker does nothing on click
  })

  it('opens the add menu to place any not-yet-placed emoji', async () => {
    const onToggle = vi.fn()
    render(<MessageReactionBar active={new Set()} onToggle={onToggle} />)
    // Radix opens on keyboard activation, a path jsdom handles (unlike the
    // PointerEvent-driven mouse open).
    fireEvent.keyDown(screen.getByTestId('reaction-add-trigger'), { key: 'Enter' })
    const items = await screen.findAllByTestId('reaction-menu-item')
    expect(items.map(i => i.getAttribute('data-emoji'))).toEqual(['👍', '✅', '👀', '⭐'])
    const star = items.find(i => i.getAttribute('data-emoji') === '⭐')!
    fireEvent.click(star)
    expect(onToggle).toHaveBeenCalledWith('⭐')
  })

  it('removal lives in the menu: a placed emoji shows "Remove" with a check', async () => {
    const onToggle = vi.fn()
    render(<MessageReactionBar active={new Set(['👍'])} onToggle={onToggle} />)
    fireEvent.keyDown(screen.getByTestId('reaction-add-trigger'), { key: 'Enter' })
    const items = await screen.findAllByTestId('reaction-menu-item')
    const thumb = items.find(i => i.getAttribute('data-emoji') === '👍')!
    // The mock i18n renders remove_reaction with an {emoji} var as
    // "remove_reaction 👍"; selecting it toggles the placed emoji off.
    expect(thumb).toHaveTextContent('remove_reaction')
    fireEvent.click(thumb)
    expect(onToggle).toHaveBeenCalledWith('👍')
  })

  it('keeps the add trigger present even when every emoji is placed (it also removes)', () => {
    render(<MessageReactionBar active={new Set(['👍', '✅', '👀', '⭐'])} onToggle={() => {}} />)
    expect(screen.getAllByTestId('reaction-chip')).toHaveLength(4)
    expect(screen.getByTestId('reaction-add-trigger')).toBeInTheDocument()
  })

  it('surfaces a message-scoped error through an inline notice', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={{ slot: 's', mid: 'm-1', emoji: '👍', kind: 'react' }} />)
    // The mock i18n renders a key with an {emoji} var as "<lastSegment> <emoji>".
    expect(screen.getByText('reaction_failed_emoji 👍')).toBeInTheDocument()
  })

  it('renders no notice when there is no error', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={null} />)
    expect(screen.queryByText(/reaction_failed_emoji/)).not.toBeInTheDocument()
  })

  it('names the emoji in the remove-failure notice', () => {
    render(<MessageReactionBar active={new Set(['👍'])} onToggle={() => {}} error={{ slot: 's', mid: 'm-1', emoji: '👍', kind: 'unreact' }} />)
    expect(screen.getByText('unreaction_failed 👍')).toBeInTheDocument()
  })

  it('states the cap in the limit notice when the server reported one', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={{ slot: 's', mid: 'm-1', emoji: '👍', kind: 'limit', limit: 500 }} />)
    expect(screen.getByText('reaction_limit_reached_count 500')).toBeInTheDocument()
  })

  it('falls back to the uncounted limit notice when no cap was reported', () => {
    render(<MessageReactionBar active={new Set()} onToggle={() => {}} error={{ slot: 's', mid: 'm-1', emoji: '👍', kind: 'limit' }} />)
    expect(screen.getByText('reaction_limit_reached')).toBeInTheDocument()
  })

  it('keeps the add trigger visible at rest once a reaction is placed', () => {
    // The markers are not clickable, so the `+` is the only way to remove one.
    const { rerender } = render(<MessageReactionBar active={new Set()} onToggle={() => {}} />)
    expect(screen.getByTestId('reaction-add-trigger').className).toMatch(/(^|\s)opacity-0(\s|$)/)
    rerender(<MessageReactionBar active={new Set(['⭐'])} onToggle={() => {}} />)
    const trigger = screen.getByTestId('reaction-add-trigger')
    expect(trigger.className).toMatch(/(^|\s)opacity-100(\s|$)/)
    expect(trigger.className).not.toMatch(/(^|\s)opacity-0(\s|$)/)
  })
})

describe('reactionLimitFromError', () => {
  it('reads the cap from a 409 body and ignores anything else', () => {
    expect(reactionLimitFromError({ status: 409, body: '{"code":"reaction_limit_reached","limit":500}' })).toBe(500)
    expect(reactionLimitFromError({ status: 409, body: '{"code":"reaction_limit_reached"}' })).toBeUndefined()
    expect(reactionLimitFromError({ status: 409, body: '{"limit":"500"}' })).toBeUndefined()
    expect(reactionLimitFromError({ status: 409, body: 'not json' })).toBeUndefined()
    expect(reactionLimitFromError(new Error('network'))).toBeUndefined()
  })
})
