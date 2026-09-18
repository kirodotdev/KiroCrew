/**
 * The Memory tab's store picker: what it lists, and what a switch does to the
 * cards under it.
 *
 * The failure guarded against here is a switch that changes the LABEL without
 * changing the request. Every store-scoped read carries the picked store as
 * `?store=<name>`, so a card served from another store's rows shows one crew's
 * memory under a second crew's name and nothing in the response says so — which
 * is why the store argument of each call is asserted rather than the rendered
 * rows alone.
 *
 * The api client is stubbed instead of answered by MSW precisely because that
 * argument is the assertion; `integration/MemoryTab.integration.test.tsx` drives
 * the same tab over the wire. Two cards are also mounted on their own, where the
 * behaviour under test belongs to the card and the surrounding tab would only
 * add rows a query has to be disambiguated against.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { MemoryBackup, MemoryStoreSummary } from '../types'
import MemoryTab from '../pages/overview/MemoryTab'
import MemoryCarveCard from '../pages/overview/MemoryCarveCard'
import MemoryBackupsCard from '../pages/overview/MemoryBackupsCard'
import MemoryRetiredCard from '../pages/overview/MemoryRetiredCard'
import MemoryStoreCard from '../pages/overview/MemoryStoreCard'

const { api } = vi.hoisted(() => ({
  api: {
    memoryStores: vi.fn(),
    memberMemoryPage: vi.fn(),
    memoryRecords: vi.fn(),
    memoryPreferences: vi.fn(),
    memoryProjects: vi.fn(),
    memoryHistory: vi.fn(),
    saveMemoryPreferences: vi.fn(),
    saveMemoryProjects: vi.fn(),
    saveMemoryHistory: vi.fn(),
    memorySettings: vi.fn(),
    memoryCarve: vi.fn(),
    memoryRetired: vi.fn(),
    memoryRestoreRetired: vi.fn(),
    memoryBackups: vi.fn(),
    memoryBackupNow: vi.fn(),
    memoryRestoreBackup: vi.fn(),
    lessons: vi.fn(),
    // ThemeProvider (part of the shared render wrapper) reads these on mount.
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))
// Both cards own their own queries, their own store scope and their own tests.
// Here they are only the seams whose vector/migration state this tab branches on.
vi.mock('../pages/overview/VectorMemoryCard', async importOriginal => ({
  ...await importOriginal<typeof import('../pages/overview/VectorMemoryCard')>(),
  default: () => <div data-testid="vector-card" />,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({
  default: () => <div data-testid="embed-card" />,
}))

/** Accessible name of the store picker, from `memoryStoreCard.memory_store`. */
const PICKER_LABEL = 'Memory store'

const DEFAULT_STORE = 'default'
const CREW_STORE = 'finance'
const UNREAD_STORE = 'support'

/** Three stores, in the gateway's own order (default first, then sorted) —
 *  asserted as that order, because the listing is not re-sorted client side.
 *  `support` carries null counts, the listing's "not known" answer for a silo it
 *  could not read. */
const STORES: MemoryStoreSummary[] = [
  {
    name: DEFAULT_STORE, is_default: true, lineage: 'v1', exists: true,
    semantic_count: 41, episodic_count: 7, lessons_count: 2,
    facets_supported: false, backup_count: 3, newest_backup: '2026-02-01T09:00:00Z',
  },
  {
    name: CREW_STORE, is_default: false, lineage: 'crew', exists: true,
    semantic_count: 5, episodic_count: 1, lessons_count: 0,
    facets_supported: true, backup_count: 0, newest_backup: null,
  },
  {
    name: UNREAD_STORE, is_default: false, lineage: 'crew', exists: true,
    semantic_count: null, episodic_count: null, lessons_count: null,
    facets_supported: true, backup_count: null, newest_backup: null,
  },
]

const BACKUPS: MemoryBackup[] = [
  { name: 'memory-20260201T090000Z.db', size_bytes: 4096, taken_at: '2026-02-01T09:00:00Z' },
  { name: 'memory-20260131T090000Z.db', size_bytes: 2048, taken_at: '2026-01-31T09:00:00Z' },
]

/**
 * A refusal shaped like the transport's `ApiError`.
 *
 * The cards read `status` and the body's machine-readable `code` structurally
 * rather than through `instanceof`, so carrying both fields is what makes this
 * indistinguishable from a real one — and `status` is load-bearing beyond the
 * rendered sentence: a settled refusal status is what stops the query's retry
 * ladder, so omitting it would make the test wait out real backoff.
 */
