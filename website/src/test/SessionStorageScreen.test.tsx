import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SessionStorageScreen from '../pages/system/SessionStorageScreen'
import type { SessionInventoryList, SessionInventoryDetail, SessionStorageCleanup, SessionTrashResult } from '../types'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const trashFn = vi.fn<(uids: string[]) => Promise<SessionTrashResult>>()
const emptyFn = vi.fn()
const restoreFn = vi.fn()
const cleanupFn = vi.fn<(days: number, dryRun: boolean) => Promise<SessionStorageCleanup>>()
let inventory: SessionInventoryList
let detail: SessionInventoryDetail

vi.mock('../api/client', () => ({
  api: {
    sessionInventory: () => Promise.resolve(inventory),
    sessionInventoryDetail: () => Promise.resolve(detail),
    sessionInventoryTrash: (...args: unknown[]) => trashFn(...(args as [string[]])),
    sessionStorageCleanup: (...args: unknown[]) => cleanupFn(...(args as [number, boolean])),
    sessionStorageRestore: (...args: unknown[]) => { restoreFn(...args); return Promise.resolve({ restored: 1 }) },
    sessionStorageEmpty: (...args: unknown[]) => { emptyFn(...args); return Promise.resolve({ freed_bytes: 100 }) },
  },
}))

function baseInventory(over: Partial<SessionInventoryList> = {}): SessionInventoryList {
  return {
    total_bytes: 27_400_000_000,
    total_sessions: 612,
    reclaimable_bytes: 24_100_000_000,
    reclaim_blocked_reason: '',
    sessions: [
      { uid: 'dashboard_chat-70', title: 'Refactor the ACP adapter', origin: 'dashboard · chat-70', bytes: 3_810_000_000, mtime: 1752480000, active: false, live: false, background: false },
      { uid: 'dashboard_chat-52', title: 'Sydney property platform', origin: 'dashboard · chat-52', bytes: 536_000_000, mtime: 1751500000, active: false, live: false, background: false },
      { uid: 'dashboard_chat-88', title: 'Storage screen redesign', origin: 'dashboard · chat-88', bytes: 12_400_000, mtime: Date.now() / 1000, active: true, live: false, background: false },
      { uid: 'subagent_a1', title: '', origin: 'subagent · a1', bytes: 50_000_000, mtime: 1752000000, active: false, live: false, background: true },
      { uid: 'subagent_a2', title: '', origin: 'subagent · a2', bytes: 30_000_000, mtime: 1752000000, active: false, live: false, background: true },
    ],
    // The group's real size, which the server sends because the rows above are
    // only a capped sample of it. Here they happen to be the whole group.
    background: { sessions: 2, bytes: 80_000_000, listed: 2 },
    age_options: [
      { days: 7, sessions: 480, bytes: 24_000_000_000 },
      { days: 30, sessions: 300, bytes: 18_000_000_000 },
      { days: 90, sessions: 120, bytes: 9_000_000_000 },
    ],
    trash: { bytes: 0, still_on_disk: true, instant: true, batches: [] },
    ...over,
  }
}

function withTrash(): SessionInventoryList {
  return baseInventory({
    trash: {
      bytes: 1_920_000_000, still_on_disk: true, instant: true,
      batches: [{
        batch_id: '20260808T041500-ab12cd34', created_at: 1752480000, reason: 'manual',
        sessions: 3, bytes: 1_920_000_000,
      }],
    },
  })
}

