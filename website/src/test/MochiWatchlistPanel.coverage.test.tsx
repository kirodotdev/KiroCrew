// First coverage for Mochi's Watch List side panel — the list/detail rail that
// owns every watch item the pet is tracking: the stale-while-revalidate load,
// the active/recently-completed/earlier partition, per-item status actions, the
// no-undo "clear completed" confirmation, the detail editor (notes, priority,
// check interval with its min/hr/day unit selector), and the history timeline.
//
// Two things shape the style of this file:
//
//   1. The panel's only seam to the outside world is `api` in `mochiApi`, so
//      that module is mocked and nothing here touches the network. The mock
//      mirrors the preload's `onWatchlistChanged(cb) => off` subscription shape
//      so the push path can be driven the way the main process drives it.
//   2. The whole file runs on fake timers with a pinned system time. The panel
//      computes `new Date()` on every render (every countdown and every
//      relative timestamp is derived from it) and stages its view transitions
//      and detail-mode status writes behind `setTimeout`. A frozen clock makes
//      all of that assertable instead of a race, and matches the pattern
//      MochiPetWidget.coverage.test.tsx already uses.
//
// Interactions go through `fireEvent`, not `userEvent`, so each step stays
// synchronous against the same fake clock the timers are stepped on.
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HistoryEntry, WatchItem } from '../apps/mochi/src/shared/watchlistTypes'

const mocks = vi.hoisted(() => {
  type Listener = (items: unknown[]) => void
  const listeners = new Set<Listener>()
  const api = {
    getWatchlist: vi.fn<() => Promise<WatchItem[]>>(),
    setWatchItemStatus: vi.fn(),
    updateWatchItem: vi.fn(),
    clearCompletedWatchItems: vi.fn<() => Promise<boolean | undefined>>(),
    /** Mirrors the preload's `onX(cb) => off` shape. */
    onWatchlistChanged: vi.fn((cb: Listener) => {
      listeners.add(cb)
      return () => { listeners.delete(cb) }
    }),
  }
  return {
    api,
    emit: (items: unknown[]) => { for (const cb of [...listeners]) cb(items) },
    listenerCount: () => listeners.size,
    clearListeners: () => listeners.clear(),
  }
})

vi.mock('../apps/mochi/src/mochiApi', () => ({ api: mocks.api }))

const {
  WatchlistPanel,
  formatCountdown,
  formatStatusSummary,
  buildEditPayload,
  applyDataRefresh,
  escapeNavigation,
  EDITABLE_FIELD_KEYS,
} = await import('../apps/mochi/src/renderer/WatchlistPanel')

const api = mocks.api

/** Frozen "now" for every test. Countdowns and relative times key off it. */
const NOW = new Date('2026-03-10T12:00:00.000Z')
const minutesFromNow = (m: number) => new Date(NOW.getTime() + m * 60_000).toISOString()
const minutesAgo = (m: number) => new Date(NOW.getTime() - m * 60_000).toISOString()

function makeItem(over: Partial<WatchItem> = {}): WatchItem {
  return {
    id: 'w-1',
    label: 'Ticket price drop',
    kind: 'url',
    target: 'https://example.invalid/tickets',
    status: 'watching',
    priority: 'normal',
    createdAt: minutesAgo(600),
    nextCheckAfter: minutesFromNow(10),
    checkCount: 4,
    failCount: 0,
    maxFailCount: 10,
    maxWatchDurationHours: 168,
    checkIntervalMins: 10,
    notifyOnChange: true,
    autoComplete: true,
    source: 'chat',
    history: [],
    ...over,
  }
}

