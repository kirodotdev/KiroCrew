import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SessionStorageScreen from '../pages/system/SessionStorageScreen'
import type { SessionInventoryList, SessionInventoryDetail, SessionStorageCleanup, SessionStorageEmptyJob, SessionTrashResult } from '../types'

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
/** What GET /empty reports. A test sets it to stage a running or finished job. */
let emptyJob: SessionStorageEmptyJob | null = null
/** When true, POST /empty rejects — a network failure or a refused request. */
let emptyFails = false

function job(over: Partial<SessionStorageEmptyJob> = {}): SessionStorageEmptyJob {
  return {
    job_id: 'empty-1',
    running: true,
    total_bytes: 1_920_000_000,
    freed_bytes: 0,
    error: '',
    skipped: [],
    ...over,
  }
}

vi.mock('../api/client', () => ({
  api: {
    sessionInventory: () => Promise.resolve(inventory),
    sessionInventoryDetail: () => Promise.resolve(detail),
    sessionInventoryTrash: (...args: unknown[]) => trashFn(...(args as [string[]])),
    sessionStorageCleanup: (...args: unknown[]) => cleanupFn(...(args as [number, boolean])),
    sessionStorageRestore: (...args: unknown[]) => { restoreFn(...args); return Promise.resolve({ restored: 1 }) },
    // 202: the delete is only ACCEPTED here. Progress arrives on the status read.
    sessionStorageEmpty: (...args: unknown[]) => {
      emptyFn(...args)
      if (emptyFails) return Promise.reject(new Error('could not start'))
      emptyJob = emptyJob ?? job()
      return Promise.resolve(emptyJob)
    },
    sessionStorageEmptyStatus: () => Promise.resolve({ job: emptyJob }),
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
    emptyJob = null
    emptyFails = false
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

  /* ─────────────────── Pagination ─────────────────── */

  /**
   * Builds `count` conversation rows, biggest first, so the default sort puts
   * `FG 001` at the top and `FG <count>` at the bottom. That makes "which page am
   * I on" assertable by title alone.
   */
  function manyForeground(count: number): SessionInventoryList {
    const rows = Array.from({ length: count }, (_, i) => ({
      uid: `dashboard_chat-${i}`,
      title: `FG ${String(i + 1).padStart(3, '0')}`,
      origin: `dashboard · chat-${i}`,
      bytes: (count - i) * 1_000_000,
      mtime: 1752000000 - i,
      active: false,
      live: false,
      background: false,
    }))
    return baseInventory({
      sessions: rows,
      total_sessions: count,
      background: { sessions: 0, bytes: 0, listed: 0 },
    })
  }

  /**
   * The reported symptom: with a full inventory on one page the Trash section —
   * the only place a staged delete is undone or confirmed — sat below every row in
   * the store, so the screen's own controls were unreachable.
   */
  it('shows one page of rows, not the whole inventory', async () => {
    inventory = manyForeground(45)
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('FG 001')).toBeTruthy())

    expect(screen.getByText('FG 020')).toBeTruthy()
    expect(screen.queryByText('FG 021')).toBeNull()
    expect(screen.getByText('Page 1 of 3')).toBeTruthy()
    expect(screen.getByText('Showing 1–20 of 45')).toBeTruthy()
  })

  it('moves to the next page and back', async () => {
    inventory = manyForeground(45)
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('FG 001')).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('FG 021')).toBeTruthy()
    expect(screen.queryByText('FG 001')).toBeNull()
    expect(screen.getByText('Page 2 of 3')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: 'Previous' }))
    expect(screen.getByText('FG 001')).toBeTruthy()
    expect(screen.getByText('Page 1 of 3')).toBeTruthy()
  })

  it('stops at the last page, which may be short', async () => {
    inventory = manyForeground(45)
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('FG 001')).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('Showing 41–45 of 45')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Next' }) as HTMLButtonElement).disabled).toBe(true)
  })

  /**
   * Page 3 of a one-page result is an empty screen, which is what a kept cursor
   * produces the moment a search narrows the list.
   */
  it('returns to the first page when the search changes', async () => {
    inventory = manyForeground(45)
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('FG 001')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('Page 2 of 3')).toBeTruthy()

    fireEvent.change(screen.getByPlaceholderText('Search sessions'), { target: { value: 'FG' } })
    await waitFor(() => expect(screen.getByText('Page 1 of 3')).toBeTruthy())
    expect(screen.getByText('FG 001')).toBeTruthy()
    expect(screen.queryByText('FG 021')).toBeNull()
  })

  it('offers no pager when the whole list fits on one page', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Refactor the ACP adapter')).toBeTruthy())

    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull()
    expect(screen.queryByText(/^Page /)).toBeNull()
  })

  /**
   * A filtered-to-nothing list used to render as a blank gap, which reads as a
   * screen that failed to load rather than a query with no hits.
   */
  it('says a search matched nothing instead of showing a blank gap', async () => {
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Refactor the ACP adapter')).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText('Search sessions'), { target: { value: 'zzzz' } })
    await waitFor(() => expect(screen.getByText('No conversations match that search.')).toBeTruthy())
    expect(screen.queryByText('Refactor the ACP adapter')).toBeNull()
  })

  /** The replay-only group is capped at 200 rows server-side, so it pages too. */
  it('pages the background group as well', async () => {
    const bg = Array.from({ length: 30 }, (_, i) => ({
      uid: `subagent_${i}`,
      title: '',
      origin: `subagent · ${String(i + 1).padStart(3, '0')}`,
      bytes: (30 - i) * 1_000,
      mtime: 1752000000,
      active: false,
      live: false,
      background: true,
    }))
    inventory = baseInventory({
      sessions: bg,
      total_sessions: 30,
      background: { sessions: 30, bytes: 465_000, listed: 30 },
    })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
    await userEvent.click(screen.getByText(/Background agents/))

    expect(screen.getByText('subagent · 020')).toBeTruthy()
    expect(screen.queryByText('subagent · 021')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('subagent · 021')).toBeTruthy()
  })

  /* ─────────────────── Emptying, while it happens ─────────────────── */

  /**
   * The reported symptom: the three buttons greyed out and nothing else changed,
   * so a delete of tens of thousands of sessions was indistinguishable from a
   * stuck screen. Ten seconds later the batch was gone, which is how the user
   * found out it had worked.
   */
  it('reports a running empty with a partial figure', async () => {
    inventory = withTrash()
    emptyJob = job({ freed_bytes: 480_000_000 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText('Emptying Trash')).toBeTruthy())
    expect(screen.getByText('480MB of 1.9GB freed')).toBeTruthy()
  })

  /**
   * Load-bearing copy, not reassurance: the job runs in the gateway and outlives
   * the request, so leaving really is safe - and a user who does not know that
   * sits and waits, which is what happened.
   */
  it('says the delete survives leaving the page', async () => {
    inventory = withTrash()
    emptyJob = job({ freed_bytes: 1 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(
      screen.getByText('This keeps running if you leave this page.'),
    ).toBeTruthy())
  })

  /**
   * A job already running when the screen mounts belongs to a run the user started
   * before navigating away. Picking it up is the whole reason the progress lives on
   * the server rather than in this component's mutation state.
   */
  it('picks up a delete that was already running before this screen opened', async () => {
    inventory = withTrash()
    emptyJob = job({ freed_bytes: 960_000_000 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText('960MB of 1.9GB freed')).toBeTruthy())
    // Nothing was clicked in this session: the run is the server's, not this
    // component's.
    expect(emptyFn).not.toHaveBeenCalled()
  })

  it('says what a finished empty freed', async () => {
    inventory = withTrash()
    emptyJob = job({ running: false, freed_bytes: 1_920_000_000 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText('Freed 1.9GB.')).toBeTruthy())
    expect(screen.queryByText('Emptying Trash')).toBeNull()
  })

  /**
   * The request is answered before the delete runs, so a refusal has nowhere else
   * to surface. Silence here is the old behaviour: the batch simply stayed.
   */
  it('says so when a delete stopped instead of leaving the batch unexplained', async () => {
    inventory = withTrash()
    emptyJob = job({ running: false, freed_bytes: 4_100_000_000, error: 'the trash root moved' })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText(/the trash root moved/)).toBeTruthy())
    // The raw gateway sentence is its own line now, so the block holds both halves:
    // a translated sentence that stands alone, and the technical detail under it.
    const block = screen.getByText(/the trash root moved/).closest('[role="status"]')
    expect(block?.textContent).toMatch(/Stopped after freeing 4.1GB\./)
  })

  /**
   * `onSettled` disarms the confirm on any outcome, which is what lets a 409 tab pick
   * up the running job - and it left a FAILED post with a cleared button and nothing
   * on screen, so the click read as accepted while nothing ran.
   */
  it('says so when the delete could not even be started', async () => {
    inventory = withTrash()
    emptyFails = true
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Delete forever/)).toBeTruthy())

    await userEvent.click(screen.getByText(/Delete forever/))
    vi.setSystemTime(Date.now() + 1000)
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))

    await waitFor(() => expect(
      screen.getByText("Couldn't start emptying the Trash. Try again."),
    ).toBeTruthy())
  })

  /**
   * react-query keeps `isError` until the next mutate, so a 409 (a second tab) left
   * the error latched while the poll picked up the REAL job - and the moment that job
   * settled the screen said the delete never ran directly above what it freed. On the
   * one surface whose job is answering "did the irreversible thing happen", the answer
   * has to be the job.
   */
  it('lets a picked-up job win over a latched start error', async () => {
    inventory = withTrash()
    emptyFails = true
    // The server already has a settled job, as a second tab would find after its 409.
    emptyJob = job({ running: false, freed_bytes: 1_920_000_000 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Delete forever/)).toBeTruthy())

    await userEvent.click(screen.getByText(/Delete forever/))
    vi.setSystemTime(Date.now() + 1000)
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))

    await waitFor(() => expect(emptyFn).toHaveBeenCalled())
    expect(screen.getByText('Freed 1.9GB.')).toBeTruthy()
    expect(screen.queryByText("Couldn't start emptying the Trash. Try again.")).toBeNull()
  })

  /**
   * The run disables controls at the TOP of a long list while its progress line sits
   * in the Trash section far below, so the reason has to be visible where the dead
   * controls are.
   */
  it('says why the controls are paused, at the controls', async () => {
    inventory = withTrash()
    emptyJob = job({ running: true, freed_bytes: 1_000_000 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() =>
      expect(
        screen.getByText('Emptying the Trash. Some actions are paused until it finishes.')
      ).toBeTruthy()
    )
  })

  /** A half-finished irreversible delete must not explain itself only in English. */
  it('offers a translated next step when the delete crashes', async () => {
    inventory = withTrash()
    emptyJob = job({ running: false, freed_bytes: 512, error: 'OSError: disk went away' })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() =>
      expect(
        screen.getByText('Try Delete forever again. Details are in the gateway log.')
      ).toBeTruthy()
    )
    // The gateway's own words stay, as the detail rather than the explanation.
    expect(screen.getByText('OSError: disk went away')).toBeTruthy()
  })

  /** The log only names a file for the unlisted-file case, so the hint is gated. */  it('offers the log hint only for the reason that has a file to name', async () => {
    inventory = withTrash()
    emptyJob = job({ running: false, freed_bytes: 10, skipped: ['unreadable_batch'] })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText(/not everything could be deleted/)).toBeTruthy())
    const line = screen.getByText(/not everything could be deleted/).textContent ?? ''
    expect(line).toMatch(/could not be read in full/)
    expect(line).not.toMatch(/gateway log/)
  })

  /**
   * A kept batch raises nothing: `_empty_trash_locked` logs and moves on, because
   * the files it holds are the only copy. Read only from the error, the job settled
   * clean and this rendered the success line "Freed 0 B." directly above a batch
   * that was still listed.
   */
  it('does not call a kept batch a success', async () => {
    inventory = withTrash()
    emptyJob = job({ running: false, freed_bytes: 0, skipped: ['unlisted_files'] })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(
      screen.getByText(/not everything could be deleted/),
    ).toBeTruthy())
    expect(screen.getByText(/manifest does not list/)).toBeTruthy()
    expect(screen.queryByText('Freed 0B.')).toBeNull()
  })

  /**
   * An explanation with no next step is still a dead end: the row offers the same
   * Delete forever, which refuses again, so the notice has to end with something
   * the user can actually do.
   */
  it('ends a kept-batch refusal with a next step', async () => {
    inventory = withTrash()
    emptyJob = job({ running: false, freed_bytes: 0, skipped: ['unlisted_files'] })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText(/not everything could be deleted/)).toBeTruthy())
    const notice = screen.getByText(/not everything could be deleted/).textContent ?? ''
    expect(notice).toMatch(/Restore what you can/)
    expect(notice).toMatch(/gateway log/)
  })

  /**
   * The byte figure changes on every poll, so a live region containing it makes a
   * screen reader speak about once a second for a job that runs for minutes. The
   * STATE is announced; the counter is readable but not read out.
   */
  it('announces the state, not the counter', async () => {
    inventory = withTrash()
    emptyJob = job({ freed_bytes: 480_000_000 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Emptying Trash')).toBeTruthy())

    expect(screen.getByText('Emptying Trash').getAttribute('role')).toBe('status')
    const counter = screen.getByText('480MB of 1.9GB freed')
    expect(counter.getAttribute('aria-live')).toBe('off')
    expect(counter.closest('[role="status"]')).toBeNull()
  })

  /** Two batches kept for the same reason is the same sentence, once. */
  it('states a repeated keep reason once', async () => {
    inventory = withTrash()
    emptyJob = job({
      running: false,
      freed_bytes: 10,
      skipped: ['unlisted_files', 'unlisted_files'],
    })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)

    await waitFor(() => expect(screen.getByText(/not everything could be deleted/)).toBeTruthy())
    const line = screen.getByText(/not everything could be deleted/).textContent ?? ''
    expect(line.match(/manifest does not list/g)?.length).toBe(1)
  })

  it('holds the other actions while a delete is running', async () => {
    inventory = withTrash()
    emptyJob = job({ freed_bytes: 1 })
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText('Emptying Trash')).toBeTruthy())

    const restore = screen.getByRole('button', { name: 'Restore' }) as HTMLButtonElement
    expect(restore.disabled).toBe(true)
  })

  /** The confirm is disarmed by acceptance, not by the delete finishing. */
  it('disarms the confirm as soon as the empty is accepted', async () => {
    inventory = withTrash()
    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Trash/)).toBeTruthy())

    await userEvent.click(screen.getByText(/Delete forever/))
    // Past the arm window, so this is consent and not a double-click.
    vi.setSystemTime(Date.now() + 1000)
    await userEvent.click(screen.getByRole('button', { name: /Delete forever/ }))

    await waitFor(() => expect(emptyFn).toHaveBeenCalledWith(['20260808T041500-ab12cd34']))
    await waitFor(() => expect(screen.getByText('Emptying Trash')).toBeTruthy())
  })

})