class Refusal extends Error {
  readonly status: number
  readonly body: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = JSON.stringify({ error: message, code })
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
  // useSortableTable persists the lessons sort here; a leaked order would make
  // an unrelated later assertion order-dependent.
  localStorage.clear()
  // `active` is the store this caller already reads with NO `store=` on the wire.
  // The picker displays it as selected while sending nothing for it, so a default
  // install's reads stay ungated; omitting it here leaves the picker showing no
  // selection at all.
  api.memoryStores.mockResolvedValue({ stores: STORES, active: 'default' })
  api.memberMemoryPage.mockResolvedValue({ entries: [] })
  api.memoryRecords.mockResolvedValue({ entries: [], total: 0, has_more: false })
  api.memoryPreferences.mockResolvedValue({ content: 'zzq-prefs' })
  api.memoryProjects.mockResolvedValue({ content: 'zzq-projects' })
  api.memoryHistory.mockResolvedValue({ content: 'zzq-history' })
  api.saveMemoryPreferences.mockResolvedValue({ ok: true })
  api.saveMemoryProjects.mockResolvedValue({ ok: true })
  api.saveMemoryHistory.mockResolvedValue({ ok: true })
  api.memorySettings.mockResolvedValue({
    history_idle_hours: 3, history_max_days: 90, migrated: false,
  })
  api.memoryCarve.mockResolvedValue({ counts: { slack: 3 } })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryRestoreRetired.mockResolvedValue({ ok: true })
  api.memoryBackups.mockResolvedValue({ backups: BACKUPS })
  api.memoryBackupNow.mockResolvedValue({ backed_up: 1, skipped: 0, pruned: 0, failed: 0 })
  api.memoryRestoreBackup.mockResolvedValue({ ok: true, superseded: 'memory.superseded.db' })
  api.lessons.mockResolvedValue({ lessons: [] })
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({ slug: 'zzq-theme' })
  api.themeBoot.mockResolvedValue({})
})

/** The picker's trigger, once the listing has landed and seeded it. */
async function pickerSeeded(): Promise<HTMLElement> {
  const picker = await screen.findByRole('combobox', { name: PICKER_LABEL })
  // The listing arrives AFTER the mount, so wait for the picker to DISPLAY the
  // active store before asserting anything about the reads. Displaying it is not
  // the same as naming it on the wire: the active store is sent as no parameter at
  // all, so the reads below assert `undefined`.
  await waitFor(() => expect(picker).toHaveTextContent('Shared · Global Memory V1'))
  return picker
}

/** Open the picker and choose `name` from the popup. */
async function pickStore(picker: HTMLElement, name: string): Promise<void> {
  fireEvent.click(picker)
  fireEvent.click(await screen.findByRole('option', { name }))
}

