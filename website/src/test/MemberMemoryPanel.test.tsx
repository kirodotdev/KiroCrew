/** Exercise the actual member surface and store switcher together. A correct
 * label alone cannot establish isolation: assertions name every read/write's
 * store, including the explicit source reads allowed only after choosing copy. */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import MemoryTab from '../pages/overview/MemoryTab'
import MemberMemoryPanel from '../pages/overview/MemberMemoryPanel'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import SidePanelLayout from '../components/SidePanelLayout'
import CrewAvatar, { seededTraits } from '../components/CrewAvatar'
import { useLocation } from 'react-router-dom'
import type { MemoryStoreSummary } from '../types'

const { api } = vi.hoisted(() => ({ api: {
  memoryStores: vi.fn(), memoryRecords: vi.fn(), memoryEditPreview: vi.fn(), memoryEditPreviewPage: vi.fn(), memoryEditApply: vi.fn(), memoryRecordsRefresh: vi.fn(), memoryRecordHistory: vi.fn(), memberMemoryPage: vi.fn(), memorySeed: vi.fn(), memoryRecall: vi.fn(),
  vectorSemanticWrite: vi.fn(), vectorSemanticDelete: vi.fn(), vectorEpisodicDelete: vi.fn(),
  memoryPreferences: vi.fn(), memoryProjects: vi.fn(), memoryHistory: vi.fn(),
  saveMemoryPreferences: vi.fn(), saveMemoryProjects: vi.fn(),
  memoryCarve: vi.fn(), memoryRetired: vi.fn(), memoryRestoreRetired: vi.fn(), memoryBackups: vi.fn(), memoryBackupNow: vi.fn(), memoryRestoreBackup: vi.fn(), cancelMemberMemoryRestore: vi.fn(), memorySettings: vi.fn(), lessons: vi.fn(),
  themes: vi.fn(), themeDetail: vi.fn(), themeBoot: vi.fn(),
} }))
vi.mock('../api/client', () => ({ api }))
vi.mock('../pages/overview/VectorMemoryCard', async importOriginal => ({
  ...await importOriginal<typeof import('../pages/overview/VectorMemoryCard')>(),
  default: () => <div data-testid="global-vector-card" />,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div data-testid="global-embedding-card" /> }))

const MEMBER_STORE = 'member-reviewer-abc'
const OTHER_STORE = 'member-writer-def'
const LEGACY_STORE = 'shared-legacy-notes'
const stores: MemoryStoreSummary[] = [
  { name: 'default', is_default: true, exists: true, lineage: 'v1', semantic_count: 1, episodic_count: 0, lessons_count: 0, facets_supported: false, backup_count: 0, newest_backup: null },
  { name: LEGACY_STORE, memory_version: 1, is_default: false, exists: true, lineage: 'v1', semantic_count: 1, episodic_count: 0, lessons_count: 0, facets_supported: false, backup_count: 0, newest_backup: null },
  ...[[MEMBER_STORE, 'reviewer'], [OTHER_STORE, 'writer']].map(([name, owner_member]) => ({ name, owner_member, memory_version: 2, is_default: false, exists: true, lineage: 'crew', semantic_count: 1, episodic_count: 1, lessons_count: 0, facets_supported: true, backup_count: 0, newest_backup: null })),
]
const FACT = { key: 'preference.review', value_json: 'Review carefully', source: 'explicit owner correction', derived_from: '{"source_store":"default","source_key":"preference.review"}' }
const EPISODE = { id: 'experience-1', text: 'A previous review found a missing guard', source: 'member session' }
const REVIEWER_AVATAR = { kind: 'image', v: 17 }
const WRITER_AVATAR = { kind: 'ghost', traits: seededTraits('writer-custom-face') }
const avatarStores = () => stores.map(s => ({ ...s, owner_avatar: s.name === MEMBER_STORE ? REVIEWER_AVATAR : s.name === OTHER_STORE ? WRITER_AVATAR : undefined }))

function writerAvatarSource() {
  const view = render(<CrewAvatar seed="writer" avatar={WRITER_AVATAR} />)
  const src = view.container.querySelector('img')!.getAttribute('src')
  view.unmount()
  return src
}

function headerAvatar(member: string) {
  return screen.getByRole('heading', { name: `Memory for ${member}` }).parentElement!.parentElement!.querySelector('img')
}

function RouteLocation() {
  const location = useLocation()
  return <output data-testid="route-location">{location.pathname}{location.search}</output>
}