describe('SessionStorageScreen (inventory)', () => {
  beforeEach(() => {
    trashFn.mockClear(); emptyFn.mockClear(); restoreFn.mockClear()
    // mockReset, not mockClear: one test installs a never-resolving implementation
    // to hold a request in flight, and mockClear would leave it in place for the
    // next test (whose select would then stay disabled).
    cleanupFn.mockReset()
    trashFn.mockResolvedValue({ sessions: 1, bytes: 100, batch_id: 'b1', refused: [] })
    cleanupFn.mockResolvedValue({ sessions: 300, bytes: 18_000_000_000, remaining: 0 })
    inventory = baseInventory()
    detail = { uid: 'dashboard_chat-52', first_message: 'Build a search that beats Domain', turns: 248, images: 58, bytes: 536_000_000, mtime: 1751500000 }
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows foreground sessions as rows', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Refactor the ACP adapter')).toBeTruthy())
    expect(screen.getByText('Sydney property platform')).toBeTruthy()
    expect(screen.getByText('Storage screen redesign')).toBeTruthy()
  })

  it('collapses background sessions into one group row', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
    // Individual subagent origin lines should NOT be visible by default
    expect(screen.queryByText('subagent · a2')).toBeNull()
  })

  it('expands background group to reveal members', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Background agents/))
    await waitFor(() => expect(screen.getByText('subagent · a2')).toBeTruthy())
  })

  /**
   * Both states are refused, so the checkbox is disabled either way. What must not
   * happen is calling a month-old idle conversation "in use" — a claim the user can
   * disprove by reading the date beside it.
   */
  it('says "in use" only when a turn is running, and "resumable" when merely idle', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Storage screen redesign')).toBeTruthy())

    // The fixture's active row is idle: recorded, so refused, but nothing running.
    expect(screen.getByText('Resumable')).toBeTruthy()
    expect(screen.queryByText('In use')).toBeNull()

    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes.find(cb => (cb as HTMLInputElement).disabled)).toBeTruthy()
  })

  it('says "in use" for a session with a turn in flight', async () => {
    inventory = baseInventory()
    inventory.sessions = inventory.sessions.map(s =>
      s.active ? { ...s, live: true } : s,
    )
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Storage screen redesign')).toBeTruthy())
    expect(screen.getByText('In use')).toBeTruthy()
    expect(screen.queryByText('Resumable')).toBeNull()
  })

  it('offers the blocked reason instead of delete when reclaiming is blocked', async () => {
    inventory = baseInventory({ reclaim_blocked_reason: 'This instance shares its store.' })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('This instance shares its store.')).toBeTruthy())
  })

  /** The payload has no per-store split, and the screen must not invent one. */
  it('never names the two stores it is built on', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Refactor the ACP adapter')).toBeTruthy())
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/kiro-cli/i)
    expect(text).not.toMatch(/two stores/i)
    expect(text).not.toMatch(/transcript/i)
  })

  /**
   * Emptying is the only irreversible step, so one click must never destroy
   * anything: the first arms, the second commits.
   */
  it('requires two clicks to empty a trash batch', async () => {
    inventory = withTrash()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Delete forever/)).toBeTruthy())

    await userEvent.click(screen.getByText(/Delete forever/))
    expect(emptyFn).not.toHaveBeenCalled()

    // Past the arm window, so this is real consent rather than a double-click.
    vi.setSystemTime(Date.now() + 1000)
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))
    expect(emptyFn).toHaveBeenCalledWith(['20260808T041500-ab12cd34'])
  })

  /**
   * The confirm replaces the arm button, so a fast double-click would otherwise
   * land its second click on a destructive button that appeared under a
   * stationary pointer. Two independent guards; this covers the timing one.
   */
  it('ignores a confirm that arrives inside the double-click window', async () => {
    inventory = withTrash()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Delete forever/)).toBeTruthy())

    await userEvent.click(screen.getByText(/Delete forever/))
    // No clock advance: the same instant a real double-click would deliver.
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))
    expect(emptyFn).not.toHaveBeenCalled()
  })

  /** And this covers the layout one: Cancel takes the vacated slot, not the confirm. */
  it('puts Cancel where the arm button was, ahead of the confirm', async () => {
    inventory = withTrash()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Delete forever/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Delete forever/))

    const labels = Array.from(document.querySelectorAll('button')).map(b => b.textContent ?? '')
    const cancelAt = labels.findIndex(t => /Cancel/.test(t))
    const confirmAt = labels.findIndex(t => /Delete forever/.test(t))
    expect(cancelAt).toBeGreaterThanOrEqual(0)
    expect(cancelAt).toBeLessThan(confirmAt)
  })

  it('restores a batch without arming anything', async () => {
    inventory = withTrash()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Restore/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Restore/))
    expect(restoreFn).toHaveBeenCalledWith('20260808T041500-ab12cd34')
  })

  it('bulk-selects and moves to trash', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Refactor the ACP adapter')).toBeTruthy())

    // Select the first two rows (non-active)
    const checkboxes = screen.getAllByRole('checkbox')
    await userEvent.click(checkboxes[0])
    await userEvent.click(checkboxes[1])

    // The bulk strip should appear
    expect(screen.getByText(/2 selected/)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /Move to Trash/ }))
    expect(trashFn).toHaveBeenCalledWith(['dashboard_chat-70', 'dashboard_chat-52'])
  })

  /**
   * A refusal must name the session in a way the reader recognises — and NOT by
   * printing the raw id. A session id is only loosely constrained server-side
   * (`_UNIT_ID_RE` admits the alphanumeric shape of an access-key id), so an id is
   * an action handle, not display text.
   */
  it('names a refused session by its label, not its raw id', async () => {
    trashFn.mockResolvedValueOnce({
      sessions: 0,
      bytes: 0,
      batch_id: '',
      refused: [{ uid: 'dashboard_chat-70', reason: 'in_use' }],
    })

    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Refactor the ACP adapter')).toBeTruthy())

    const checkboxes = screen.getAllByRole('checkbox')
    await userEvent.click(checkboxes[0])
    await userEvent.click(screen.getByRole('button', { name: /Move to Trash/ }))

    await waitFor(() => expect(screen.getByText(/could not be moved/)).toBeTruthy())
    // The refusal line carries the session's title and the reason…
    expect(screen.getByText(/Refactor the ACP adapter: .*in use/i)).toBeTruthy()
    // …and does not print the id.
    expect(screen.queryByText(/dashboard_chat-70:/)).toBeNull()
  })

  /**
   * The machine this screen exists for holds over 166,000 sessions. Scaling the
   * bars with `Math.max(...rows)` turns that into 166,000 function arguments,
   * past the engine's limit, and the RangeError blanks the whole screen — on
   * exactly the install that needs the feature most.
   */
  it('renders a six-figure inventory instead of throwing', async () => {
    const many = Array.from({ length: 170_000 }, (_, i) => ({
      uid: `subagent_${i}`,
      title: '',
      origin: `subagent · ${i}`,
      bytes: 1_000 + i,
      mtime: 1752000000,
      active: false,
      live: false,
      background: true,
    }))
    inventory = baseInventory({
      sessions: many,
      total_sessions: many.length,
      background: { sessions: many.length, bytes: many.length * 1_000, listed: many.length },
    })

    // A spread over this array throws before React ever renders, so reaching the
    // header at all is the assertion.
    expect(() => Math.max(1, ...many.map(s => s.bytes))).toThrow(RangeError)

    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
  })

  /**
   * The list is a capped sample of the replay-only group, so the group's header
   * has to come from the server's own count. Deriving it from the rows is the
   * mistake this pins: it would report 2 where the store holds 168,832.
   */
  it('sizes the background group from the server, not from the rows it received', async () => {
    inventory = baseInventory({
      background: { sessions: 168_832, bytes: 27_000_000_000, listed: 2 },
    })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText(/Background agents \(168,832\)/)).toBeTruthy())
    expect(screen.queryByText(/Background agents \(2\)/)).toBeNull()
  })

  it('says how many of the group it is showing when the list is capped', async () => {
    inventory = baseInventory({
      background: { sessions: 168_832, bytes: 27_000_000_000, listed: 2 },
    })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Background agents/))

    expect(screen.getByText(/Showing the 2 largest of 168,832/)).toBeTruthy()
  })

  it('does not claim a cap when the whole group was sent', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Background agents/))

    expect(screen.queryByText(/Showing the/)).toBeNull()
  })

  /**
   * The bulk of a large store cannot be reached by ticking boxes, so the age
   * sweep is the path that actually frees it. It must preview from the SERVER
   * before committing: the counts that arrived with the listing are already
   * stale, and this is a bulk delete.
   */
  it('previews an age sweep from the server before committing it', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))

    await waitFor(() => expect(cleanupFn).toHaveBeenCalledWith(90, true))
    expect(screen.getByText(/Would move 300 sessions/)).toBeTruthy()
    expect(cleanupFn).toHaveBeenCalledTimes(1)
  })

  /**
   * Both count-bearing strings go through the catalog's plural forms, so a single
   * session does not read "1 sessions". The option label and the preview line are
   * separate keys, so both are checked.
   */
  it('uses the singular form for a single session', async () => {
    inventory = baseInventory({
      age_options: [{ days: 7, sessions: 1, bytes: 4_098 }],
    })
    cleanupFn.mockResolvedValue({ sessions: 1, bytes: 4_098, remaining: 0 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())

    expect(screen.getByText(/1 session ·/)).toBeTruthy()
    expect(screen.queryByText(/1 sessions/)).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))
    await waitFor(() => expect(screen.getByText(/Would move 1 session ·/)).toBeTruthy())
    expect(screen.queryByText(/Would move 1 sessions/)).toBeNull()
  })

  /**
   * A preview must describe the sweep the confirm will run. Changing the
   * threshold while a preview was in flight let the late response render over the
   * new selection — the screen showed one set of numbers while the confirm would
   * have deleted a different set. Two layers guard it, so both are pinned.
   */
  it('locks the threshold while a preview is in flight', async () => {
    let release: (v: SessionStorageCleanup) => void = () => {}
    cleanupFn.mockImplementation(
      () => new Promise<SessionStorageCleanup>(resolve => { release = resolve }),
    )
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))
    await waitFor(() => expect(cleanupFn).toHaveBeenCalledWith(90, true))

    expect(screen.getByLabelText('Reclaim by age')).toBeDisabled()

    release({ sessions: 120, bytes: 9_000_000_000, remaining: 0 })
    await waitFor(() => expect(screen.getByText(/Would move 120 sessions/)).toBeTruthy())
  })

  it('drops a preview when the threshold moves off it', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))
    await waitFor(() => expect(screen.getByText(/Would move 300 sessions/)).toBeTruthy())

    // Radix Select: a change event on the trigger does nothing — open it, then
    // click the option (the repo's established way to drive SimpleSelect).
    fireEvent.click(screen.getByLabelText('Reclaim by age'))
    fireEvent.click(await screen.findByRole('option', { name: /Older than 7 days/ }))

    // No stale numbers, and no destructive confirm bound to them.
    await waitFor(() => expect(screen.queryByText(/Would move 300 sessions/)).toBeNull())
    expect(screen.queryByRole('button', { name: /Move to Trash/ })).toBeNull()
  })

  /**
   * A refused cleanup must say so. Silently re-enabling the button is the same
   * "looks broken, no reason given" symptom this screen exists to remove, and it
   * happens at a destructive moment.
   */
  it('says so when a sweep is refused instead of failing silently', async () => {
    cleanupFn.mockRejectedValue(new Error('reclaim blocked'))
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))

    await waitFor(() => expect(screen.getByText(/could not be run/i)).toBeTruthy())
  })

  it('does not point at the age sweep when that control is hidden', async () => {
    // Blocked hides the sweep, so the truncation note must not tell the reader to
    // use it.
    inventory = baseInventory({
      reclaim_blocked_reason: 'This instance shares its store.',
      background: { sessions: 168_832, bytes: 27_000_000_000, listed: 2 },
    })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Background agents/))

    expect(screen.getByText(/Showing the 2 largest of 168,832/)).toBeTruthy()
    expect(screen.queryByText(/by age above/i)).toBeNull()
  })

  it('reports the remaining count when a sweep exceeds one batch', async () => {
    cleanupFn.mockResolvedValue({ sessions: 200_000, bytes: 9_000_000_000, remaining: 12_345 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))

    await waitFor(() => expect(screen.getByText(/12,345 more to go/)).toBeTruthy())
  })

  it('sweeps by threshold, not by the uids it previewed', async () => {    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Reclaim by age/)).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Preview/ }))
    await waitFor(() => expect(screen.getByText(/Would move 300 sessions/)).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Move to Trash/ }))

    await waitFor(() => expect(cleanupFn).toHaveBeenCalledWith(90, false))
  })

  it('offers no age sweep while reclaiming is refused outright', async () => {
    inventory = baseInventory({ reclaim_blocked_reason: 'This instance shares its store.' })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/shares its store/)).toBeTruthy())

    expect(screen.queryByText(/Reclaim by age/)).toBeNull()
  })

  /**
   * The reported symptom: a greyed checkbox with nothing saying why reads as a
   * broken screen rather than a protected session.
   */
  it('says why a session that cannot be selected is disabled', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Storage screen redesign')).toBeTruthy())

    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[]
    const held = boxes.find(b => b.disabled)
    expect(held).toBeTruthy()
    expect(held!.title).toMatch(/storage cleanup is unavailable/i)
  })

})