describe('memory store picker — the listing', () => {
  it('selects existing stores without offering detached store creation', async () => {
    const onStoreChange = vi.fn()
    renderWithProviders(<MemoryStoreCard store="default" onStoreChange={onStoreChange} />)
    const picker = await pickerSeeded()
    expect(screen.getByText('Semantic', { exact: true })).toHaveTextContent('Semantic 41')
    expect(screen.getByText('Episodic', { exact: true })).toHaveTextContent('Episodic 7')
    expect(screen.queryByText(/^Facts and lessons:/)).toBeNull()
    expect(screen.queryByText(/^Experiences:/)).toBeNull()
    expect(screen.queryByText(/^Lessons:/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'New store', exact: true })).toBeNull()
    expect(screen.queryByRole('dialog', { name: 'New memory store' })).toBeNull()
    await pickStore(picker, CREW_STORE)
    expect(onStoreChange).toHaveBeenCalledExactlyOnceWith(CREW_STORE)
  })

  it.each([
    { version: 1 as const, semantic: 'Semantic', episodic: 'Episodic' },
    { version: 2 as const, semantic: 'Facts and lessons:', episodic: 'Experiences:' },
  ])('uses the existing V$version vocabulary for a named store summary', async ({ version, semantic, episodic }) => {
    api.memoryStores.mockResolvedValue({
      stores: STORES.map(row => row.name === CREW_STORE ? { ...row, memory_version: version } : row),
      active: 'default',
    })
    renderWithProviders(<MemoryStoreCard store={CREW_STORE} onStoreChange={vi.fn()} />)
    expect(await screen.findByText(semantic, { exact: true })).toHaveTextContent(`${semantic} 5`)
    expect(screen.getByText(episodic, { exact: true })).toHaveTextContent(`${episodic} 1`)
    expect(screen.queryByText(version === 1 ? 'Facts and lessons:' : 'Semantic', { exact: true })).toBeNull()
    expect(screen.queryByText(version === 1 ? 'Experiences:' : 'Episodic', { exact: true })).toBeNull()
  })

  it('lists every declared store and names Global V1 once', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const picker = await pickerSeeded()

    expect(picker).toHaveTextContent('Shared · Global Memory V1')
    expect(screen.queryByText('Shared default store')).toBeNull()

    fireEvent.click(picker)
    const options = (await screen.findAllByRole('option')).map(o => o.textContent)
    expect(options).toEqual(['Shared · Global Memory V1', CREW_STORE, UNREAD_STORE])
  })

  it('names the V2 owner while re-reading every card under the selected store', async () => {
    api.memoryStores.mockResolvedValue({
      stores: STORES.map(row => row.name === CREW_STORE
        ? { ...row, memory_version: 2, owner_member: 'finance-reviewer' }
        : row),
      active: 'default',
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const picker = await pickerSeeded()
    // `undefined`, not `'default'`. The default store here IS the active store, and
    // the picker sends no parameter for it: the gateway reads an absent `?store=` as
    // the global store, while a PRESENT one takes the owner gate. Sending
    // `store=default` would gate a read that needs no gate.
    await waitFor(() => expect(api.memoryPreferences).toHaveBeenCalledWith(undefined))
    await waitFor(() => expect(api.memoryBackups).toHaveBeenCalledWith(undefined))
    vi.clearAllMocks()

    await pickStore(picker, 'Private to finance-reviewer · Memory V2')
    await waitFor(() => expect(picker).toHaveAttribute('title', 'Private to finance-reviewer · Memory V2'))
    fireEvent.mouseDown(await screen.findByRole('tab', { name: 'Profile' }), { button: 0, ctrlKey: false })

    // Every store-scoped read on the page, re-issued under the new name. The
    // query key carries the store, so a card that failed to thread it would
    // serve the previous store's cached rows and never appear here.
    await waitFor(() => expect(api.memoryPreferences).toHaveBeenCalledWith(CREW_STORE))
    expect(api.memoryProjects).toHaveBeenCalledWith(CREW_STORE)
    expect(api.memoryRecords).toHaveBeenCalledWith(CREW_STORE, { q: '', kind: 'all' }, 0, 50)
    expect(api.memoryHistory).not.toHaveBeenCalled()
    expect(api.memorySettings).not.toHaveBeenCalled()
    expect(api.lessons).not.toHaveBeenCalled()
    expect(screen.queryByTestId('vector-card')).toBeNull()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Recovery' }), { button: 0, ctrlKey: false })
    await waitFor(() => expect(api.memoryRetired).toHaveBeenCalledWith(CREW_STORE, expect.any(Number)))
    expect(api.memoryBackups).toHaveBeenCalledWith(CREW_STORE)
    const advanced = screen.getByText('Advanced memory analysis').closest('details')!
    advanced.open = true
    fireEvent(advanced, new Event('toggle'))
    await waitFor(() => expect(api.memoryCarve).toHaveBeenCalledWith(expect.objectContaining({ store: CREW_STORE })))
    // Nothing was re-read under the store the user just left, which for the active
    // store means no storeless read either.
    expect(api.memoryPreferences).not.toHaveBeenCalledWith(undefined)
    expect(api.memoryBackups).not.toHaveBeenCalledWith(undefined)

    // The catalog's owner supplies the heading, while the distinct store name
    // scopes every request above. Neither identity comes from the other.
    expect(await screen.findByRole('heading', { name: 'Memory for finance-reviewer' })).toBeInTheDocument()
    expect(screen.queryByText('Shared default store')).toBeNull()
  })

  it('explains an owner-only refusal instead of blanking the card', async () => {
    api.memoryStores.mockRejectedValue(
      new Refusal(403, 'owner_only', 'zzq-owner-gate-server-sentence'),
    )
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const notice = await screen.findByText(
      'Only the signed-in owner can choose a store. No selection means Global Memory V1.',
    )
    expect(notice.closest('[role="alert"]')).not.toBeNull()

    // Private stores are provisioned with members. The Memory page keeps its
    // disabled picker and never offers a detached arbitrary store creation.
    const picker = screen.getByRole('combobox', { name: PICKER_LABEL })
    expect(picker).toBeDisabled()
    expect(screen.queryByRole('button', { name: /New store/ })).toBeNull()

    // A caller who cannot enumerate stores keeps the page it had: every read
    // stays storeless, so the gateway serves the global store.
    await waitFor(() => expect(api.memoryPreferences).toHaveBeenCalledWith(undefined))
    expect(api.memoryPreferences).not.toHaveBeenCalledWith(expect.any(String))
    expect(api.memoryBackups).not.toHaveBeenCalledWith(expect.any(String))
  })
})