function NavigationVeto({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  return null
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', `/settings/overview?view=memory&store=${MEMBER_STORE}`)
  api.memoryStores.mockResolvedValue({ stores, active: 'default' })
  api.memberMemoryPage.mockImplementation(async (store: string, table: string, offset: number) => ({ entries: offset ? [] : store === 'default' ? table === 'semantic' ? [
    { key: 'global.selected', value_json: 'Selected source knowledge' },
    { key: 'global.unselected', value_json: 'Unselected source knowledge' },
  ] : [] : table === 'semantic' ? [FACT] : [EPISODE] }))
  api.memoryPreferences.mockImplementation(async (store?: string) => ({ content: `Preferences for ${store}` }))
  api.memoryProjects.mockImplementation(async (store?: string) => ({ content: `Projects for ${store}` }))
  api.memoryHistory.mockResolvedValue({ content: 'Global history' })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.lessons.mockResolvedValue({ lessons: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryRestoreRetired.mockResolvedValue({ ok: true })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryRestoreBackup.mockResolvedValue({ ok: true, pending: true })
  api.cancelMemberMemoryRestore.mockResolvedValue({ ok: true, cancelled: true, pending: false })
  api.memoryRecords.mockImplementation(async (store: string, query: { q: string; kind: string }, offset = 0) => {
    const read = (table: string) => query.q ? api.memberMemoryPage(store, table, offset, query.q) : api.memberMemoryPage(store, table, offset)
    const pages = await Promise.all([read('semantic'), read('episodic')])
    const entries = pages.flatMap(page => page.entries).map(row => ({ ...row, kind: row.key ? row.key.startsWith('lesson.') ? 'directive' : 'fact' : 'episode', id: row.key || row.id, revision: 'a'.repeat(64), text: row.text || String(row.value_json || '') })).filter(row => query.kind === 'all' || row.kind === query.kind)
    return { entries, total: entries.length, has_more: false }
  })
  api.memoryEditPreview.mockResolvedValue({ preview_id: 'reviewed-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 1, changed_count: 1, unchanged_count: 0, entries: [], preview_offset: 0, preview_limit: 25, preview_has_more: false, warnings: [] })
  api.memoryEditApply.mockResolvedValue({ ok: true, changed_count: 1 })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false })
  api.vectorSemanticWrite.mockResolvedValue({ ok: true })
  api.vectorSemanticDelete.mockResolvedValue({ ok: true })
  api.vectorEpisodicDelete.mockResolvedValue({ ok: true })
  api.saveMemoryPreferences.mockResolvedValue({ ok: true })
  api.saveMemoryProjects.mockResolvedValue({ ok: true })
  api.memorySeed.mockResolvedValue({ results: [{ id: 'global.selected', outcome: 'existing', reason: 'Existing target item preserved' }] })
  api.memoryRecall.mockResolvedValue({ semantic_context: '[memory:key:preference.review] Selected member evidence', episodic_context: '', lessons_context: '', retrieval: { facts: [{ id: 'key:preference.review', key: FACT.key, snippet: 'Selected member evidence', source: FACT.source, derived_from: JSON.parse(FACT.derived_from), retrieval: { matched_terms: ['review'] } }], episodes: [] } })
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({ slug: 'test-theme' })
  api.themeBoot.mockResolvedValue({})
})
afterEach(() => window.history.replaceState({}, '', '/'))

async function loaded() {
  await screen.findByText('Memory for reviewer')
  await screen.findByText(FACT.value_json)
}

async function openProfile() {
  fireEvent.mouseDown(screen.getByRole('tab', { name: 'Profile' }), { button: 0, ctrlKey: false })
  await screen.findByDisplayValue(`Preferences for ${MEMBER_STORE}`)
}

async function chooseWriter() {
  fireEvent.click(screen.getByRole('combobox', { name: 'Memory store' }))
  fireEvent.click(await screen.findByRole('option', { name: 'Private to writer · Memory V2' }))
}

describe('private member memory lifecycle', () => {
  it('labels a named legacy store as usable Memory V1 without presenting private V2 actions', async () => {
    window.history.replaceState({}, '', `/settings/overview?view=memory&store=${LEGACY_STORE}`)
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    await screen.findByRole('heading', { name: `Memory for ${LEGACY_STORE}` })
    expect(screen.getByText('Memory V1', { exact: true })).toBeVisible()
    const guidance = screen.getByText(/This member uses its current memory \(V1\)\./)
    expect(guidance).toHaveTextContent(/^This member uses its current memory \(V1\)\.$/)
    expect(screen.queryByText(/This member cannot return to its previous memory/)).toBeNull()
    expect(screen.queryByText('Private to this member · Memory V2')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy memories' })).toBeNull()
    expect(api.memberMemoryPage).toHaveBeenCalledWith(LEGACY_STORE, 'semantic', 0)
  })

  it('waits for the owning identity without showing a directory name or offering writes', async () => {
    let resolveIdentity!: (value: { stores: MemoryStoreSummary[]; active: string }) => void
    api.memoryStores.mockImplementation(() => new Promise(resolve => { resolveIdentity = resolve }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading member identity')
    expect(screen.queryByRole('heading', { name: `Memory for ${MEMBER_STORE}` })).toBeNull()
    expect(screen.queryByText(MEMBER_STORE, { exact: true })).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Memory store' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy memories' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(api.memberMemoryPage).not.toHaveBeenCalled()
    expect(api.memoryPreferences).not.toHaveBeenCalled()
    expect(api.memorySettings).not.toHaveBeenCalled()
    await act(async () => resolveIdentity({ stores: avatarStores(), active: 'default' }))
    await loaded()
    expect(headerAvatar('reviewer')).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=17')
    expect(screen.queryByText('Loading member identity…')).toBeNull()
  })

  it('shows a concrete catalog failure and retries identity before mounting scoped actions', async () => {
    api.memoryStores.mockRejectedValue(Object.assign(new Error('The member catalog cannot be read'), { status: 409 }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    expect(await screen.findByText('The member catalog cannot be read')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'This memory store’s member identity is unavailable.' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Copy memories' })).toBeNull()
    expect(api.memberMemoryPage).not.toHaveBeenCalled()
    expect(api.memorySettings).not.toHaveBeenCalled()
    api.memoryStores.mockResolvedValue({ stores: avatarStores(), active: 'default' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry member identity' }))
    await loaded()
    expect(screen.queryByText('The member catalog cannot be read')).toBeNull()
  })

  it.each(['absent', 'ownerless', 'unsupported-version'])('refuses a %s private identity instead of presenting the store id', async condition => {
    const catalog = condition === 'absent'
      ? stores.filter(s => s.name !== MEMBER_STORE)
      : stores.map(s => s.name === MEMBER_STORE
        ? { ...s, ...(condition === 'ownerless' ? { owner_member: '' } : { memory_version: 3, owner_member: 'Review & QA' }) }
        : s)
    api.memoryStores.mockResolvedValue({ stores: catalog, active: 'default' })
    renderWithProviders(<><MemoryTab refreshTrigger={0} /><RouteLocation /></>)
    await screen.findByRole('heading', { name: 'This memory store’s member identity is unavailable.' })
    expect(screen.queryByRole('heading', { name: `Memory for ${MEMBER_STORE}` })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy memories' })).toBeNull()
    expect(api.memberMemoryPage).not.toHaveBeenCalled()
    expect(api.memorySettings).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open crew manager' }))
    expect(screen.getByTestId('route-location').textContent).toBe(condition === 'unsupported-version'
      ? '/capabilities?tab=crews&crew=Review%20%26%20QA'
      : '/capabilities?tab=crews')
  })

  it('asks the existing navigation guard before leaving an unavailable identity', async () => {
    const guard = vi.fn().mockReturnValue(false)
    api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
    renderWithProviders(<NavigationLeaveGuardProvider>
      <NavigationVeto guard={guard} />
      <MemoryTab refreshTrigger={0} />
      <RouteLocation />
    </NavigationLeaveGuardProvider>)
    const openManager = await screen.findByRole('button', { name: 'Open crew manager' })
    fireEvent.click(openManager)
    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('route-location').textContent).toBe('/')
    guard.mockReturnValue(true)
    fireEvent.click(openManager)
    expect(guard).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('route-location').textContent).toBe('/capabilities?tab=crews')
    expect(api.memberMemoryPage).not.toHaveBeenCalled()
  })

  it('keeps the standalone invalid-identity fallback navigable without exposing records', () => {
    const summary = { ...stores[2], memory_version: 3, owner_member: 'Review & QA' }
    renderWithProviders(<><MemberMemoryPanel store={MEMBER_STORE} summary={summary} /><RouteLocation /></>)
    expect(screen.getByText(/configured memory store is unavailable/i)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Open crew manager' }))
    expect(screen.getByTestId('route-location').textContent).toBe('/capabilities?tab=crews&crew=Review%20%26%20QA')
    expect(api.memberMemoryPage).not.toHaveBeenCalled()
  })

  it('keeps the resolved owner and profile draft mounted through a failed catalog refresh', async () => {
    const { queryClient } = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    await openProfile()
    const preferences = screen.getByDisplayValue(`Preferences for ${MEMBER_STORE}`)
    fireEvent.change(preferences, { target: { value: 'Keep this unsaved member preference' } })
    api.memoryStores.mockRejectedValue(Object.assign(new Error('Member catalog is temporarily unavailable'), { status: 409 }))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['memory-stores'] }) })
    expect(await screen.findByText('Member catalog is temporarily unavailable')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Memory for reviewer' })).toBeVisible()
    expect(screen.getByDisplayValue('Keep this unsaved member preference')).toBe(preferences)
    api.memoryStores.mockResolvedValue({ stores: avatarStores(), active: 'default' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry member identity' }))
    await waitFor(() => expect(screen.queryByText('Member catalog is temporarily unavailable')).toBeNull())
    expect(screen.getByDisplayValue('Keep this unsaved member preference')).toBe(preferences)
    expect(api.saveMemoryPreferences).not.toHaveBeenCalled()
  })

  it('keeps the same header copy action when the member becomes empty', async () => {
    const { queryClient } = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    const copy = screen.getByRole('button', { name: 'Copy memories', exact: true })
    api.memberMemoryPage.mockResolvedValue({ entries: [] })
    api.memoryStores.mockResolvedValue({ stores: stores.map(s => s.name === MEMBER_STORE ? { ...s, semantic_count: 0, episodic_count: 0 } : s), active: 'default' })
    await act(async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['memory-stores'] }),
        queryClient.invalidateQueries({ queryKey: ['memory-records', MEMBER_STORE] }),
      ])
    })
    await screen.findByText('A fresh start for this member')
    expect(screen.getByRole('button', { name: 'Copy memories', exact: true })).toBe(copy)
    expect(copy).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Open member conversation' })).toBeVisible()
    expect(api.memorySeed).not.toHaveBeenCalled()
  })

  it('opens an empty member’s exact conversation without copying global memory', async () => {
    api.memberMemoryPage.mockResolvedValue({ entries: [] })
    api.memoryStores.mockResolvedValue({ stores: stores.map(s => s.name === MEMBER_STORE ? { ...s, owner_member: 'Review & QA', semantic_count: 0, episodic_count: 0 } : s), active: 'default' })
    renderWithProviders(<><MemoryTab refreshTrigger={0} /><RouteLocation /></>)
    await screen.findByText('A fresh start for this member')
    fireEvent.click(screen.getByRole('button', { name: 'Open member conversation' }))
    expect(screen.getByTestId('route-location')).toHaveTextContent('/members?member=Review%20%26%20QA')
    expect(api.memorySeed).not.toHaveBeenCalled()
    expect(api.memberMemoryPage.mock.calls.every(call => call[0] === MEMBER_STORE)).toBe(true)
  })

  it('explains and retries a listing-marked unavailable store, with a direct recovery action', async () => {
    api.memoryStores.mockResolvedValue({ stores: stores.map(s => s.name === MEMBER_STORE ? { ...s, exists: false, semantic_count: null, episodic_count: null } : s), active: 'default' })
    const healthy = api.memberMemoryPage.getMockImplementation()!
    api.memberMemoryPage.mockRejectedValue(Object.assign(new Error('Private database is missing; restore this member backup'), { status: 409 }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('Private database is missing; restore this member backup')
    expect(screen.queryByText('A fresh start for this member')).toBeNull()
    expect(screen.queryByText(FACT.value_json)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Recovery', exact: true }))
    await waitFor(() => expect(api.memoryBackups).toHaveBeenCalledWith(MEMBER_STORE))
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Memories' }), { button: 0, ctrlKey: false })
    api.memberMemoryPage.mockImplementation(healthy)
    api.memoryStores.mockResolvedValue({ stores, active: 'default' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry memory access' }))
    expect(await screen.findByText(FACT.value_json)).toBeInTheDocument()
    expect(api.memorySettings).not.toHaveBeenCalled()
  })

  it('corrects an experience under its stable id and retains the draft through a failed save', async () => {
    api.memoryEditPreview.mockRejectedValueOnce(new Error('Experience changed while editing'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[1])
    const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
    const text = 'The scheduled restart correctly preserved this member’s identity'
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: text } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await within(dialog).findByText('Experience changed while editing')
    expect(within(dialog).getByRole('textbox', { name: 'Memory text' })).toHaveValue(text)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Apply changes (1)' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(api.memoryEditPreview).toHaveBeenLastCalledWith(MEMBER_STORE, { items: [{ kind: 'episode', id: EPISODE.id, revision: 'a'.repeat(64) }] }, { type: 'set', text })
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
    expect(api.vectorEpisodicDelete).not.toHaveBeenCalled()
  })

  it('reaches older retired memories and refreshes live memories after their restoration', async () => {
    let restored = false
    const old = { id: 'older-retired', text: 'Recovered earlier member experience', ts: '2026-09-01T00:00:00Z' }
    api.memoryRetired.mockImplementation(async (_store: string, _limit: number, offset = 0) => ({ retired: offset ? restored ? [] : [old] : Array.from({ length: 50 }, (_, index) => ({ id: `retired-${index}`, text: `Retired experience ${index}` })) }))
    api.memoryRestoreRetired.mockImplementation(async () => { restored = true; return { ok: true } })
    const healthy = api.memberMemoryPage.getMockImplementation()!
    api.memberMemoryPage.mockImplementation(async (store: string, kind: string, offset: number) => {
      const result = await healthy(store, kind, offset)
      return kind === 'episodic' && restored ? { entries: [...result.entries, old] } : result
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Recovery' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('button', { name: 'Show more' }))
    const row = (await screen.findByText(old.text)).closest('tr')!
    expect(api.memoryRetired).toHaveBeenCalledWith(MEMBER_STORE, 50, 50)
    fireEvent.click(within(row).getByRole('button', { name: 'Restore experience' }))
    await waitFor(() => expect(api.memoryRestoreRetired).toHaveBeenCalledWith(old.id, MEMBER_STORE))
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Memories' }), { button: 0, ctrlKey: false })
    expect(await screen.findByText(old.text)).toBeInTheDocument()
  })

  it('keeps persisted pending restore visible after remount and blocks duplicate staging', async () => {
    api.memoryBackups.mockResolvedValue({ backups: [{ name: 'member-backup.zip', size_bytes: 1000, taken_at: '2026-09-07T12:00:00Z' }], pending: true, restart_required: true, pending_restore: { backup_name: 'member-backup.zip', staged_at: '2026-09-07T13:00:00Z' } })
    const view = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    for (let visit = 0; visit < 2; visit++) {
      fireEvent.mouseDown(screen.getByRole('tab', { name: 'Recovery' }), { button: 0, ctrlKey: false })
      expect(await screen.findByText('Restart the Kiro Crew gateway to restore this backup. Current memory stays active until then.')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Open restart controls' })).toHaveAttribute('href', '/settings/about')
      expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toBeDisabled()
      if (visit === 0) { view.rerender(<></>); view.rerender(<MemoryTab refreshTrigger={0} />); await loaded() }
    }
    expect(api.memoryRestoreBackup).not.toHaveBeenCalled()
  })

  it('cancels a staged member restore with explicit success and keeps the original backup available', async () => {
    const backups = [{ name: 'member-backup.zip', size_bytes: 1000, taken_at: '2026-09-07T12:00:00Z' }]
    api.memoryBackups.mockResolvedValue({ backups, pending: true, restart_required: true })
    api.cancelMemberMemoryRestore.mockImplementation(async () => {
      api.memoryBackups.mockResolvedValue({ backups, pending: false, restart_required: false })
      return { ok: true, cancelled: true, pending: false }
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Recovery' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel staged restore' }))
    const status = await screen.findByText('Staged restore cancelled. Current memory and backup are unchanged.')
    expect(status).not.toHaveTextContent(/restart/i)
    expect(api.cancelMemberMemoryRestore).toHaveBeenCalledWith(MEMBER_STORE)
    expect(screen.getByRole('button', { name: 'Restore backup', exact: true })).toBeEnabled()
    expect(screen.queryByRole('link', { name: 'Open restart controls' })).not.toBeInTheDocument()
    expect(api.memoryRestoreBackup).not.toHaveBeenCalled()
  })

  it('keeps text typed during a profile save visibly unsaved', async () => {
    let finishSave!: (value: unknown) => void
    api.saveMemoryPreferences.mockImplementation(() => new Promise(resolve => { finishSave = resolve }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    await openProfile()
    const preferences = screen.getByRole('textbox', { name: 'Preferences' })
    const card = preferences.parentElement!
    fireEvent.change(preferences, { target: { value: 'Saved snapshot' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.saveMemoryPreferences).toHaveBeenCalledWith('Saved snapshot', MEMBER_STORE))
    fireEvent.change(preferences, { target: { value: 'Newer unsaved draft' } })
    finishSave({ ok: true })
    await waitFor(() => expect(within(card).getByRole('button', { name: 'Save' })).toBeEnabled())
    expect(preferences).toHaveValue('Newer unsaved draft')
    expect(within(card).queryByRole('button', { name: 'Saved' })).toBeNull()
  })

  it('retries a failed profile read without discarding the other profile draft', async () => {
    api.memoryPreferences.mockRejectedValue(Object.assign(new Error('Preferences file is unreadable'), { status: 409 }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Profile' }), { button: 0, ctrlKey: false })
    await screen.findByText('Preferences file is unreadable')
    const projects = await screen.findByRole('textbox', { name: 'Projects' })
    await waitFor(() => expect(projects).toBeEnabled())
    fireEvent.change(projects, { target: { value: 'Preserve this project draft' } })
    api.memoryPreferences.mockResolvedValue({ content: 'Repaired preferences' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry memory access' }))
    expect(await screen.findByDisplayValue('Repaired preferences')).toBeEnabled()
    expect(projects).toHaveValue('Preserve this project draft')
    expect(api.saveMemoryProjects).not.toHaveBeenCalled()
  })

  it('uses each owner’s exact roster avatar in the header and switcher, including uploaded and pinned faces', async () => {
    const writerSrc = writerAvatarSource()
    api.memoryStores.mockResolvedValue({ stores: avatarStores(), active: 'default' })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    expect(headerAvatar('reviewer')).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=17')
    const picker = screen.getByRole('combobox', { name: 'Memory store' })
    expect(picker.querySelector('img')).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=17')
    fireEvent.click(picker)
    const writer = await screen.findByRole('option', { name: 'Private to writer · Memory V2' })
    expect(writer.querySelector('img')).toHaveAttribute('src', writerSrc)
    expect(screen.getByRole('option', { name: 'Shared · Global Memory V1' }).querySelector('img')).toBeNull()
    fireEvent.click(writer)
    await screen.findByRole('heading', { name: 'Memory for writer' })
    expect(headerAvatar('writer')).toHaveAttribute('src', writerSrc)
    expect(screen.getByRole('combobox', { name: 'Memory store' }).querySelector('img')).toHaveAttribute('src', writerSrc)
  })

  it('shows the source member avatar while copying without replacing the target identity', async () => {
    const writerSrc = writerAvatarSource()
    api.memoryStores.mockResolvedValue({ stores: avatarStores(), active: 'default' })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    const targetAvatar = headerAvatar('reviewer')
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    expect(dialog).toHaveAccessibleName('Copy memories to “reviewer”')
    const source = within(dialog).getByRole('combobox', { name: 'Source memory' })
    fireEvent.click(source)
    const writer = await screen.findByRole('option', { name: 'writer' })
    expect(writer.querySelector('img')).toHaveAttribute('src', writerSrc)
    fireEvent.click(writer)
    expect(source.querySelector('img')).toHaveAttribute('src', writerSrc)
    await waitFor(() => expect(api.memberMemoryPage).toHaveBeenCalledWith(OTHER_STORE, 'semantic', 0))
    expect(targetAvatar).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=17')
  })

  it('refreshes a changed owner avatar when returning to a cached memory page', async () => {
    api.memoryStores.mockResolvedValue({ stores: avatarStores(), active: 'default' })
    const view = renderWithProviders(<MemoryTab refreshTrigger={0} />, { queryDefaults: { staleTime: Infinity } })
    await loaded()
    expect(headerAvatar('reviewer')).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=17')
    view.rerender(<></>)
    api.memoryStores.mockResolvedValue({ stores: avatarStores().map(s => s.name === MEMBER_STORE ? { ...s, owner_avatar: { kind: 'image', v: 18 } } : s), active: 'default' })
    view.rerender(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(headerAvatar('reviewer')).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=18'))
    expect(screen.getByRole('combobox', { name: 'Memory store' }).querySelector('img')).toHaveAttribute('src', '/api/agents/reviewer/avatar?v=18')
  })

  it('opens the deep-linked member with scoped reads and no global memory surface', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    expect(api.memberMemoryPage.mock.calls).toEqual([[MEMBER_STORE, 'semantic', 0], [MEMBER_STORE, 'episodic', 0]])
    expect(api.memoryPreferences).not.toHaveBeenCalled()
    expect(api.memoryBackups).not.toHaveBeenCalled()
    await openProfile()
    expect(api.memoryPreferences).toHaveBeenCalledWith(MEMBER_STORE)
    expect(api.memoryProjects).toHaveBeenCalledWith(MEMBER_STORE)
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Recovery' }), { button: 0, ctrlKey: false })
    await waitFor(() => expect(api.memoryBackups).toHaveBeenCalledWith(MEMBER_STORE))
    expect(api.memoryBackups).toHaveBeenCalledWith(MEMBER_STORE)
    expect(api.memoryRetired).toHaveBeenCalledWith(MEMBER_STORE, expect.any(Number))
    const advanced = screen.getByText('Advanced memory analysis').closest('details')!
    advanced.open = true
    fireEvent(advanced, new Event('toggle'))
    await waitFor(() => expect(api.memoryCarve).toHaveBeenCalled())
    expect(api.memoryCarve).toHaveBeenCalledWith(expect.objectContaining({ store: MEMBER_STORE }))
    expect(api.memorySettings).not.toHaveBeenCalled()
    expect(api.lessons).not.toHaveBeenCalled()
    expect(api.memoryHistory).not.toHaveBeenCalled()
    expect(screen.queryByTestId('global-vector-card')).toBeNull()
    expect(screen.queryByTestId('global-embedding-card')).toBeNull()
    expect(screen.queryByRole('button', { name: /New store/ })).toBeNull()
  })

  it('corrects and forgets the selected member items after explicit confirmation', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0])
    let dialog = screen.getByRole('dialog', { name: 'Edit memory' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: 'Review every changed boundary' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith(MEMBER_STORE, { items: [{ kind: 'fact', id: FACT.key, revision: 'a'.repeat(64) }] }, { type: 'set', value: 'Review every changed boundary' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Apply changes (1)' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(screen.getAllByRole('button', { name: 'View details' })[0])
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Memory details' })).getByRole('button', { name: 'Forget' }))
    dialog = screen.getByRole('dialog', { name: 'Forget' })
    expect(api.vectorSemanticDelete).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith(MEMBER_STORE, { items: [{ kind: 'fact', id: FACT.key, revision: 'a'.repeat(64) }] }, { type: 'forget' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Forget selected memories: 1' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(screen.getAllByRole('button', { name: 'View details' })[1])
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Memory details' })).getByRole('button', { name: 'Forget' }))
    dialog = screen.getByRole('dialog', { name: 'Forget' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith(MEMBER_STORE, { items: [{ kind: 'episode', id: EPISODE.id, revision: 'a'.repeat(64) }] }, { type: 'forget' }))
  })

  it('copies only checked source items and keeps copy outcomes inside the dialog', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    expect(api.memberMemoryPage.mock.calls.every(([store]) => store === MEMBER_STORE)).toBe(true)
    expect(screen.queryByText('Adds only selected memories to this member; the source stays unchanged.')).toBeNull()
    expect(api.memorySeed).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    await waitFor(() => expect(within(dialog).getByText('Copy selected memories here without changing existing or source memories. Each copy records its origin.')).toBeVisible())
    expect(within(dialog).getByText('Search memory', { exact: true })).toBeVisible()
    expect(within(dialog).getByRole('textbox', { name: 'Search memory' })).toBeVisible()
    fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Selected source knowledge' }))
    expect(within(dialog).getByRole('checkbox', { name: 'Unselected source knowledge' })).not.toBeChecked()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy selected (1)' }))
    await waitFor(() => expect(api.memorySeed).toHaveBeenCalledWith('default', MEMBER_STORE, [{ kind: 'fact', id: 'global.selected' }]))
    expect(await within(dialog).findByText('Copied: 0. Skipped: 1.')).toBeInTheDocument()
    await waitFor(() => expect(within(dialog).getByText('Adds only selected memories to this member; the source stays unchanged.', { exact: true })).toBeVisible())
    expect(within(dialog).getAllByRole('status')).toHaveLength(1)
    expect(within(dialog).getByText('Existing target item preserved')).toBeInTheDocument()
    expect(within(dialog).queryByRole('alert')).toBeNull()
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
    expect(api.vectorSemanticDelete).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByText('Adds only selected memories to this member; the source stays unchanged.', { exact: true })).toBeNull()
    fireEvent.click(screen.getAllByRole('button', { name: 'View details' })[0])
    expect(screen.getByText('Copied from Shared · Global Memory V1')).toBeInTheDocument()
    expect(screen.queryByText(FACT.derived_from)).toBeNull()
  })

  it('lets the owner choose a source item beyond two hundred memories', async () => {
    const original = api.memberMemoryPage.getMockImplementation()!
    api.memberMemoryPage.mockImplementation((store: string, table: string, offset: number) => store === 'default' && table === 'semantic'
      ? Promise.resolve({ entries: Array.from({ length: Math.min(100, 201 - offset) }, (_, index) => ({ key: `source.${offset + index}`, value_json: `Source item ${offset + index}` })) })
      : original(store, table, offset))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    await within(dialog).findByRole('checkbox', { name: 'Source item 99' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Show more' }))
    await within(dialog).findByRole('checkbox', { name: 'Source item 199' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Show more' }))
    fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Source item 200' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy selected (1)' }))
    await waitFor(() => expect(api.memorySeed).toHaveBeenCalledWith('default', MEMBER_STORE, [{ kind: 'fact', id: 'source.200' }]))
    expect(api.memberMemoryPage).toHaveBeenCalledWith('default', 'semantic', 200)
  })

  it('corrects a directive rule while retaining its structured metadata', async () => {
    const original = { rule: 'Old review rule', category: 'tool', ts: '2026-09-01', source_id: 'owner-note' }
    api.memberMemoryPage.mockImplementation(async (_store: string, table: string) => ({ entries: table === 'semantic' ? [{ key: 'lesson.review', value_json: JSON.stringify(original) }] : [] }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('Old review rule')
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: 'New review rule' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith(MEMBER_STORE, expect.any(Object), { type: 'set', value: { ...original, rule: 'New review rule' } }))
  })

  it('validates a structured fact correction before sending a parsed object', async () => {
    api.memberMemoryPage.mockImplementation(async (_store: string, table: string) => ({ entries: table === 'semantic' ? [{ key: 'project.settings', value_json: '{"language":"en"}' }] : [] }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByRole('button', { name: 'Edit' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
    const editor = within(dialog).getByRole('textbox', { name: 'Memory text' })
    fireEvent.change(editor, { target: { value: '{broken JSON' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await within(dialog).findByRole('alert')
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
    fireEvent.change(editor, { target: { value: '{"language":"zh"}' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith(MEMBER_STORE, expect.any(Object), { type: 'set', value: { language: 'zh' } }))
  })

  it('keeps a dirty member document until discard is confirmed, then reads the next member', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    await openProfile()
    const preferences = screen.getByDisplayValue(`Preferences for ${MEMBER_STORE}`)
    fireEvent.change(preferences, { target: { value: 'Unsaved member draft' } })
    await chooseWriter()
    let dialog = screen.getByRole('dialog', { name: 'Discard changes' })
    await waitFor(() => expect(within(dialog).getByText('Switching memory will discard the open draft and selection.', { exact: true })).toBeVisible())
    expect(preferences).toHaveValue('Unsaved member draft')
    expect(api.memoryPreferences).not.toHaveBeenCalledWith(OTHER_STORE)
    expect(api.saveMemoryPreferences).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Keep editing' }))
    expect(screen.queryByRole('dialog', { name: 'Discard changes' })).toBeNull()
    expect(preferences).toHaveValue('Unsaved member draft')
    expect(api.memoryPreferences).not.toHaveBeenCalledWith(OTHER_STORE)
    await chooseWriter()
    dialog = screen.getByRole('dialog', { name: 'Discard changes' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByText('Memory for writer')).toBeInTheDocument()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Profile' }), { button: 0, ctrlKey: false })
    expect(await screen.findByDisplayValue(`Preferences for ${OTHER_STORE}`)).toBeInTheDocument()
    expect(screen.queryByDisplayValue('Unsaved member draft')).toBeNull()
    expect(api.memorySettings).not.toHaveBeenCalled()
  })

  it('describes leaving the memory panel without calling it a store switch', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      renderWithProviders(
        <SidePanelLayout
          title="Settings"
          tabs={[
            { key: 'memory', label: 'Memory surface', icon: <span /> },
            { key: 'other', label: 'Other surface', icon: <span /> },
          ]}
          defaultTab="memory"
        >
          {tab => tab === 'memory' ? <MemoryTab refreshTrigger={0} /> : <div>Other settings</div>}
        </SidePanelLayout>,
      )
      await loaded()
      await openProfile()
      const preferences = screen.getByDisplayValue(`Preferences for ${MEMBER_STORE}`)
      fireEvent.change(preferences, { target: { value: 'Unsaved member draft' } })

      fireEvent.click(screen.getByRole('button', { name: 'Other surface' }))
      expect(confirmSpy).toHaveBeenCalledWith('Leaving will discard unsaved drafts and the current selection.')
      expect(preferences).toHaveValue('Unsaved member draft')
      expect(screen.queryByText('Other settings')).toBeNull()

      confirmSpy.mockReturnValue(true)
      fireEvent.click(screen.getByRole('button', { name: 'Other surface' }))
      expect(await screen.findByText('Other settings')).toBeVisible()
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('saves member preferences to the same store and clears its switch guard', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    await openProfile()
    const preferences = screen.getByDisplayValue(`Preferences for ${MEMBER_STORE}`)
    fireEvent.change(preferences, { target: { value: 'Keep reviews concise' } })
    const card = preferences.parentElement!
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.saveMemoryPreferences).toHaveBeenCalledWith('Keep reviews concise', MEMBER_STORE))
    await within(card).findByRole('button', { name: 'Saved' })
    await chooseWriter()
    expect(await screen.findByText('Memory for writer')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Discard changes' })).toBeNull()
  })

  it('explains a failed private read without showing global rows', async () => {
    api.memberMemoryPage.mockRejectedValue(Object.assign(new Error('Private database is missing; restore this member backup'), { status: 409 }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    expect(await screen.findByText('Private database is missing; restore this member backup')).toBeInTheDocument()
    expect(api.memberMemoryPage.mock.calls.every(([store]) => store === MEMBER_STORE)).toBe(true)
    expect(api.memoryHistory).not.toHaveBeenCalled()
    expect(api.memorySettings).not.toHaveBeenCalled()
    expect(screen.queryByTestId('global-vector-card')).toBeNull()
  })

  it('shows recall evidence from only the selected member', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'review' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recall for this task' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Recall for this task' }))
    expect(await screen.findByText('Selected member evidence')).toBeVisible()
    expect(api.memoryRecall).toHaveBeenCalledWith('review', MEMBER_STORE)
    expect(screen.getByText(/Matching terms: review/)).not.toBeVisible()
    fireEvent.click(screen.getByText('Source and retrieval details'))
    expect(screen.getByText(/Matching terms: review/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(screen.queryByText('Selected member evidence')).toBeNull()
  })

  it('keeps an unconfirmed partial copy distinct from a skipped or successful copy', async () => {
    api.memorySeed.mockResolvedValue({ partial: true, results: [{ id: 'global.selected', outcome: 'unconfirmed', reason: 'Could not confirm the database write' }] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Selected source knowledge' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy selected (1)' }))
    const failure = await within(dialog).findByRole('alert')
    expect(failure).toHaveTextContent('Could not confirm the database write')
    expect(within(dialog).queryByRole('status')).toBeNull()
    expect(within(dialog).getByRole('checkbox', { name: 'Selected source knowledge' })).toBeChecked()
  })

  it('surfaces a reasonless rejected copy as a failure', async () => {
    api.memorySeed.mockResolvedValue({ partial: false, results: [{ id: 'global.selected', outcome: 'rejected' }] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Selected source knowledge' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy selected (1)' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Copy failed')
  })

  it('renders a not-attempted copy reason through the shared error notice', async () => {
    api.memorySeed.mockResolvedValue({ partial: true, results: [{ id: 'global.selected', outcome: 'not_attempted', reason: 'Copy stopped before this item was attempted' }] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    fireEvent.click(await within(dialog).findByRole('checkbox', { name: 'Selected source knowledge' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy selected (1)' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Copy stopped before this item was attempted')
    expect(within(dialog).queryByText('Copy stopped before this item was attempted', { selector: 'p' })).toBeNull()
  })

  it('searches the store on the server so a match beyond the loaded page is reachable', async () => {
    const original = api.memberMemoryPage.getMockImplementation()!
    api.memberMemoryPage.mockImplementation((store: string, table: string, offset: number, query?: string) => query === 'needle'
      ? Promise.resolve({ entries: table === 'semantic' ? [{ key: 'older.fact', value_json: 'Older needle knowledge' }] : [] })
      : original(store, table, offset))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'needle' } })
    expect(await screen.findByText('Older needle knowledge')).toBeInTheDocument()
    expect(api.memberMemoryPage).toHaveBeenCalledWith(MEMBER_STORE, 'semantic', 0, 'needle')
    expect(api.memberMemoryPage).toHaveBeenCalledWith(MEMBER_STORE, 'episodic', 0, 'needle')
    expect(screen.queryByText('A fresh start for this member')).toBeNull()
  })

  it.each(['member', 'copy'] as const)('preserves server-normalized text matches in the %s search', async surface => {
    const original = api.memberMemoryPage.getMockImplementation()!
    api.memberMemoryPage.mockImplementation((store: string, table: string, offset: number, query?: string) => query === 'strasse'
      ? Promise.resolve({ entries: table === 'semantic' ? [{ key: 'address', value_json: 'Straße 12' }] : [] })
      : original(store, table, offset))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    if (surface === 'copy') {
      fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
      await screen.findByRole('checkbox', { name: 'Selected source knowledge' })
    }
    const view = surface === 'copy' ? within(screen.getByRole('dialog', { name: /^Copy memories to/ })) : screen
    fireEvent.change(view.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'strasse' } })
    if (surface === 'copy') expect(view.getByText('Search memory', { exact: true })).toBeVisible()
    expect(view.getByRole('status')).toBeInTheDocument()
    expect(view.queryByText(surface === 'copy' ? 'Selected source knowledge' : FACT.value_json)).toBeNull()
    expect(await view.findByText('Straße 12')).toBeInTheDocument()
    expect(api.memberMemoryPage).toHaveBeenCalledWith(surface === 'copy' ? 'default' : MEMBER_STORE, 'semantic', 0, 'strasse')
  })

  it('preserves a profile draft when navigating between the member sections', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    await openProfile()
    fireEvent.change(screen.getByRole('textbox', { name: 'Preferences' }), { target: { value: 'Keep this draft through tab changes' } })
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Memories' }), { button: 0, ctrlKey: false })
    expect(screen.queryByRole('textbox', { name: 'Preferences' })).toBeNull()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Profile' }), { button: 0, ctrlKey: false })
    expect(screen.getByRole('textbox', { name: 'Preferences' })).toHaveValue('Keep this draft through tab changes')
    await chooseWriter()
    expect(screen.getByRole('dialog', { name: 'Discard changes' })).toBeInTheDocument()
    expect(api.saveMemoryPreferences).not.toHaveBeenCalled()
  })

  it('warns before browser navigation only while a member profile draft is unsaved', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    await openProfile()
    const clean = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(clean)
    expect(clean.defaultPrevented).toBe(false)
    const preferences = screen.getByRole('textbox', { name: 'Preferences' })
    fireEvent.change(preferences, { target: { value: 'An unsaved preference' } })
    const dirty = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirty)
    expect(dirty.defaultPrevented).toBe(true)
    fireEvent.click(within(preferences.parentElement!).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.saveMemoryPreferences).toHaveBeenCalled())
    await within(preferences.parentElement!).findByRole('button', { name: 'Saved' })
    await waitFor(() => {
      const saved = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(saved)
      expect(saved.defaultPrevented).toBe(false)
    })
  })

  it('filters experiences without mixing in facts and returns to all memories', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    expect(screen.getByText('Facts save details. Lessons guide the member’s work. Experiences are events the member can recall.', { exact: true })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /^Experiences/ }))
    expect(await screen.findByText(EPISODE.text)).toBeInTheDocument()
    expect(screen.queryByText(FACT.value_json)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(await screen.findByText(FACT.value_json)).toBeInTheDocument()
  })

  it('caps copy selection at fifty while keeping selected items deselectable', async () => {
    const original = api.memberMemoryPage.getMockImplementation()!
    api.memberMemoryPage.mockImplementation((store: string, table: string, offset: number) => store === 'default' && table === 'semantic'
      ? Promise.resolve({ entries: Array.from({ length: 51 }, (_, index) => ({ key: `source.${index}`, value_json: `Source item ${index}` })) })
      : original(store, table, offset))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Copy memories' }))
    const dialog = screen.getByRole('dialog', { name: /^Copy memories to/ })
    const choices = await within(dialog).findAllByRole('checkbox')
    choices.slice(0, 50).forEach(choice => fireEvent.click(choice))
    expect(choices[50]).toBeDisabled()
    expect(choices[0]).not.toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Copy selected (50)' })).toBeEnabled()
    fireEvent.click(choices[0])
    expect(choices[50]).toBeEnabled()
    expect(within(dialog).getByRole('button', { name: 'Copy selected (49)' })).toBeEnabled()
  })

  it('stages a confirmed recovery for this member and explains the restart requirement', async () => {
    const name = 'member-backup-20260907.zip'
    api.memoryBackups.mockResolvedValue({ backups: [{ name, size_bytes: 40960, taken_at: '2026-09-07T12:00:00Z' }] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Recovery' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('button', { name: 'Restore backup', exact: true }))
    expect(api.memoryRestoreBackup).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm restore' }))
    await waitFor(() => expect(api.memoryRestoreBackup).toHaveBeenCalledWith(name, MEMBER_STORE))
    expect(await screen.findByRole('status')).toHaveTextContent(/restart/i)
    expect(api.memorySettings).not.toHaveBeenCalled()
  })

  it('opens the full experience and source from a three-line card preview', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await loaded()
    expect(screen.getByText(EPISODE.text)).toHaveClass('line-clamp-3')
    fireEvent.click(screen.getAllByRole('button', { name: 'View details' })[1])
    const detail = screen.getByRole('dialog', { name: 'Memory details' })
    expect(within(detail).getByText(EPISODE.text)).not.toHaveClass('line-clamp-3')
    await waitFor(() => expect(within(detail).getByText(EPISODE.source, { exact: true })).toBeVisible())
    expect(within(detail).getByRole('button', { name: 'Forget' })).toBeEnabled()
    expect(api.vectorEpisodicDelete).not.toHaveBeenCalled()
  })
})