/** Let the load promise settle and any staged timer fire. */
async function tick(ms = 0): Promise<void> {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

/** Mount with `items` already resolvable, then flush the initial load. */
async function mountWith(
  items: WatchItem[],
  props: { petName?: string } = {},
): Promise<{ onClose: ReturnType<typeof vi.fn> }> {
  api.getWatchlist.mockResolvedValue(items)
  const onClose = vi.fn()
  render(<WatchlistPanel visible onClose={onClose} {...props} />)
  await tick()
  return { onClose }
}

/** The clickable row for `label` — the row itself carries role="button". */
function row(label: string): HTMLElement {
  const rows = screen.getAllByRole('button').filter(el => el.textContent?.includes(label))
  const found = rows.find(el => el.getAttribute('tabindex') === '0')
  if (!found) throw new Error(`no watch row for ${label}`)
  return found
}

/** Open the detail view for `label` and let the 250ms view transition settle. */
async function openDetail(label: string): Promise<void> {
  fireEvent.click(row(label))
  await tick(250)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
  api.getWatchlist.mockReset()
  api.getWatchlist.mockResolvedValue([])
  api.setWatchItemStatus.mockReset()
  api.updateWatchItem.mockReset()
  api.clearCompletedWatchItems.mockReset()
  api.clearCompletedWatchItems.mockResolvedValue(true)
  api.onWatchlistChanged.mockClear()
  mocks.clearListeners()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

// ── Pure helpers ───────────────────────────────────────────────────────────

describe('formatCountdown', () => {
  it('reports overdue once the target time has passed', () => {
    expect(formatCountdown(minutesAgo(1), NOW)).toEqual({ kind: 'overdue' })
  })

  it('treats the exact boundary as overdue', () => {
    expect(formatCountdown(NOW.toISOString(), NOW)).toEqual({ kind: 'overdue' })
  })

  it('rounds sub-hour gaps up to whole minutes', () => {
    expect(formatCountdown(new Date(NOW.getTime() + 30_001).toISOString(), NOW))
      .toEqual({ kind: 'mins', params: { mins: '1' } })
    expect(formatCountdown(minutesFromNow(59), NOW))
      .toEqual({ kind: 'mins', params: { mins: '59' } })
  })

  it('splits gaps of an hour or more into hours and remainder minutes', () => {
    expect(formatCountdown(minutesFromNow(60), NOW))
      .toEqual({ kind: 'hours', params: { hours: '1', mins: '0' } })
    expect(formatCountdown(minutesFromNow(150), NOW))
      .toEqual({ kind: 'hours', params: { hours: '2', mins: '30' } })
  })
})

describe('formatStatusSummary', () => {
  it('appends the last result when there is one', () => {
    expect(formatStatusSummary(makeItem({ lastResult: 'still $420' })))
      .toBe('Watching — still $420')
  })

  it('falls back to the status name alone', () => {
    expect(formatStatusSummary(makeItem({ status: 'triggered' }))).toBe('Triggered')
  })
})

describe('buildEditPayload', () => {
  it('keeps only the three editable fields', () => {
    expect(buildEditPayload({
      notes: 'n', priority: 'high', checkIntervalMins: 30,
      // A field the panel never edits must not reach the bridge.
      label: 'hijacked', status: 'done',
    } as never)).toEqual({ notes: 'n', priority: 'high', checkIntervalMins: 30 })
  })

  it('drops explicitly-undefined values and returns {} for an empty draft', () => {
    expect(buildEditPayload({ notes: undefined, priority: 'low' })).toEqual({ priority: 'low' })
    expect(buildEditPayload({})).toEqual({})
  })

  it('publishes the editable key set', () => {
    expect([...EDITABLE_FIELD_KEYS].sort()).toEqual(['checkIntervalMins', 'notes', 'priority'])
  })
})

describe('applyDataRefresh', () => {
  it('drops the selection and the draft when the selected item is gone', () => {
    expect(applyDataRefresh([makeItem({ id: 'w-2' })], 'w-1', { notes: 'draft' }))
      .toEqual({ selectedItemId: null, editDraft: null })
  })

  it('preserves the selection and the draft when the item survives', () => {
    const draft = { notes: 'draft' }
    expect(applyDataRefresh([makeItem({ id: 'w-1' })], 'w-1', draft))
      .toEqual({ selectedItemId: 'w-1', editDraft: draft })
  })

  it('is a no-op when nothing is selected', () => {
    expect(applyDataRefresh([], null, null)).toEqual({ selectedItemId: null, editDraft: null })
  })
})

describe('escapeNavigation', () => {
  it('unwinds detail → list before closing the panel', () => {
    expect(escapeNavigation('w-1')).toEqual({ action: 'to-list' })
    expect(escapeNavigation(null)).toEqual({ action: 'close' })
  })
})

// ── Shell + load ───────────────────────────────────────────────────────────

describe('WatchlistPanel shell', () => {
  it('renders nothing while hidden, and does not subscribe or fetch', () => {
    const { container } = render(<WatchlistPanel visible={false} onClose={vi.fn()} />)
    expect(container.firstChild).toBeNull()
    expect(api.getWatchlist).not.toHaveBeenCalled()
    expect(api.onWatchlistChanged).not.toHaveBeenCalled()
  })

  it('shows the loading placeholder until the first payload arrives', async () => {
    let resolve: (items: WatchItem[]) => void = () => {}
    api.getWatchlist.mockReturnValue(new Promise<WatchItem[]>(r => { resolve = r }))
    render(<WatchlistPanel visible onClose={vi.fn()} />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()

    await act(async () => { resolve([makeItem()]) })
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
    expect(screen.getByText('Ticket price drop')).toBeInTheDocument()
  })

  it('falls back to the empty state when the load rejects', async () => {
    api.getWatchlist.mockRejectedValue(new Error('bridge down'))
    render(<WatchlistPanel visible onClose={vi.fn()} />)
    await tick()
    expect(screen.getByText('No items being watched')).toBeInTheDocument()
  })

  it('treats a null payload as an empty list and names the pet in the hint', async () => {
    api.getWatchlist.mockResolvedValue(null as unknown as WatchItem[])
    render(<WatchlistPanel visible onClose={vi.fn()} petName="Momo" />)
    await tick()
    expect(screen.getByText('No items being watched')).toBeInTheDocument()
    expect(screen.getByText('Ask Momo to watch a price, a delivery, or a web page'))
      .toBeInTheDocument()
  })

  it('falls back to the default pet name when none is given', async () => {
    await mountWith([])
    expect(screen.getByText(/^Ask \S+ to watch a price/)).toBeInTheDocument()
  })

  it('badges the active count and hides the badge when nothing is active', async () => {
    await mountWith([
      makeItem({ id: 'w-1' }),
      makeItem({ id: 'w-2', label: 'Second' }),
      makeItem({ id: 'w-3', label: 'Finished', status: 'done', completedAt: minutesAgo(5) }),
    ])
    const header = screen.getByText('Watch List').parentElement as HTMLElement
    expect(within(header).getByText('2')).toBeInTheDocument()

    cleanup()
    await mountWith([makeItem({ status: 'cancelled', completedAt: minutesAgo(5) })])
    const soloHeader = screen.getByText('Watch List').parentElement as HTMLElement
    expect(within(soloHeader).queryByText('1')).not.toBeInTheDocument()
  })

  it('discards an in-flight load when the panel is closed before it lands', async () => {
    let resolve: (items: WatchItem[]) => void = () => {}
    api.getWatchlist.mockReturnValue(new Promise<WatchItem[]>(r => { resolve = r }))
    const { rerender } = render(<WatchlistPanel visible onClose={vi.fn()} />)
    rerender(<WatchlistPanel visible={false} onClose={vi.fn()} />)
    await act(async () => { resolve([makeItem()]) })

    // Re-opening refetches rather than showing what the cancelled load carried.
    api.getWatchlist.mockResolvedValue([makeItem({ label: 'Fresh item' })])
    rerender(<WatchlistPanel visible onClose={vi.fn()} />)
    await tick()
    expect(screen.getByText('Fresh item')).toBeInTheDocument()
    expect(screen.queryByText('Ticket price drop')).not.toBeInTheDocument()
  })

  it('discards a rejected load when the panel is already closed', async () => {
    let reject: (e: Error) => void = () => {}
    api.getWatchlist.mockReturnValue(new Promise<WatchItem[]>((_, r) => { reject = r }))
    const { rerender } = render(<WatchlistPanel visible onClose={vi.fn()} />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    rerender(<WatchlistPanel visible={false} onClose={vi.fn()} />)
    await act(async () => { reject(new Error('bridge down')) })
    expect(screen.queryByText('No items being watched')).not.toBeInTheDocument()
  })

  it('closes on the header close button', async () => {
    const { onClose } = await mountWith([])
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('unsubscribes from the change feed on unmount', async () => {
    await mountWith([])
    expect(mocks.listenerCount()).toBe(1)
    cleanup()
    expect(mocks.listenerCount()).toBe(0)
  })
})

// ── List mode ──────────────────────────────────────────────────────────────

describe('WatchlistPanel list mode', () => {
  it('renders label, countdown, kind pill and status summary', async () => {
    await mountWith([makeItem({
      nextCheckAfter: minutesFromNow(150), lastResult: 'still $420',
    })])
    expect(screen.getByText('Ticket price drop')).toBeInTheDocument()
    expect(screen.getByText('2h 30m left')).toBeInTheDocument()
    expect(screen.getByText('Web page')).toBeInTheDocument()
    expect(screen.getByText('Watching — still $420')).toBeInTheDocument()
  })

  it('renders an overdue countdown for a check that is due', async () => {
    await mountWith([makeItem({ nextCheckAfter: minutesAgo(3) })])
    expect(screen.getByText('Checking soon')).toBeInTheDocument()
  })

  it('renders minute countdowns and omits the countdown when there is no next check', async () => {
    await mountWith([
      makeItem({ id: 'w-1', nextCheckAfter: minutesFromNow(12) }),
      makeItem({ id: 'w-2', label: 'No schedule', nextCheckAfter: '' }),
    ])
    expect(screen.getByText('12m left')).toBeInTheDocument()
    expect(within(row('No schedule')).queryByText(/left|Checking soon/)).not.toBeInTheDocument()
  })

  it('completes an item: writes through the bridge and drops it from the active count', async () => {
    await mountWith([makeItem()])
    fireEvent.click(screen.getByRole('button', { name: 'Complete' }))
    expect(api.setWatchItemStatus).toHaveBeenCalledWith('w-1', 'done')

    const header = screen.getByText('Watch List').parentElement as HTMLElement
    expect(within(header).queryByText('1')).not.toBeInTheDocument()
    // Terminal now, so its own actions are gone and it hides behind the toggle.
    expect(screen.queryByRole('button', { name: 'Complete' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show completed (1)' })).toBeInTheDocument()
  })

  it('cancels an item through the bridge, touching only that item', async () => {
    await mountWith([
      makeItem({ id: 'w-1' }),
      makeItem({ id: 'w-2', label: 'Sibling' }),
    ])
    fireEvent.click(within(row('Ticket price drop')).getByRole('button', { name: 'Stop watching' }))
    expect(api.setWatchItemStatus).toHaveBeenCalledWith('w-1', 'cancelled')
    expect(screen.getByRole('button', { name: 'Show completed (1)' })).toBeInTheDocument()
    // The sibling is still active, so its own actions survive.
    expect(within(row('Sibling')).getByRole('button', { name: 'Complete' })).toBeInTheDocument()
  })

  it('partitions completed items into recent and earlier, and toggles them', async () => {
    await mountWith([
      makeItem({ id: 'w-1', label: 'Active one' }),
      makeItem({ id: 'w-2', label: 'Just done', status: 'done', completedAt: minutesAgo(30) }),
      makeItem({ id: 'w-3', label: 'Old done', status: 'done', completedAt: minutesAgo(60 * 30) }),
      // No completedAt at all still counts as "earlier".
      makeItem({ id: 'w-4', label: 'Undated fail', status: 'failed' }),
    ])
    expect(screen.queryByText('Just done')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show completed (3)' }))
    expect(screen.getByText('Recently completed')).toBeInTheDocument()
    expect(screen.getByText('Just done')).toBeInTheDocument()
    expect(screen.getByText('Earlier')).toBeInTheDocument()
    expect(screen.getByText('Old done')).toBeInTheDocument()
    expect(screen.getByText('Undated fail')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Hide completed' }))
    expect(screen.queryByText('Recently completed')).not.toBeInTheDocument()
    expect(screen.queryByText('Just done')).not.toBeInTheDocument()
  })

  it('omits the section heading a partition has no members for', async () => {
    await mountWith([
      makeItem({ id: 'w-2', label: 'Just done', status: 'done', completedAt: minutesAgo(30) }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (1)' }))
    expect(screen.getByText('Recently completed')).toBeInTheDocument()
    expect(screen.queryByText('Earlier')).not.toBeInTheDocument()
  })

  it('offers Reopen on a completed watch, but not on a completed reminder', async () => {
    await mountWith([
      makeItem({ id: 'w-1', label: 'Done watch', status: 'done', completedAt: minutesAgo(5) }),
      makeItem({
        id: 'w-2', label: 'Done reminder', kind: 'reminder',
        status: 'done', completedAt: minutesAgo(5),
      }),
      makeItem({
        id: 'w-3', label: 'Done meeting', kind: 'meeting',
        status: 'done', completedAt: minutesAgo(5),
      }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (3)' }))
    expect(within(row('Done reminder')).queryByRole('button', { name: 'Reopen' })).toBeNull()
    expect(within(row('Done meeting')).queryByRole('button', { name: 'Reopen' })).toBeNull()

    fireEvent.click(within(row('Done watch')).getByRole('button', { name: 'Reopen' }))
    expect(api.setWatchItemStatus).toHaveBeenCalledWith('w-1', 'watching')
    // Reopened items rejoin the active list, so only two remain completed.
    expect(screen.getByRole('button', { name: 'Hide completed' })).toBeInTheDocument()
    expect(within(row('Done watch')).getByRole('button', { name: 'Complete' })).toBeInTheDocument()
  })

  it('does not open the detail view when a row action is clicked', async () => {
    await mountWith([makeItem()])
    fireEvent.click(screen.getByRole('button', { name: 'Complete' }))
    await tick(250)
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
  })

  it('opens the detail view from the keyboard with Enter and Space', async () => {
    await mountWith([makeItem()])
    fireEvent.keyDown(row('Ticket price drop'), { key: 'Enter' })
    await tick(250)
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)
    fireEvent.keyDown(row('Ticket price drop'), { key: ' ' })
    await tick(250)
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
  })

  it('ignores other keys on a row', async () => {
    await mountWith([makeItem()])
    fireEvent.keyDown(row('Ticket price drop'), { key: 'a' })
    await tick(250)
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
  })

  it('does not open the detail view from a keystroke inside the action strip', async () => {
    await mountWith([
      makeItem({ id: 'w-1' }),
      makeItem({ id: 'w-2', label: 'Done one', status: 'done', completedAt: minutesAgo(5) }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (1)' }))

    // The action strip is revealed on hover/focus-within and swallows key events,
    // so Enter on a revealed button must not also select the row behind it.
    for (const label of ['Ticket price drop', 'Done one']) {
      const strip = row(label).querySelector('.wl-actions') as HTMLElement
      fireEvent.keyDown(strip, { key: 'Enter' })
      await tick(250)
      expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
    }
  })

  it('brightens the close button on hover', async () => {
    await mountWith([])
    const close = screen.getByRole('button', { name: 'Close' })
    expect(close.style.opacity).toBe('0.6')
    fireEvent.mouseEnter(close)
    expect(close.style.opacity).toBe('1')
    fireEvent.mouseLeave(close)
    expect(close.style.opacity).toBe('0.6')
  })
})

// ── Clear completed ────────────────────────────────────────────────────────

describe('WatchlistPanel clear completed', () => {
  const seed = () => [
    makeItem({ id: 'w-1', label: 'Active one' }),
    makeItem({ id: 'w-2', label: 'Done one', status: 'done', completedAt: minutesAgo(30) }),
    makeItem({ id: 'w-3', label: 'Done two', status: 'cancelled', completedAt: minutesAgo(40) }),
  ]

  async function openConfirm(): Promise<HTMLElement> {
    await mountWith(seed())
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (2)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear all completed' }))
    return screen.getByRole('dialog')
  }

  it('states the count before deleting anything', async () => {
    const dialog = await openConfirm()
    expect(within(dialog).getByText('Clear completed?')).toBeInTheDocument()
    expect(within(dialog).getByText('Deletes 2 completed item(s). This cannot be undone.'))
      .toBeInTheDocument()
    expect(api.clearCompletedWatchItems).not.toHaveBeenCalled()
  })

  it('keeps everything when the confirmation is declined', async () => {
    const dialog = await openConfirm()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Keep them' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(api.clearCompletedWatchItems).not.toHaveBeenCalled()
    expect(screen.getByText('Done one')).toBeInTheDocument()
  })

  it('shows a working state, then drops the completed items and re-collapses', async () => {
    let resolve: (ok: boolean) => void = () => {}
    api.clearCompletedWatchItems.mockReturnValue(new Promise<boolean>(r => { resolve = r }))
    const dialog = await openConfirm()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' }))

    const working = within(screen.getByRole('dialog')).getByRole('button', { name: 'Clearing…' })
    expect(working).toBeDisabled()

    await act(async () => { resolve(true) })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Done one')).not.toBeInTheDocument()
    expect(screen.getByText('Active one')).toBeInTheDocument()
    // Nothing terminal is left, so neither toggle survives.
    expect(screen.queryByRole('button', { name: /completed/ })).not.toBeInTheDocument()
  })

  it('keeps the items on screen when the bridge rejects the delete', async () => {
    api.clearCompletedWatchItems.mockResolvedValue(false)
    const dialog = await openConfirm()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' }))
    await tick()

    const failed = screen.getByRole('dialog')
    expect(within(failed).getByText('Could not clear them. Nothing was deleted — try again.'))
      .toBeInTheDocument()
    expect(within(failed).queryByRole('button', { name: 'Clear' })).toBeNull()
    expect(screen.getByText('Done one')).toBeInTheDocument()

    fireEvent.click(within(failed).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText('Done one')).toBeInTheDocument()
  })

  it('clears on an undefined result, treating only an explicit false as failure', async () => {
    api.clearCompletedWatchItems.mockResolvedValue(undefined)
    const dialog = await openConfirm()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' }))
    await tick()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Done one')).not.toBeInTheDocument()
  })
})

// ── Escape handling ────────────────────────────────────────────────────────

describe('WatchlistPanel escape handling', () => {
  const esc = () => fireEvent.keyDown(window, { key: 'Escape' })

  it('closes the panel from list mode', async () => {
    const { onClose } = await mountWith([makeItem()])
    esc()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('unwinds detail mode to the list instead of closing', async () => {
    const { onClose } = await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    esc()
    await tick(250)
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Complete' })).toBeInTheDocument()
  })

  it('consumes the keystroke for the confirm overlay without closing the panel', async () => {
    const { onClose } = await mountWith([
      makeItem({ id: 'w-2', label: 'Done one', status: 'done', completedAt: minutesAgo(5) }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (1)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear all completed' }))
    esc()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('consumes the keystroke for the failure overlay too', async () => {
    api.clearCompletedWatchItems.mockResolvedValue(false)
    const { onClose } = await mountWith([
      makeItem({ id: 'w-2', label: 'Done one', status: 'done', completedAt: minutesAgo(5) }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (1)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear all completed' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear' }))
    await tick()
    esc()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('ignores keys other than Escape', async () => {
    const { onClose } = await mountWith([makeItem()])
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(onClose).not.toHaveBeenCalled()
  })
})

// ── Push updates ───────────────────────────────────────────────────────────

describe('WatchlistPanel change feed', () => {
  it('replaces the list when the bridge pushes new data', async () => {
    await mountWith([makeItem()])
    await act(async () => {
      mocks.emit([makeItem({ id: 'w-9', label: 'Pushed item' })])
    })
    expect(screen.getByText('Pushed item')).toBeInTheDocument()
    expect(screen.queryByText('Ticket price drop')).not.toBeInTheDocument()
  })

  it('treats a null push as an empty list', async () => {
    await mountWith([makeItem()])
    await act(async () => { mocks.emit(null as unknown as unknown[]) })
    expect(screen.getByText('No items being watched')).toBeInTheDocument()
  })

  it('returns to the list when the open item disappears from the push', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()

    await act(async () => { mocks.emit([makeItem({ id: 'w-2', label: 'Something else' })]) })
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
    expect(screen.getByText('Something else')).toBeInTheDocument()
  })

  it('keeps the open item when it survives the push', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    await act(async () => {
      mocks.emit([makeItem({ lastResult: 'now $380' }), makeItem({ id: 'w-2', label: 'Other' })])
    })
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(screen.getByText('now $380')).toBeInTheDocument()
  })
})

// ── Detail mode ────────────────────────────────────────────────────────────

describe('WatchlistPanel detail mode', () => {
  it('renders the label, the kind/status/priority pills and the next-check block', async () => {
    await mountWith([makeItem({
      priority: 'high', nextCheckAfter: minutesFromNow(45), lastResult: 'still $420',
    })])
    await openDetail('Ticket price drop')

    // The meta row is kind / status / priority, in that order. Scoped to the row
    // because the priority select repeats every priority name as an <option>.
    const pills = screen.getByText('Web page').parentElement as HTMLElement
    expect([...pills.children].map(c => c.textContent)).toEqual(['Web page', 'Watching', 'High'])
    expect(screen.getByText('Next Check')).toBeInTheDocument()
    expect(screen.getByText('45m left')).toBeInTheDocument()
    // Same-day target: a clock time, and no date suffix.
    const timeRow = screen.getByText('45m left').parentElement as HTMLElement
    const stamp = (timeRow.children[1] as HTMLElement).textContent ?? ''
    expect(stamp).toMatch(/^\d{1,2}:\d{2}/)
    expect(stamp).not.toContain('·')
  })

  it('appends the date when the next check is not today', async () => {
    await mountWith([makeItem({ nextCheckAfter: minutesFromNow(60 * 40) })])
    await openDetail('Ticket price drop')
    const timeRow = screen.getByText('40h 0m left').parentElement as HTMLElement
    expect((timeRow.children[1] as HTMLElement).textContent).toMatch(/·\s\w{3}\s\d+$/)
  })

  it('renders the trigger-time block and no interval editor for a reminder', async () => {
    await mountWith([makeItem({
      kind: 'reminder', triggerAt: minutesFromNow(20), nextCheckAfter: minutesFromNow(20),
    })])
    await openDetail('Ticket price drop')
    expect(screen.getByText('Trigger Time')).toBeInTheDocument()
    expect(screen.queryByText('Next Check')).not.toBeInTheDocument()
    expect(screen.queryByText('Check Interval')).not.toBeInTheDocument()
    // Time-triggered items expose only the target row.
    expect(screen.getByText('Target')).toBeInTheDocument()
    expect(screen.queryByText('Check Count')).not.toBeInTheDocument()
    expect(screen.queryByText('Last Result')).not.toBeInTheDocument()
  })

  it('omits the time block when a time-triggered item has no trigger time', async () => {
    await mountWith([makeItem({ kind: 'meeting', triggerAt: undefined })])
    await openDetail('Ticket price drop')
    expect(screen.queryByText('Trigger Time')).not.toBeInTheDocument()
    expect(screen.queryByText('Next Check')).not.toBeInTheDocument()
  })

  it('marks an overdue next check with the overdue phrasing', async () => {
    await mountWith([makeItem({ nextCheckAfter: minutesAgo(5) })])
    await openDetail('Ticket price drop')
    expect(screen.getAllByText('Checking soon').length).toBeGreaterThan(0)
  })

  it('renders the info rows, filling missing values with a dash', async () => {
    await mountWith([makeItem({ lastResult: undefined, lastChecked: undefined, checkCount: 7 })])
    await openDetail('Ticket price drop')
    expect(screen.getByText('https://example.invalid/tickets')).toBeInTheDocument()
    expect(screen.getAllByText('—')).toHaveLength(2)
    expect(screen.getByText('7')).toBeInTheDocument()
  })

  it('renders the last-checked value as a relative time', async () => {
    const cases: Array<[string, string]> = [
      [minutesAgo(0), 'just now'],
      [minutesFromNow(5), 'just now'],
      [minutesAgo(20), '20m ago'],
      [minutesAgo(200), '3h ago'],
      [minutesAgo(60 * 50), '2d ago'],
    ]
    for (const [lastChecked, expected] of cases) {
      await mountWith([makeItem({ lastChecked })])
      await openDetail('Ticket price drop')
      expect(screen.getByText(expected)).toBeInTheDocument()
      cleanup()
    }
  })

  it('goes back to the list from the Back button', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
    expect(screen.getByText('Ticket price drop')).toBeInTheDocument()
  })

  it('completes from detail mode, returning to the list first', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.click(within(screen.getByText('Back').parentElement as HTMLElement)
      .getByRole('button', { name: 'Complete' }))
    expect(api.setWatchItemStatus).not.toHaveBeenCalled()
    await tick(250)
    expect(api.setWatchItemStatus).toHaveBeenCalledWith('w-1', 'done')
    expect(screen.queryByRole('button', { name: 'Back' })).not.toBeInTheDocument()
  })

  it('cancels from detail mode', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.click(screen.getByRole('button', { name: 'Stop watching' }))
    await tick(250)
    expect(api.setWatchItemStatus).toHaveBeenCalledWith('w-1', 'cancelled')
  })

  it('reopens a terminal item from detail mode, and offers no status actions on a done reminder', async () => {
    await mountWith([
      makeItem({ id: 'w-1', label: 'Done watch', status: 'done', completedAt: minutesAgo(5) }),
      makeItem({
        id: 'w-2', label: 'Done reminder', kind: 'reminder',
        status: 'done', completedAt: minutesAgo(5),
      }),
    ])
    fireEvent.click(screen.getByRole('button', { name: 'Show completed (2)' }))

    await openDetail('Done reminder')
    expect(screen.queryByRole('button', { name: 'Reopen' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)

    await openDetail('Done watch')
    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    await tick(250)
    expect(api.setWatchItemStatus).toHaveBeenCalledWith('w-1', 'watching')
  })
})

// ── Detail editing ─────────────────────────────────────────────────────────

describe('WatchlistPanel detail editing', () => {
  const notes = () => screen.getByPlaceholderText('...') as HTMLTextAreaElement
  const saveBtn = () => screen.getByRole('button', { name: 'Save' })
  const intervalInput = () => screen.getByRole('spinbutton') as HTMLInputElement

  it('disables Save until the draft actually changes', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    expect(saveBtn()).toBeDisabled()

    fireEvent.change(notes(), { target: { value: 'watch the fare class' } })
    expect(saveBtn()).toBeEnabled()
  })

  it('saves notes through the bridge and clears the draft', async () => {
    await mountWith([makeItem({ notes: 'original' })])
    await openDetail('Ticket price drop')
    expect(notes().value).toBe('original')

    fireEvent.change(notes(), { target: { value: 'edited' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-1', { notes: 'edited' })
    expect(saveBtn()).toBeDisabled()
    // The saved value is now the item's own, not a lingering draft.
    expect(notes().value).toBe('edited')
  })

  it('starts the notes field empty when the item has none', async () => {
    await mountWith([makeItem({ notes: undefined })])
    await openDetail('Ticket price drop')
    expect(notes().value).toBe('')
  })

  it('saves a priority change', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    const select = screen.getByRole('combobox') as HTMLSelectElement
    expect(select.value).toBe('normal')

    fireEvent.change(select, { target: { value: 'high' } })
    expect(select.value).toBe('high')
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-1', { priority: 'high' })
  })

  it('recomputes the next check when the interval is saved', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.change(intervalInput(), { target: { value: '25' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-1', { checkIntervalMins: 25 })

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)
    // NOW + 25 minutes, from the frozen clock.
    expect(screen.getByText('25m left')).toBeInTheDocument()
  })

  it('floors a sub-minimum interval at the bridge minimum', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.change(intervalInput(), { target: { value: '1' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-1', { checkIntervalMins: 3 })
  })

  it('treats unparseable interval input as 1', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.change(intervalInput(), { target: { value: 'abc' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-1', { checkIntervalMins: 3 })
  })

  it('picks the interval unit from the stored value', async () => {
    await mountWith([
      makeItem({ id: 'w-1', label: 'In minutes', checkIntervalMins: 10 }),
      makeItem({ id: 'w-2', label: 'In hours', checkIntervalMins: 120 }),
      makeItem({ id: 'w-3', label: 'In days', checkIntervalMins: 2880 }),
    ])

    await openDetail('In minutes')
    expect(intervalInput().value).toBe('10')
    expect(intervalInput()).toHaveAttribute('min', '3')
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)

    await openDetail('In hours')
    expect(intervalInput().value).toBe('2')
    expect(intervalInput()).toHaveAttribute('min', '1')
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)

    await openDetail('In days')
    expect(intervalInput().value).toBe('2')
  })

  it('converts through the unit selector and saves the value in minutes', async () => {
    await mountWith([makeItem({ checkIntervalMins: 30 })])
    await openDetail('Ticket price drop')

    fireEvent.click(screen.getByRole('button', { name: 'hr' }))
    // 30 minutes rounds to 1 hour rather than showing 0.
    expect(intervalInput().value).toBe('1')
    fireEvent.change(intervalInput(), { target: { value: '3' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenLastCalledWith('w-1', { checkIntervalMins: 180 })

    fireEvent.click(screen.getByRole('button', { name: 'day' }))
    expect(intervalInput().value).toBe('1')
    fireEvent.change(intervalInput(), { target: { value: '2' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenLastCalledWith('w-1', { checkIntervalMins: 2880 })

    fireEvent.click(screen.getByRole('button', { name: 'min' }))
    expect(intervalInput().value).toBe('2880')
  })

  it('never shows a zero interval when the stored value rounds below one unit', async () => {
    await mountWith([makeItem({ checkIntervalMins: 10 })])
    await openDetail('Ticket price drop')

    fireEvent.click(screen.getByRole('button', { name: 'hr' }))
    expect(intervalInput().value).toBe('1')
    fireEvent.click(screen.getByRole('button', { name: 'day' }))
    expect(intervalInput().value).toBe('1')
  })

  it('sends all three fields when all three are edited', async () => {
    await mountWith([makeItem()])
    await openDetail('Ticket price drop')
    fireEvent.change(notes(), { target: { value: 'all three' } })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'low' } })
    fireEvent.change(intervalInput(), { target: { value: '45' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-1', {
      notes: 'all three', priority: 'low', checkIntervalMins: 45,
    })
  })

  it('leaves the other items untouched when one is saved', async () => {
    await mountWith([
      makeItem({ id: 'w-1', notes: 'first note' }),
      makeItem({ id: 'w-2', label: 'Sibling', notes: 'second note', checkIntervalMins: 10 }),
    ])
    await openDetail('Sibling')
    fireEvent.change(notes(), { target: { value: 'sibling edited' } })
    fireEvent.change(intervalInput(), { target: { value: '20' } })
    fireEvent.click(saveBtn())
    expect(api.updateWatchItem).toHaveBeenCalledWith('w-2', {
      notes: 'sibling edited', checkIntervalMins: 20,
    })

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)
    // Only the saved row's countdown moved; the sibling keeps its own schedule.
    expect(screen.getByText('20m left')).toBeInTheDocument()
    expect(screen.getByText('10m left')).toBeInTheDocument()

    await openDetail('Ticket price drop')
    expect(notes().value).toBe('first note')
  })

  it('drops the draft when the user navigates back without saving', async () => {
    await mountWith([makeItem({ notes: 'original' })])
    await openDetail('Ticket price drop')
    fireEvent.change(notes(), { target: { value: 'abandoned' } })
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await tick(250)

    await openDetail('Ticket price drop')
    expect(notes().value).toBe('original')
    expect(saveBtn()).toBeDisabled()
    expect(api.updateWatchItem).not.toHaveBeenCalled()
  })
})

// ── History ────────────────────────────────────────────────────────────────

describe('WatchlistPanel history', () => {
  const entry = (over: Partial<HistoryEntry>): HistoryEntry => ({
    checkedAt: minutesAgo(30), result: 'no change', changed: false, ...over,
  })

  it('shows only changed entries, newest first, until Show all is used', async () => {
    await mountWith([makeItem({
      history: [
        entry({ checkedAt: minutesAgo(90), result: 'first look' }),
        entry({ checkedAt: minutesAgo(60), result: 'price dropped', changed: true }),
        entry({ checkedAt: minutesAgo(20), result: 'holding' }),
      ],
    })])
    await openDetail('Ticket price drop')

    expect(screen.getByText('History')).toBeInTheDocument()
    expect(screen.getByText('price dropped')).toBeInTheDocument()
    expect(screen.queryByText('first look')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show all (3)' }))
    const timeline = document.querySelector('.wl-history-timeline') as HTMLElement
    // Newest first, and each row is prefixed with its own relative timestamp.
    expect([...timeline.children].map(c => c.textContent)).toEqual([
      '20m agoholding', '1h agoprice dropped', '1h agofirst look',
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Changes only' }))
    expect(screen.queryByText('first look')).not.toBeInTheDocument()
  })

  it('hides the Show all toggle when every entry already changed', async () => {
    await mountWith([makeItem({
      history: [entry({ result: 'price dropped', changed: true })],
    })])
    await openDetail('Ticket price drop')
    expect(screen.queryByRole('button', { name: /Show all/ })).toBeNull()
    expect(screen.getByText('price dropped')).toBeInTheDocument()
  })

  it('says so when there is history but nothing has changed yet', async () => {
    await mountWith([makeItem({ history: [entry({}), entry({ checkedAt: minutesAgo(10) })] })])
    await openDetail('Ticket price drop')
    expect(screen.getByText('No changes detected yet')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show all (2)' }))
    expect(screen.queryByText('No changes detected yet')).not.toBeInTheDocument()
    expect(screen.getAllByText('no change')).toHaveLength(2)
  })

  it('renders no history section for an item without history', async () => {
    await mountWith([makeItem({ history: [] })])
    await openDetail('Ticket price drop')
    expect(screen.queryByText('History')).not.toBeInTheDocument()
  })
})