describe('memory store picker — a store whose schema has no facets', () => {
  it('shows one muted Global V1 explanation without making an inapplicable request', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await pickerSeeded()
    const explanations = screen.getAllByText(/This format has no source groups\./)
    expect(explanations).toHaveLength(1)
    expect(explanations[0]).toHaveClass('text-muted')
    expect(explanations[0].closest('[role="alert"]')).toBeNull()
    expect(api.memoryCarve).not.toHaveBeenCalled()
    expect(screen.queryByRole('combobox', { name: 'Count by' })).toBeNull()
  })

  it('uses a named store’s declared capability before requesting groups', async () => {
    api.memoryStores.mockResolvedValue({ stores: STORES.map(row => row.name === CREW_STORE ? { ...row, facets_supported: false } : row), active: 'default' })
    renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)
    const explanation = await screen.findByText(/This format has no source groups\./)
    expect(explanation).toHaveClass('text-muted')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(api.memoryCarve).not.toHaveBeenCalled()
    expect(screen.queryByRole('combobox', { name: 'Count by' })).toBeNull()
  })

  it('waits for the selected group before claiming that no memories match', async () => {
    let finish!: (value: { entries: [] }) => void
    const pending = new Promise<{ entries: [] }>(resolve => { finish = resolve })
    api.memoryCarve.mockImplementation((request: { countBy?: string }) => request.countBy
      ? Promise.resolve({ counts: { slack: 1 } })
      : pending)
    renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)
    fireEvent.click(await screen.findByRole('button', { name: /slack.*1/i }))
    expect(screen.getByRole('status')).toBeVisible()
    expect(screen.queryByText('No memories match your search')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
    finish({ entries: [] })
    expect(await screen.findByText('No memories match your search')).toBeVisible()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('uses owner-facing group labels while keeping the request identifiers', async () => {
    api.memoryCarve.mockResolvedValue({ counts: { directive: 2 } })
    renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)
    expect(await screen.findByText('Explore memory')).toBeVisible()
    const grouping = screen.getByRole('combobox', { name: 'Count by' })
    expect(grouping).toHaveTextContent('Channel')
    fireEvent.click(grouping)
    fireEvent.click(await screen.findByRole('option', { name: 'Memory type' }))
    await waitFor(() => expect(api.memoryCarve).toHaveBeenCalledWith({ store: CREW_STORE, countBy: 'kind' }))
    fireEvent.click(await screen.findByRole('button', { name: /Lesson.*2/ }))
    await waitFor(() => expect(api.memoryCarve).toHaveBeenCalledWith({ store: CREW_STORE, kind: 'directive', limit: 50 }))
  })

  it('keeps every grouped-memory field readable in the narrow card layout', async () => {
    api.memoryCarve.mockImplementation((request: { countBy?: string }) => request.countBy
      ? Promise.resolve({ counts: { slack: 1 } })
      : Promise.resolve({ entries: [{
          id: 'episode:narrow', kind: 'episode',
          text: 'A long remembered correction that must wrap inside the narrow card',
          crew: 'reviewer-with-a-long-name', surface: 'slack-thread-with-a-long-name',
        }] }))
    renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)
    fireEvent.click(await screen.findByRole('button', { name: /slack.*1/i }))

    const narrow = await screen.findByTestId('memory-carve-mobile-results')
    expect(narrow).toHaveClass('sm:hidden')
    expect(within(narrow).getByText('Experience')).toBeVisible()
    expect(within(narrow).getByText('A long remembered correction that must wrap inside the narrow card')).toHaveClass('break-words')
    expect(within(narrow).getByText('Member')).toBeVisible()
    expect(within(narrow).getByText('reviewer-with-a-long-name')).toHaveClass('break-words')
    expect(within(narrow).getByText('Channel')).toBeVisible()
    expect(within(narrow).getByText('slack-thread-with-a-long-name')).toHaveClass('break-words')
    expect(screen.getByRole('table').parentElement).toHaveClass('hidden', 'overflow-x-auto', 'sm:block')
  })

  it('keeps a rejected group request visible when the listing advertised support', async () => {
    api.memoryCarve.mockRejectedValue(
      new Refusal(409, 'facets_unsupported', 'zzq-facets-unsupported-server-sentence'),
    )
    renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('zzq-facets-unsupported-server-sentence')
    expect(screen.queryByText(/This format has no source groups\./)).toBeNull()

    expect(screen.queryByText('No memories in this group yet')).toBeNull()
    expect(screen.queryByText('No memories match your search')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
    // And the card still says what it is.
    expect(screen.getByText('Explore memory')).toBeInTheDocument()
  })
})

