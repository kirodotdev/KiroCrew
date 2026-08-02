import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import FollowUpCard from '../components/FollowUpCard'
import reducer, { setFollowupCard, clearFollowupCard, dismissFollowupItem, deleteSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { FollowupItem } from '../store/chatSlice'

const item = (over: Partial<FollowupItem> = {}): FollowupItem => ({
  title: 'Add rate limiting',
  description: 'The upload endpoint is unbounded.',
  prompt: 'Add a token-bucket limiter to POST /api/upload.',
  ...over,
})

function setup(props: Partial<React.ComponentProps<typeof FollowUpCard>> = {}) {
  const onAddToSession = vi.fn()
  const onStartInWorktree = vi.fn().mockResolvedValue(undefined)
  const onSkip = vi.fn()
  const utils = render(
    <FollowUpCard
      items={[item()]}
      projectDir="/repo"
      onAddToSession={onAddToSession}
      onStartInWorktree={onStartInWorktree}
      onSkip={onSkip}
      {...props}
    />,
  )
  return { onAddToSession, onStartInWorktree, onSkip, ...utils }
}

describe('FollowUpCard', () => {
  it('renders the title, description and all three actions', () => {
    setup()
    expect(screen.getByText('Add rate limiting')).toBeInTheDocument()
    expect(screen.getByText('The upload endpoint is unbounded.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start in new worktree/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /add to this session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /skip/i })).toBeInTheDocument()
  })

  it('states that nothing is sent without the user pressing send', () => {
    setup()
    expect(screen.getByText(/nothing is sent until you press send/i)).toBeInTheDocument()
  })

  it('calls onAddToSession with the item', () => {
    const { onAddToSession } = setup()
    fireEvent.click(screen.getByRole('button', { name: /add to this session/i }))
    expect(onAddToSession).toHaveBeenCalledWith(item())
  })

  it('passes the item index to onSkip so siblings survive', () => {
    const onSkip = vi.fn()
    render(
      <FollowUpCard
        items={[item({ title: 'First' }), item({ title: 'Second' })]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={vi.fn()}
        onSkip={onSkip}
      />,
    )
    fireEvent.click(screen.getAllByRole('button', { name: /skip/i })[1])
    expect(onSkip).toHaveBeenCalledWith(1)
  })

  it('disables the worktree action when the session has no project dir', () => {
    setup({ projectDir: undefined })
    expect(screen.getByRole('button', { name: /start in new worktree/i })).toBeDisabled()
    // The in-session route stays available — it needs no repo.
    expect(screen.getByRole('button', { name: /add to this session/i })).not.toBeDisabled()
  })

  it('renders the worktree failure inline instead of throwing', async () => {
    const onStartInWorktree = vi.fn().mockRejectedValue(new Error('Branch already exists: feat/x'))
    setup({ onStartInWorktree })
    fireEvent.click(screen.getByRole('button', { name: /start in new worktree/i }))
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Branch already exists: feat/x')
    })
    // Button is usable again so the user can retry after fixing the branch.
    expect(screen.getByRole('button', { name: /start in new worktree/i })).not.toBeDisabled()
  })

  it('does not fire a second worktree call while one is in flight', async () => {
    let release: (() => void) | undefined
    const onStartInWorktree = vi.fn(() => new Promise<void>(res => { release = res }))
    setup({ onStartInWorktree })
    const btn = screen.getByRole('button', { name: /start in new worktree/i })
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByText(/creating worktree/i)).toBeInTheDocument())
    fireEvent.click(btn)
    expect(onStartInWorktree).toHaveBeenCalledTimes(1)
    release?.()
  })

  it('ignores a worktree failure that lands after the items changed', async () => {
    // The rejection would otherwise write its error against the NEW list's
    // index, misattributing it to a different suggestion.
    let reject: ((e: Error) => void) | undefined
    const onStartInWorktree = vi.fn(() => new Promise<void>((_res, rej) => { reject = rej }))
    const a = item({ title: 'A' })
    const b = item({ title: 'B' })
    const props = { projectDir: '/repo', onAddToSession: vi.fn(), onStartInWorktree, onSkip: vi.fn() }
    const { rerender } = render(<FollowUpCard items={[a, b]} {...props} />)
    fireEvent.click(screen.getAllByRole('button', { name: /start in new worktree/i })[0])
    await waitFor(() => expect(onStartInWorktree).toHaveBeenCalled())
    rerender(<FollowUpCard items={[b]} {...props} />)
    reject?.(new Error('Branch already exists'))
    await waitFor(() => expect(screen.getByText('B')).toBeInTheDocument())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('drops a failed item\'s error when the item list changes', async () => {
    // Errors are keyed by array index; skipping the failed item shifts its
    // sibling into that index. Without the reset, B would render under A's error.
    const a = item({ title: 'A' })
    const b = item({ title: 'B' })
    const onStartInWorktree = vi.fn().mockRejectedValue(new Error('Branch already exists: feat/a'))
    const { rerender } = render(
      <FollowUpCard
        items={[a, b]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={onStartInWorktree}
        onSkip={vi.fn()}
      />,
    )
    fireEvent.click(screen.getAllByRole('button', { name: /start in new worktree/i })[0])
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    rerender(
      <FollowUpCard
        items={[b]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={onStartInWorktree}
        onSkip={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByText('B')).toBeInTheDocument()
  })
})

describe('followup card reducers', () => {
  const initial = reducer(undefined, { type: 'init' })

  it('sets and clears a card for its own slot', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 10 }))
    expect(withCard.followups['chat-1'].items).toHaveLength(1)
    expect(reducer(withCard, clearFollowupCard({ slot: 'chat-1' })).followups['chat-1']).toBeUndefined()
  })

  it('a card in one session does not evict another session\'s card', () => {
    const a = reducer(initial, setFollowupCard({ slot: 'chat-a', items: [item({ title: 'A' })], ts: 1 }))
    const both = reducer(a, setFollowupCard({ slot: 'chat-b', items: [item({ title: 'B' })], ts: 2 }))
    expect(both.followups['chat-a'].items[0].title).toBe('A')
    expect(both.followups['chat-b'].items[0].title).toBe('B')
  })

  it('clearing one session leaves the other intact', () => {
    const a = reducer(initial, setFollowupCard({ slot: 'chat-a', items: [item()], ts: 1 }))
    const both = reducer(a, setFollowupCard({ slot: 'chat-b', items: [item()], ts: 2 }))
    const cleared = reducer(both, clearFollowupCard({ slot: 'chat-a' }))
    expect(cleared.followups['chat-a']).toBeUndefined()
    expect(cleared.followups['chat-b']).toBeDefined()
  })

  it('a stale clear does not remove a newer card for the same slot', () => {
    const older = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'old' })], ts: 100 }))
    const newer = reducer(older, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'new' })], ts: 200 }))
    // An in-flight worktree action started against ts=100 completes now.
    const after = reducer(newer, clearFollowupCard({ slot: 'chat-1', ts: 100 }))
    expect(after.followups['chat-1']?.items[0].title).toBe('new')
  })

  it('a matching clear does remove the card', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 100 }))
    expect(reducer(withCard, clearFollowupCard({ slot: 'chat-1', ts: 100 })).followups['chat-1']).toBeUndefined()
  })

  it('dismissing one item keeps the rest', () => {
    const withCard = reducer(
      initial,
      setFollowupCard({ slot: 'chat-1', items: [item({ title: 'A' }), item({ title: 'B' })], ts: 1 }),
    )
    const after = reducer(withCard, dismissFollowupItem({ slot: 'chat-1', index: 0 }))
    expect(after.followups['chat-1'].items.map(i => i.title)).toEqual(['B'])
  })

  it('dismissing the last item clears the card', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 1 }))
    expect(reducer(withCard, dismissFollowupItem({ slot: 'chat-1', index: 0 })).followups['chat-1']).toBeUndefined()
  })

  it('a new card replaces an unacted-on one in the SAME slot rather than stacking', () => {
    const first = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'A' })], ts: 1 }))
    const second = reducer(first, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'B' })], ts: 2 }))
    expect(second.followups['chat-1'].items).toHaveLength(1)
    expect(second.followups['chat-1'].items[0].title).toBe('B')
  })

  it('a stale dismiss does not delete an index from a newer card', () => {
    // A replacement card can land between render and Skip click; an
    // unqualified dismiss would drop that index from a card never seen.
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'A' }), item({ title: 'B' })], ts: 10 }))
    const replaced = reducer(withCard, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'C' })], ts: 20 }))
    const stale = reducer(replaced, dismissFollowupItem({ slot: 'chat-1', index: 0, ts: 10 }))
    expect(stale.followups['chat-1'].items.map(i => i.title)).toEqual(['C'])
    // A matching ts still dismisses.
    const fresh = reducer(replaced, dismissFollowupItem({ slot: 'chat-1', index: 0, ts: 20 }))
    expect(fresh.followups['chat-1']).toBeUndefined()
  })

  it('refuses to index the map with a prototype-pollution key', () => {
    // Slot names are server-normalized to [\w\-.], which PERMITS __proto__.
    const own = (map: object, key: string) => Object.prototype.hasOwnProperty.call(map, key)
    for (const key of ['__proto__', 'constructor', 'prototype']) {
      const after = reducer(initial, setFollowupCard({ slot: key, items: [item()], ts: 1 }))
      // `constructor`/`prototype` resolve through the prototype chain, so assert
      // no OWN entry was written rather than an undefined read.
      expect(own(after.followups, key)).toBe(false)
      expect(Object.getPrototypeOf(after.followups)).toBe(Object.prototype)
      // The clear/dismiss reducers must be equally inert on such a key.
      expect(own(reducer(after, clearFollowupCard({ slot: key })).followups, key)).toBe(false)
      expect(
        own(reducer(after, dismissFollowupItem({ slot: key, index: 0 })).followups, key),
      ).toBe(false)
    }
  })

  it('ignores an empty item list', () => {
    expect(reducer(initial, setFollowupCard({ slot: 'chat-1', items: [], ts: 1 })).followups['chat-1']).toBeUndefined()
  })

  it('drops a deleted slot\'s follow-up card', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 1 }))
    const after = reducer(withCard, { type: deleteSlot.fulfilled.type, payload: 'chat-1' })
    expect(after.followups['chat-1']).toBeUndefined()
  })

  it('prunes follow-up cards for slots the server no longer lists', () => {
    let s = reducer(initial, setFollowupCard({ slot: 'chat-live', items: [item({ title: 'A' })], ts: 1 }))
    s = reducer(s, setFollowupCard({ slot: 'chat-gone', items: [item({ title: 'B' })], ts: 2 }))
    const after = reducer(s, sseSlots([{ key: 'chat-live' }] as never))
    expect(after.followups['chat-live']).toBeDefined()
    expect(after.followups['chat-gone']).toBeUndefined()
  })
})
