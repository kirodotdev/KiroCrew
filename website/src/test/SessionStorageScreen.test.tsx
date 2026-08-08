import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SessionStorageScreen from '../pages/system/SessionStorageScreen'
import type { SessionInventoryList, SessionInventoryDetail, SessionTrashResult } from '../types'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const trashFn = vi.fn<(uids: string[]) => Promise<SessionTrashResult>>()
const emptyFn = vi.fn()
const restoreFn = vi.fn()
let inventory: SessionInventoryList
let detail: SessionInventoryDetail

vi.mock('../api/client', () => ({
  api: {
    sessionInventory: () => Promise.resolve(inventory),
    sessionInventoryDetail: () => Promise.resolve(detail),
    sessionInventoryTrash: (...args: unknown[]) => trashFn(...(args as [string[]])),
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
    trashFn.mockResolvedValue({ sessions: 1, bytes: 100, batch_id: 'b1', refused: [] })
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
    inventory = baseInventory({ sessions: many, total_sessions: many.length })

    // A spread over this array throws before React ever renders, so reaching the
    // header at all is the assertion.
    expect(() => Math.max(1, ...many.map(s => s.bytes))).toThrow(RangeError)

    renderWithProviders(<SessionStorageScreen onBack={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Background agents/)).toBeTruthy())
  })

})