describe('memory store picker — restoring a backup', () => {
  /** The first backup row of the mounted card. Row 0 is the header.
   *
   *  The table is mounted before the listing lands, carrying an empty-state row
   *  in its place, so waiting on the table alone hands back that row and queries
   *  it for a Restore backup button only a real backup has. Wait for the rows. */
  async function firstBackupRow(): Promise<HTMLElement> {
    const table = await screen.findByRole('table')
    await waitFor(() => expect(within(table).getAllByRole('row'))
      .toHaveLength(BACKUPS.length + 1))
    return within(table).getAllByRole('row')[1]
  }

  it('arms on the first click and posts nothing until the restore is confirmed', async () => {
    renderWithProviders(<MemoryBackupsCard store={CREW_STORE} />)
    const row = await firstBackupRow()

    fireEvent.click(within(row).getByRole('button', { name: /^Restore backup$/ }))

    // Only the second click stages a restore for the next restart. Asserted as
    // an absent request rather than an absent dialog: a confirmation the
    // mutation does not actually wait for is indistinguishable on screen.
    expect(api.memoryRestoreBackup).not.toHaveBeenCalled()
    const warning = within(row).getByText(/Stage this backup for the next Kiro Crew gateway restart/)
    expect(warning).toHaveTextContent('Current memory is saved beside the store before restoration.')

    fireEvent.click(within(row).getByRole('button', { name: /Confirm restore/ }))
    await waitFor(() => expect(api.memoryRestoreBackup)
      .toHaveBeenCalledWith(BACKUPS[0].name, CREW_STORE))
  })

  it('leaves the database alone when an armed restore is cancelled', async () => {
    renderWithProviders(<MemoryBackupsCard store={CREW_STORE} />)
    const row = await firstBackupRow()

    fireEvent.click(within(row).getByRole('button', { name: /^Restore backup$/ }))
    fireEvent.click(within(row).getByRole('button', { name: /^Cancel$/ }))

    // Disarmed: the row offers the two-click path again from the start.
    expect(within(row).getByRole('button', { name: /^Restore backup$/ })).toBeInTheDocument()
    expect(api.memoryRestoreBackup).not.toHaveBeenCalled()
  })
})

describe('global retired memory restoration', () => {
  it('invalidates the canonical global live-memory queries after restoring an episode', async () => {
    const retired = {
      id: 'retired-global',
      text: 'An earlier global experience',
      superseded_by: null,
      retired_times: 1,
      ts: '2026-09-09T10:00:00Z',
    }
    api.memoryRetired.mockResolvedValue({ retired: [retired] })
    const { queryClient } = renderWithProviders(<MemoryRetiredCard store="" />)
    const globalStatsKey = ['member-memory', 'default', 'vector-stats']
    queryClient.setQueryData(globalStatsKey, { semantic_count: 1, episodic_count: 1 })

    const row = (await screen.findByText(retired.text)).closest('tr')!
    fireEvent.click(within(row).getByRole('button', { name: 'Restore experience', exact: true }))

    await waitFor(() => expect(api.memoryRestoreRetired)
      .toHaveBeenCalledExactlyOnceWith(retired.id, undefined))
    await waitFor(() => expect(queryClient.getQueryState(globalStatsKey)?.isInvalidated).toBe(true))
  })
})
