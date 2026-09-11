import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import MemoryRecordsEditor from '../pages/overview/MemoryRecordsEditor'
import SidePanelLayout from '../components/SidePanelLayout'
import type { MemoryRecord } from '../types/memoryEditing'

const { api, viewport } = vi.hoisted(() => ({ viewport: { mobile: false }, api: { vectorSemanticWrite: vi.fn(), memoryRecordHistory: vi.fn(), memoryRecords: vi.fn(), memoryRecordsRefresh: vi.fn(), memoryQuerySelectionRefresh: vi.fn(), memoryEditPreview: vi.fn(), memoryEditPreviewPage: vi.fn(), memoryEditApply: vi.fn(), memoryRecall: vi.fn(), memoryStores: vi.fn(), themes: vi.fn(), themeDetail: vi.fn(), themeBoot: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile }))
const record = (index: number, extra: Partial<MemoryRecord> = {}): MemoryRecord => ({ kind: 'fact', id: `key:user.contact_${index}`, key: `user.contact_${index}`, value_json: JSON.stringify(`Contact ${index}: old@example.com`), text: `Contact ${index}: old@example.com`, revision: String(index).padStart(64, '0'), source: 'user_explicit', metadata: { email_addresses: ['old@example.com'] }, ...extra })
let records: MemoryRecord[]
beforeEach(() => {
  vi.clearAllMocks()
  for (const mock of Object.values(api)) mock.mockReset()
  viewport.mobile = false
  records = Array.from({ length: 65 }, (_, index) => record(index))
  api.memoryRecords.mockImplementation((_store, query, offset = 0, limit = 50) => {
    const matches = records.filter(row => (query.kind === 'all' || row.kind === query.kind) && (!query.q || row.text.includes(query.q)))
    return Promise.resolve({ entries: matches.slice(offset, offset + limit), total: matches.length, has_more: offset + limit < matches.length })
  })
  api.memoryEditPreview.mockResolvedValue({ preview_id: 'signed-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 65, changed_count: 65, unchanged_count: 0, entries: Array.from({ length: 25 }, (_, index) => ({ before: record(index), after: record(index, { value_json: JSON.stringify(`Contact ${index}: new@example.com`) }) })), preview_offset: 0, preview_limit: 25, preview_has_more: true, warnings: [] })
  api.memoryEditApply.mockResolvedValue({ ok: true, changed_count: 65 })
  api.vectorSemanticWrite.mockResolvedValue({ ok: true })
  api.memoryRecordsRefresh.mockImplementation((_store, items) => Promise.resolve({ entries: items.map((item: { id: string }) => records.find(row => row.id === item.id)).filter(Boolean), missing: [] }))
  api.memoryQuerySelectionRefresh.mockResolvedValue({ matched_count: 65 })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false })
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.themes.mockResolvedValue({ themes: [] }); api.themeDetail.mockResolvedValue({ slug: 'test-theme' }); api.themeBoot.mockResolvedValue({})
})
const mount = (store = 'default') => renderWithProviders(<MemoryRecordsEditor store={store} />)
const checkbox = (index: number) => screen.getByRole('checkbox', { name: `Select memory: Contact ${index}: old@example.com`, exact: true })
async function replaceForm() {
  fireEvent.click(screen.getByRole('button', { name: 'Find and replace', exact: true }))
  const dialog = screen.getByRole('dialog', { name: 'Edit selected memories' })
  fireEvent.change(within(dialog).getByLabelText('Find text'), { target: { value: 'old@example.com' } })
  fireEvent.change(within(dialog).getByLabelText('Replace with'), { target: { value: 'new@example.com' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes', exact: true }))
  await within(dialog).findByText('Will change: 65')
  await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Adjust edit', exact: true })).toBeVisible())
  return dialog
}

describe('memory record editing in both lineages', () => {
  it('explains selection actions before selection and restores the hint after clearing', async () => {
    mount()
    await screen.findByText('Contact 0: old@example.com')
    const hint = 'Select memories to replace text or forget. Preview before saving.'
    expect(screen.getByText(hint)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Find and replace', exact: true })).toBeNull()
    fireEvent.click(checkbox(0))
    expect(screen.queryByText(hint)).toBeNull()
    const replace = screen.getByRole('button', { name: 'Find and replace', exact: true })
    const forget = screen.getByRole('button', { name: 'Forget selected memories: 1', exact: true })
    const clear = screen.getByRole('button', { name: 'Clear selection', exact: true })
    expect(replace).toBeEnabled()
    expect(forget).toBeEnabled()
    const actions = replace.parentElement!
    expect(actions).toBe(forget.parentElement)
    expect(within(actions).getAllByRole('button')).toEqual([replace, forget])
    expect(actions).not.toContainElement(clear)
    expect(within(clear.parentElement!).getByRole('checkbox', { name: 'Select this page' })).toBeVisible()
    expect(within(clear.parentElement!).getAllByRole('button')).toEqual([clear])
    fireEvent.click(clear)
    expect(screen.getByText(hint)).toBeVisible()
    expect(api.memoryEditPreview).not.toHaveBeenCalled()
    expect(api.memoryEditApply).not.toHaveBeenCalled()
  })

  it.each([
    { store: 'default', privateMemory: false },
    { store: 'member-review', privateMemory: true },
  ])('keeps the $store memory key accessible without changing its record identity', async ({ store, privateMemory }) => {
    records = [record(0)]
    renderWithProviders(<MemoryRecordsEditor store={store} privateMemory={privateMemory} />)
    expect(await screen.findByText(records[0].text)).toBeVisible()
    if (privateMemory) {
      expect(screen.queryByText(records[0].key!)).toBeNull()
      expect(screen.queryByTitle(records[0].key!)).toBeNull()
    } else {
      expect(screen.getByText(records[0].key!)).toBeVisible()
      expect(screen.getByTitle(records[0].key!)).toBeVisible()
    }
    fireEvent.click(checkbox(0))
    expect(checkbox(0)).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'View details' }))
    const detail = screen.getByRole('dialog', { name: 'Memory details' })
    await waitFor(() => expect(within(detail).getByText(records[0].text)).toBeVisible())
    if (privateMemory) {
      expect(within(detail).getByText('Memory key')).toBeVisible()
      expect(within(detail).getByText(records[0].key!)).toBeVisible()
    } else {
      expect(within(detail).queryByText('Memory key')).toBeNull()
    }
    await waitFor(() => expect(api.memoryRecordHistory).toHaveBeenCalledWith(store, records[0], 25, 0))
    expect(api.memoryEditPreview).not.toHaveBeenCalled()
    expect(api.memoryEditApply).not.toHaveBeenCalled()
  })

  it.each([
    { store: 'default', privateMemory: false },
    { store: 'legacy-team', privateMemory: false },
    { store: 'member-review', privateMemory: true },
  ])('explains an empty $store with its actual ownership', async ({ store, privateMemory }) => {
    records = []
    renderWithProviders(<MemoryRecordsEditor store={store} privateMemory={privateMemory} />)
    await screen.findByText(privateMemory ? 'A fresh start for this member' : 'No memories yet')
    if (privateMemory) {
      expect(screen.getByText(/the source stays unchanged/)).toBeVisible()
      expect(screen.queryByText('No memories yet')).toBeNull()
    } else {
      expect(screen.getByText('Saved facts, lessons, and experiences appear here.')).toBeVisible()
      expect(screen.queryByText('A fresh start for this member')).toBeNull()
      expect(screen.queryByText(/Private memory grows/)).toBeNull()
      expect(screen.queryByText(/the source stays unchanged/)).toBeNull()
    }
    expect(screen.queryByRole('checkbox', { name: 'Select this page' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Find and replace', exact: true })).toBeNull()
  })

  it('creates a fresh global fact, refreshes the records and clears the saved draft', async () => {
    const onDirtyChange = vi.fn()
    api.vectorSemanticWrite.mockImplementation(async (key: string, value: string) => {
      records.unshift(record(70, { key, id: key, text: value, value_json: JSON.stringify(value) }))
      return { ok: true }
    })
    renderWithProviders(<MemoryRecordsEditor store="default" onDirtyChange={onDirtyChange} />)
    await screen.findByText('Contact 0: old@example.com')
    expect(screen.queryByTestId('memory-recall-evidence')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Recall for this task' })).not.toBeInTheDocument()
    const key = screen.getByRole('textbox', { name: 'Key (e.g. pref.backend.framework)' })
    const value = screen.getByRole('textbox', { name: 'Value' })
    expect(screen.getByRole('button', { name: 'Set', exact: true })).toBeDisabled()
    fireEvent.change(key, { target: { value: 'project.language' } })
    fireEvent.change(value, { target: { value: 'Use TypeScript' } })
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true))
    fireEvent.submit(value.closest('form')!)
    expect(await screen.findByText('Use TypeScript')).toBeVisible()
    expect(api.vectorSemanticWrite).toHaveBeenCalledWith('project.language', 'Use TypeScript')
    expect(api.memoryEditApply).not.toHaveBeenCalled()
    expect(key).toHaveValue(''); expect(value).toHaveValue('')
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false))
  })

  it('retains a failed global fact draft for retry', async () => {
    api.vectorSemanticWrite.mockRejectedValueOnce(new Error('Fact write unavailable'))
    mount(); await screen.findByText('Contact 0: old@example.com')
    fireEvent.change(screen.getByRole('textbox', { name: 'Key (e.g. pref.backend.framework)' }), { target: { value: 'project.language' } })
    const value = screen.getByRole('textbox', { name: 'Value' })
    fireEvent.change(value, { target: { value: 'Use TypeScript' } })
    fireEvent.submit(value.closest('form')!)
    await screen.findByText('Fact write unavailable')
    expect(value).toHaveValue('Use TypeScript')
    expect(screen.getByRole('textbox', { name: 'Key (e.g. pref.backend.framework)' })).toHaveValue('project.language')
    fireEvent.submit(value.closest('form')!)
    await screen.findByText('Saved')
    expect(api.vectorSemanticWrite).toHaveBeenCalledTimes(2)
  })

  it.each([{ store: 'member-review', privateMemory: true }, { store: 'member-review', privateMemory: false }, { store: 'default', privateMemory: true }])('never exposes the global writer in $store with privateMemory=$privateMemory', async props => {
    renderWithProviders(<MemoryRecordsEditor {...props} />)
    await screen.findByText('Contact 0: old@example.com')
    expect(screen.queryByRole('button', { name: 'Set', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Value' })).not.toBeInTheDocument()
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
  })

  it.each(['default', 'member-review'])('keeps explicit selection across server pages in %s and never writes before reviewed apply', async store => {
    mount(store)
    await screen.findByText('Contact 0: old@example.com')
    fireEvent.click(checkbox(0))
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText('Contact 50: old@example.com')
    fireEvent.click(checkbox(50))
    expect(screen.getByText('Selected: 2')).toBeVisible()
    const dialog = await replaceForm()
    expect(within(dialog).getByText(/^Preview expires at .+\. Preview again after expiry\.$/)).toBeVisible()
    expect(api.memoryEditPreview).toHaveBeenCalledWith(store, { items: [expect.objectContaining({ id: record(0).id, revision: record(0).revision }), expect.objectContaining({ id: record(50).id, revision: record(50).revision })] }, { type: 'replace_text', find: 'old@example.com', replacement: 'new@example.com', match_case: false })
    expect(api.memoryEditApply).not.toHaveBeenCalled()
    expect(within(dialog).getByText('1–25 of 65')).toBeVisible()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' }))
    await waitFor(() => expect(api.memoryEditApply).toHaveBeenCalledWith(store, 'signed-preview'))
    expect(await screen.findByText('Updated records: 65')).toBeVisible()
  })

  it('reviews every signed preview page and still applies the original token', async () => {
    api.memoryEditPreviewPage.mockResolvedValue({
      preview_id: 'signed-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 65, changed_count: 65, unchanged_count: 0,
      entries: Array.from({ length: 25 }, (_, index) => ({ before: record(index + 25), after: record(index + 25, { value_json: JSON.stringify(`Contact ${index + 25}: new@example.com`) }) })),
      preview_offset: 25, preview_limit: 25, preview_has_more: true, warnings: [],
    })
    mount('member-review'); await screen.findByText('Contact 0: old@example.com'); fireEvent.click(checkbox(0))
    const dialog = await replaceForm()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Next page', exact: true }))
    expect(await within(dialog).findByText('26–50 of 65')).toBeVisible()
    expect(within(dialog).getByText('Contact 25: new@example.com')).toBeVisible()
    expect(api.memoryEditPreviewPage).toHaveBeenCalledExactlyOnceWith('member-review', 'signed-preview', 25)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' }))
    await waitFor(() => expect(api.memoryEditApply).toHaveBeenCalledExactlyOnceWith('member-review', 'signed-preview'))
  })

  it('keeps the reviewed page and selection when preview paging fails', async () => {
    const next = {
      preview_id: 'signed-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 65, changed_count: 65, unchanged_count: 0,
      entries: Array.from({ length: 25 }, (_, index) => ({ before: record(index + 25), after: record(index + 25, { value_json: JSON.stringify(`Contact ${index + 25}: new@example.com`) }) })),
      preview_offset: 25, preview_limit: 25, preview_has_more: true, warnings: [],
    }
    api.memoryEditPreviewPage.mockRejectedValueOnce(Object.assign(new Error('Preview page unavailable'), { status: 503 })).mockResolvedValueOnce(next)
    mount(); await screen.findByText('Contact 0: old@example.com'); fireEvent.click(checkbox(0))
    const dialog = await replaceForm()
    const applyButton = within(dialog).getByRole('button', { name: 'Apply changes (65)' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Next page', exact: true }))
    expect(await within(dialog).findByText('Preview page unavailable')).toBeVisible()
    expect(within(dialog).getByText('1–25 of 65')).toBeVisible()
    expect(within(dialog).getByText('Contact 0: new@example.com')).toBeVisible()
    expect(applyButton).toBeDisabled()
    expect(screen.getByText('Selected: 1')).toBeVisible()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry preview page' }))
    expect(await within(dialog).findByText('26–50 of 65')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Apply changes (65)' })).toBeEnabled()
    expect(api.memoryEditPreviewPage.mock.calls).toEqual([
      ['default', 'signed-preview', 25],
      ['default', 'signed-preview', 25],
    ])
    expect(api.memoryEditApply).not.toHaveBeenCalled()
  })

  it('selects every matching record by query and preserves cross-page exclusions', async () => {
    mount('member-review'); await screen.findByText('Contact 0: old@example.com')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select this page' }))
    fireEvent.click(screen.getByRole('button', { name: 'Select all 65 matching memories' }))
    expect(screen.getByRole('textbox', { name: 'Search memory' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Next page' })); await screen.findByText('Contact 50: old@example.com')
    fireEvent.click(checkbox(50)); expect(screen.getByText('Selected: 64')).toBeVisible()
    await replaceForm()
    expect(api.memoryEditPreview.mock.calls[0][1]).toEqual({ query: { q: '', kind: 'all' }, exclude: [{ kind: 'fact', id: record(50).id }] })
  })

  it.each(['default', 'member-review'])('searches addresses and later pages in %s without a hidden email filter', async store => {
    renderWithProviders(<MemoryRecordsEditor store={store} privateMemory={store !== 'default'} />)
    await screen.findByText('Contact 0: old@example.com')
    expect(screen.queryByRole('button', { name: 'Email', exact: true })).not.toBeInTheDocument()
    expect(screen.getAllByText('old@example.com', { exact: true })[0]).toBeVisible()
    expect(screen.queryByRole('button', { name: 'old@example.com', exact: true })).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'old@example.com' } })
    await waitFor(() => expect(api.memoryRecords).toHaveBeenLastCalledWith(store, { q: 'old@example.com', kind: 'all' }, 0, 50))
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'Contact 60:' } })
    expect(await screen.findByText('Contact 60: old@example.com')).toBeVisible()
    expect(api.memoryRecords).toHaveBeenLastCalledWith(store, { q: 'Contact 60:', kind: 'all' }, 0, 50)
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    await waitFor(() => expect(api.memoryRecords).toHaveBeenLastCalledWith(store, { q: '', kind: 'all' }, 0, 50))
  })

  it('corrects structured rules through a versioned preview without changing rule metadata', async () => {
    const value = { rule: 'Keep old reviews concise', repo_scope: 'private-repo', category: 'knowledge' }
    records = [record(0, { kind: 'directive', key: 'lesson.review', id: 'key:lesson.review', value_json: JSON.stringify(value) })]
    mount(); await screen.findByText(value.rule)
    fireEvent.click(screen.getByRole('button', { name: 'Edit', exact: true }))
    const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: 'Keep new reviews concise' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith('default', { items: [{ kind: 'directive', id: 'key:lesson.review', revision: records[0].revision }] }, { type: 'set', value: { ...value, rule: 'Keep new reviews concise' } }))
  })

  it('refreshes a stale selected record while retaining its correction draft', async () => {
    records = [record(0)]
    api.memoryEditApply.mockRejectedValueOnce(Object.assign(new Error('Preview is stale'), { status: 409 }))
    mount('member-review'); await screen.findByText(records[0].text)
    fireEvent.click(screen.getByRole('button', { name: 'Edit', exact: true }))
    const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: 'My new correction' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' })); await within(dialog).findByText('Will change: 65')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' })); await within(dialog).findByText('Preview is stale')
    records[0] = { ...records[0], revision: 'f'.repeat(64) }
    fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh selected records' }))
    expect(await within(dialog).findByDisplayValue('My new correction')).toBeVisible()
    expect(api.memoryRecordsRefresh).toHaveBeenCalledWith('member-review', [{ kind: 'fact', id: record(0).id }])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    await waitFor(() => expect(api.memoryEditPreview).toHaveBeenLastCalledWith('member-review', { items: [{ kind: 'fact', id: record(0).id, revision: 'f'.repeat(64) }] }, { type: 'set', value: 'My new correction' }))
  })

  it('retries the same preview after an uncertain apply without regenerating replacement edits', async () => {
    api.memoryEditApply.mockRejectedValueOnce(new Error('Connection lost after submission')).mockResolvedValue({ ok: true, changed_count: 65 })
    mount(); await screen.findByText('Contact 0: old@example.com'); fireEvent.click(checkbox(0))
    const dialog = await replaceForm()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' })); await within(dialog).findByText('Connection lost after submission')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry this application' }))
    await screen.findByText('Updated records: 65')
    expect(api.memoryEditPreview).toHaveBeenCalledTimes(1)
    expect(api.memoryEditApply.mock.calls).toEqual([['default', 'signed-preview'], ['default', 'signed-preview']])
  })

  it.each(['default', 'member-review'])('cancels a scoped %s forget without losing selection, then retains recovery limits on apply', async store => {
    api.memoryEditPreview.mockResolvedValue({ preview_id: 'forget-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 1, changed_count: 1, unchanged_count: 0, entries: [{ before: record(0), after: null, operation: 'forget' }], preview_offset: 0, preview_limit: 25, preview_has_more: false, warnings: [] })
    api.memoryEditApply.mockRejectedValueOnce(new Error('Forget was not confirmed'))
    const onDirtyChange = vi.fn()
    renderWithProviders(<MemoryRecordsEditor store={store} onDirtyChange={onDirtyChange} />)
    await screen.findByText('Contact 0: old@example.com'); fireEvent.click(checkbox(0))
    fireEvent.click(screen.getByRole('button', { name: 'Forget selected memories: 1', exact: true }))
    let dialog = screen.getByRole('dialog', { name: 'Forget selected memories: 1', exact: true })
    const explanation = 'Forget excludes these memories from search. No direct Undo. Recovery restores the whole store from a backup containing them after a Kiro Crew gateway restart.'
    await waitFor(() => expect(within(dialog).getByText(explanation)).toBeVisible())
    expect(api.memoryEditApply).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    let forget = await within(dialog).findByRole('button', { name: 'Forget selected memories: 1' })
    expect(within(dialog).queryByRole('button', { name: 'Adjust edit', exact: true })).toBeNull()
    expect(within(dialog).getAllByText(explanation)).toHaveLength(1)
    expect(within(dialog).getByText(explanation)).toBeVisible()
    expect(within(dialog).getByText('This memory will be forgotten.')).toBeVisible()
    expect(api.memoryEditPreview).toHaveBeenCalledWith(store, { items: [{ kind: 'fact', id: record(0).id, revision: record(0).revision }] }, { type: 'forget' })
    expect(api.memoryEditApply).not.toHaveBeenCalled()
    const cancel = within(dialog).getByRole('button', { name: 'Cancel', exact: true })
    await waitFor(() => expect(cancel).toBeVisible())
    expect(cancel).toBeEnabled()
    fireEvent.click(cancel)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Forget selected memories: 1', exact: true })).toBeNull())
    expect(api.memoryEditApply).not.toHaveBeenCalled()
    expect(checkbox(0)).toBeChecked()
    expect(screen.getByText('Selected: 1')).toBeVisible()
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByRole('button', { name: 'Forget selected memories: 1', exact: true }))
    dialog = screen.getByRole('dialog', { name: 'Forget selected memories: 1', exact: true })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
    forget = await within(dialog).findByRole('button', { name: 'Forget selected memories: 1' })
    expect(api.memoryEditPreview).toHaveBeenCalledTimes(2)
    expect(api.memoryEditPreview).toHaveBeenLastCalledWith(store, { items: [{ kind: 'fact', id: record(0).id, revision: record(0).revision }] }, { type: 'forget' })
    fireEvent.click(forget)
    expect(await within(dialog).findByText('Forget was not confirmed')).toBeVisible()
    expect(api.memoryEditApply).toHaveBeenCalledExactlyOnceWith(store, 'forget-preview')
    expect(within(dialog).getByRole('button', { name: 'Forget selected memories: 1' })).toBeEnabled()
  })
})

it.each([
  { source: 'user_explicit', label: 'You' },
  { source: 'user_seed', label: 'Copied' },
  { source: 'consolidation', label: 'Conversation' },
  { source: 'migration', label: 'Imported records' },
  { source: 'import', label: 'Imported records' },
  ...['copyright', 'ownerless', 'userland', 'session-token', 'copy:original-record', 'project.user_owner_contact', 'consolidation:dashboard:original'].map(source => ({ source, label: source })),
])('labels only known bare row sources, preserving $source', async ({ source, label }) => {
  records = [record(0, { source })]
  mount('member-review')
  fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
  const detail = screen.getByRole('dialog', { name: 'Memory details' })
  await waitFor(() => expect(within(detail).getByText(label, { exact: true })).toBeVisible())
})

it.each([
  { extra: { metadata: { source_ref: 'user_explicit' } }, reference: 'You' },
  { extra: { metadata: { source_ref: 'consolidation' } }, reference: 'Conversation' },
  { extra: { metadata: { source_ref: 'user_seed' } }, reference: 'Copied' },
  { extra: { derived_from: 'import' }, reference: 'Imported records' },
  { extra: { metadata: { source_ref: 'conversation:source#message:1' } }, reference: 'conversation:source#message:1' },
  { extra: { metadata: { source_ref: 'project.user_owner_contact' } }, reference: 'project.user_owner_contact' },
  { extra: { derived_from: JSON.stringify({ source_store: 'default', source_key: 'user_explicit' }), metadata: { source_ref: 'user_seed' } }, reference: 'user_explicit' },
  { extra: { derived_from: JSON.stringify({ source_store: 'default', item_id: 'consolidation' }), metadata: { source_ref: 'user_seed' } }, reference: 'consolidation' },
])('labels bare source tags while preserving reference $reference', async ({ extra, reference }) => {
  records = [record(0, extra)]
  mount('member-review')
  fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
  const detail = screen.getByRole('dialog', { name: 'Memory details' })
  await waitFor(() => expect(within(detail).getByText(`Original reference: ${reference}`)).toBeVisible())
})


it.each([
  { source: 'consolidation', metadata: JSON.stringify({ source_ref: 'conversation:proposed-contact' }), label: 'Conversation', reference: 'conversation:proposed-contact' },
  { source: '', metadata: '{invalid', label: 'Source not recorded', reference: undefined },
])('shows the proposal’s own $label origin and equally weighted choices before previewing', async ({ source, metadata, label, reference }) => {
  records = [record(0, { metadata: { pending_conflicts: 1, source_ref: 'conversation:current-contact' } })]
  api.memoryRecordHistory.mockResolvedValue({ entries: [{ id: 'proposal-1', revision: 2, base_revision: 1, status: 'conflict', operation: 'set', source, before_json: JSON.stringify({ value_json: records[0].value_json }), after_json: JSON.stringify({ value_json: JSON.stringify('Use team@example.com') }), metadata_json: metadata, created_at: '2026-09-07T12:00:00Z' }], has_more: false, current_revision: 1 })
  mount('member-review')
  fireEvent.click(await screen.findByRole('button', { name: 'Review proposals (1)' }))
  const detail = screen.getByRole('dialog', { name: 'Memory details' })
  await waitFor(() => expect(within(detail).getByText('Use team@example.com')).toBeVisible())
  expect(api.memoryRecordHistory).toHaveBeenCalledWith('member-review', records[0], 25, 0)
  const proposal = within(detail).getByText('Proposed change').closest('details')!
  expect(within(proposal).getByText(label)).toBeVisible()
  if (reference) expect(within(proposal).getByText(`Original reference: ${reference}`)).toBeVisible()
  expect(within(proposal).queryByText('You')).toBeNull()
  expect(within(proposal).queryByText(/conversation:current-contact/)).toBeNull()
  const useProposal = within(proposal).getByRole('button', { name: 'Use proposed value' })
  const keepCurrent = within(proposal).getByRole('button', { name: 'Keep current value' })
  expect(useProposal.className).toBe(keepCurrent.className)
  fireEvent.click(useProposal)
  const correction = screen.getByRole('dialog', { name: 'Edit memory' })
  expect(within(correction).getByRole('textbox', { name: 'Memory text' })).toHaveValue('Use team@example.com')
  expect(api.memoryEditApply).not.toHaveBeenCalled()
  fireEvent.click(within(correction).getByRole('button', { name: 'Preview changes' }))
  await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith('member-review', { items: [{ kind: 'fact', id: records[0].id, revision: records[0].revision }] }, { type: 'set', value: 'Use team@example.com' }))
})

it.each(['default', 'member-review'])('reviews a %s deletion proposal as a scoped forget before applying', async store => {
  const current = record(0, { metadata: { pending_conflicts: 1, revision: 3 } })
  records = [current, record(1)]
  const deletion = { operation: 'forget', source: 'consolidation', before_json: JSON.stringify({ value_json: current.value_json }), metadata_json: '{}', created_at: '2026-09-07T12:00:00Z' }
  api.memoryRecordHistory.mockResolvedValue({ current_revision: 3, entries: [
    { ...deletion, id: 4, revision: 4, base_revision: 3, status: 'conflict', after_json: JSON.stringify({ value_json: current.value_json, is_deleted: 1 }) },
    { ...deletion, id: 2, revision: 2, base_revision: 1, status: 'accepted', after_json: null },
  ], has_more: false })
  api.memoryEditPreview.mockResolvedValue({ preview_id: 'forget-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 1, changed_count: 1, unchanged_count: 0, entries: [{ before: current, after: null, operation: 'forget' }], preview_offset: 0, preview_limit: 25, preview_has_more: false, warnings: [] })
  api.memoryEditApply.mockImplementation(async () => {
    records = records.filter(row => row.id !== current.id)
    return { ok: true, changed_count: 1 }
  })
  mount(store)
  fireEvent.click(await screen.findByRole('checkbox', { name: `Select memory: ${record(1).text}`, exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Review proposals (1)' }))
  const detail = screen.getByRole('dialog', { name: 'Memory details' })
  const proposal = (await within(detail).findByText('Proposed change')).closest('details')!
  await waitFor(() => expect(within(proposal).getByText('This memory will be forgotten.')).toBeVisible())
  expect(within(proposal).getAllByText(current.text)).toHaveLength(1)
  const accepted = within(detail).getByText('Recorded change').closest('details')!
  fireEvent.click(within(accepted).getByText('Recorded change').closest('summary')!)
  expect(within(accepted).getByText('This memory will be forgotten.')).toBeVisible()
  expect(within(accepted).queryByRole('button', { name: 'Use proposed value' })).not.toBeInTheDocument()
  fireEvent.click(within(proposal).getByRole('button', { name: 'Use proposed value' }))
  const forget = screen.getByRole('dialog', { name: 'Forget', exact: true })
  expect(within(forget).queryByRole('textbox', { name: 'Memory text' })).not.toBeInTheDocument()
  expect(api.memoryEditPreview).not.toHaveBeenCalled()
  expect(api.memoryEditApply).not.toHaveBeenCalled()
  fireEvent.click(within(forget).getByRole('button', { name: 'Preview changes' }))
  await within(forget).findByText('This memory will be forgotten.')
  expect(within(forget).queryByRole('button', { name: 'Adjust edit', exact: true })).toBeNull()
  expect(api.memoryEditPreview).toHaveBeenCalledExactlyOnceWith(store, { items: [{ kind: 'fact', id: current.id, revision: current.revision }] }, { type: 'forget' })
  expect(api.memoryEditApply).not.toHaveBeenCalled()
  fireEvent.click(within(forget).getByRole('button', { name: 'Forget selected memories: 1' }))
  await waitFor(() => expect(api.memoryEditApply).toHaveBeenCalledExactlyOnceWith(store, 'forget-preview'))
  expect(await screen.findByText('Updated records: 1')).toBeVisible()
  await waitFor(() => expect(screen.queryByText(current.text)).not.toBeInTheDocument())
  expect(screen.getByText(record(1).text)).toBeVisible()
})

it.each(['default', 'member-review'])('keeps a missing %s correction draft refreshable after the record is restored', async store => {
  records = [record(0)]
  api.memoryEditPreview.mockRejectedValueOnce(Object.assign(new Error('The selected record changed'), { status: 409 }))
  api.memoryRecordsRefresh.mockResolvedValueOnce({ entries: [], missing: [{ kind: 'fact', id: record(0).id }] })
  mount(store); await screen.findByText(records[0].text)
  fireEvent.click(screen.getByRole('button', { name: 'Edit', exact: true }))
  const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: 'Keep this draft available' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
  await within(dialog).findByText('The selected record changed')
  fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh selected records' }))
  await within(dialog).findByText('Missing memories removed from the selection; the rest is refreshed.')
  expect(within(dialog).getByRole('textbox', { name: 'Memory text' })).toHaveValue('Keep this draft available')
  expect(within(dialog).getByRole('button', { name: 'Preview changes' })).toBeDisabled()
  expect(api.memoryEditApply).not.toHaveBeenCalled()
  records[0] = { ...records[0], revision: 'f'.repeat(64) }
  fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh selected records' }))
  await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Preview changes' })).toBeEnabled())
  expect(within(dialog).getByRole('textbox', { name: 'Memory text' })).toHaveValue('Keep this draft available')
  fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
  await waitFor(() => expect(api.memoryEditPreview).toHaveBeenLastCalledWith(store, { items: [{ kind: 'fact', id: records[0].id, revision: records[0].revision }] }, { type: 'set', value: 'Keep this draft available' }))
  expect(api.memoryEditApply).not.toHaveBeenCalled()
})

it.each(['default', 'member-review'])('refreshes %s query membership and inactive exclusions without losing the batch draft', async store => {
  api.memoryEditApply.mockRejectedValueOnce(Object.assign(new Error('Query membership changed'), { status: 409 }))
  mount(store); await screen.findByText('Contact 0: old@example.com')
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select this page' }))
  fireEvent.click(screen.getByRole('button', { name: 'Select all 65 matching memories' }))
  fireEvent.click(checkbox(0)); fireEvent.click(checkbox(1))
  const dialog = await replaceForm()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' }))
  await within(dialog).findByText('Query membership changed')
  // Both excluded identities are gone, and three matching records arrived.
  records = [...records.slice(2), record(65), record(66), record(67)]
  api.memoryQuerySelectionRefresh.mockRejectedValueOnce(new Error('Selection refresh unavailable')).mockResolvedValue({ matched_count: 66 })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh selected records' }))
  await within(dialog).findByText('Selection refresh unavailable')
  expect(within(dialog).queryByText('Query membership changed')).not.toBeInTheDocument()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh selected records' }))
  await screen.findByText('Selected: 66')
  expect(api.memoryQuerySelectionRefresh).toHaveBeenCalledWith(store, { query: { q: '', kind: 'all' }, exclude: [{ kind: 'fact', id: record(0).id }, { kind: 'fact', id: record(1).id }] })
  expect(within(dialog).getByLabelText('Find text')).toHaveValue('old@example.com')
  expect(within(dialog).getByLabelText('Replace with')).toHaveValue('new@example.com')
  expect(api.memoryEditPreview).toHaveBeenCalledTimes(1)
  expect(api.memoryEditApply).toHaveBeenCalledTimes(1)
  fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
  await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledTimes(2))
  expect(api.memoryEditPreview.mock.calls[1][1]).toEqual(api.memoryQuerySelectionRefresh.mock.calls[0][1])
})

it('allows clearing an all-matching scope that becomes empty during review', async () => {
  api.memoryEditApply.mockRejectedValueOnce(Object.assign(new Error('All matches disappeared'), { status: 409 }))
  mount(); await screen.findByText('Contact 0: old@example.com')
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select this page' }))
  fireEvent.click(screen.getByRole('button', { name: 'Select all 65 matching memories' }))
  const dialog = await replaceForm()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' }))
  await within(dialog).findByText('All matches disappeared')
  records = []
  api.memoryQuerySelectionRefresh.mockResolvedValue({ matched_count: 0 })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh selected records' }))
  await screen.findByText('Selected: 0')
  expect(within(dialog).getByRole('button', { name: 'Preview changes' })).toBeDisabled()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }))
  expect(screen.getByRole('textbox', { name: 'Search memory' })).toBeEnabled()
})

it.each(['default', 'member-review'])('loads older %s revisions and retries a failed page without losing current history', async store => {
  records = [record(0)]
  const revisions = Array.from({ length: 26 }, (_, index) => ({ id: index + 1, revision: index + 1, base_revision: index, status: 'accepted', operation: 'set', source: 'user_explicit', before_json: null, after_json: JSON.stringify({ value_json: JSON.stringify(`Saved contact revision ${index + 1}`) }), metadata_json: '{}', created_at: '2026-09-07T12:00:00Z' }))
  api.memoryRecordHistory.mockResolvedValueOnce({ entries: revisions.slice(0, 25), current_revision: 26, has_more: true })
    .mockRejectedValueOnce(Object.assign(new Error('History page unavailable'), { status: 400 }))
    .mockResolvedValue({ entries: [revisions[24], revisions[25]], current_revision: 26, has_more: false })
  mount(store)
  fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
  const dialog = screen.getByRole('dialog', { name: 'Memory details' })
  fireEvent.click(await within(dialog).findByRole('button', { name: 'Show more' }))
  await within(dialog).findByText('History page unavailable')
  expect(within(dialog).getByText('Saved contact revision 1')).toBeInTheDocument()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Retry memory access' }))
  await within(dialog).findByText('Saved contact revision 26')
  expect(api.memoryRecordHistory.mock.calls.map(call => [call[0], call[2], call[3]])).toEqual([[store, 25, 0], [store, 25, 25], [store, 25, 25]])
  expect(within(dialog).getAllByText('Saved contact revision 25')).toHaveLength(1)
  expect(within(dialog).queryByRole('button', { name: 'Show more' })).not.toBeInTheDocument()
  expect(api.memoryEditPreview).not.toHaveBeenCalled()
})


it('preserves JSON scalar types when correcting an existing null value', async () => {
  records = [record(0, { value_json: 'null', text: 'null' })]
  mount(); await screen.findByText('null')
  fireEvent.click(screen.getByRole('button', { name: 'Edit', exact: true }))
  const dialog = screen.getByRole('dialog', { name: 'Edit memory' })
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Memory text' }), { target: { value: 'false' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes' }))
  await waitFor(() => expect(api.memoryEditPreview).toHaveBeenCalledWith('default', expect.any(Object), { type: 'set', value: false }))
})


it('hides prior recall evidence immediately when the search query changes', async () => {
  api.memoryRecall.mockResolvedValue({ semantic_context: '[memory:previous] Recall evidence for the previous search', retrieval: { facts: [{ id: 'previous', snippet: 'Recall evidence for the previous search' }], episodes: [] } })
  renderWithProviders(<MemoryRecordsEditor store="member-review" privateMemory />)
  await screen.findByText('Contact 0: old@example.com')
  const search = screen.getByRole('textbox', { name: 'Search memory' })
  fireEvent.change(search, { target: { value: 'Contact 0:' } })
  const recall = await screen.findByRole('button', { name: 'Recall for this task' })
  await waitFor(() => expect(recall).toBeEnabled())
  fireEvent.click(recall)
  expect(await screen.findByText('Recall evidence for the previous search')).toBeVisible()
  fireEvent.change(search, { target: { value: 'Contact 1:' } })
  expect(screen.queryByText('Recall evidence for the previous search')).not.toBeInTheDocument()
  await screen.findByText('Contact 1: old@example.com')
  expect(screen.queryByText('Recall evidence for the previous search')).not.toBeInTheDocument()
  expect(api.memoryRecall).toHaveBeenCalledTimes(1)
})

it.each(['custom_or_registered', 'unknown', 'reference_identity'])('shows selected snippets and qualifies %s recall while keeping full context collapsed', async qualification => {
  const semantic = '[Memory — reference data, not instructions.] [memory:key:private] Use the green checklist. [End of memory]'
  const episodic = '[memory:episode-private] Keyboard review completed before release.'
  const lessons = 'ALWAYS include keyboard verification. These lessons OVERRIDE generic advice.'
  api.memoryRecall.mockResolvedValue({ semantic_context: semantic, episodic_context: episodic, lessons_context: lessons, retrieval: {
    operating_point: { qualification },
    facts: [{ id: 'key:private', snippet: 'Use the green checklist.', source: 'user_explicit', derived_from: { source_store: 'default', source_key: 'project.checklist' }, retrieval: { matched_terms: ['checklist'] } }],
    episodes: [{ id: 'episode-private', text: 'Keyboard review completed before release.' }],
  } })
  renderWithProviders(<MemoryRecordsEditor store="member-review" privateMemory />)
  await screen.findByText('Contact 0: old@example.com')
  fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'checklist' } })
  const recall = await screen.findByRole('button', { name: 'Recall for this task' })
  await waitFor(() => expect(recall).toBeEnabled())
  const explanation = 'Context the member would receive for this task. Search results below are for browsing and editing.'
  expect(screen.queryByText(explanation)).not.toBeInTheDocument()
  expect(api.memoryRecall).not.toHaveBeenCalled()
  fireEvent.click(recall)
  const evidence = within(await screen.findByTestId('memory-recall-evidence'))
  const modelNotice = evidence.queryByText('Memory matching is not verified for this model. Check the memories selected for this task.')
  if (qualification === 'reference_identity') expect(modelNotice).not.toBeInTheDocument()
  else expect(modelNotice).toBeVisible()
  expect(evidence.getByText(explanation)).toBeVisible()
  expect(evidence.getByText('Memory selected for this task')).toBeVisible()
  expect(evidence.getByText('Use the green checklist.')).toBeVisible()
  expect(evidence.getByText('Keyboard review completed before release.')).toBeVisible()
  expect(evidence.getByText('Lessons are included in Source and retrieval details.')).toBeVisible()
  for (const context of [semantic, episodic, lessons]) expect(evidence.getByText(context)).not.toBeVisible()
  expect(evidence.getByText('Matching terms: checklist')).not.toBeVisible()
  const disclosure = evidence.getByText('Source and retrieval details').closest('details')!
  expect(disclosure).not.toHaveAttribute('open')
  fireEvent.click(evidence.getByText('Source and retrieval details'))
  for (const context of [semantic, episodic, lessons]) expect(evidence.getByText(context)).toBeVisible()
  expect(evidence.getByText('Matching terms: checklist')).toBeVisible()
  expect(evidence.getByText('Original reference: project.checklist')).toBeVisible()
  expect(api.memoryRecall).toHaveBeenCalledWith('checklist', 'member-review')
})

it.each(['rules', 'empty'])('keeps the model notice visible in %s recall without factual matches', async contents => {
  api.memoryRecall.mockResolvedValue({ lessons_context: contents === 'rules' ? 'ALWAYS verify keyboard navigation.' : '', retrieval: { facts: [], episodes: [], operating_point: { qualification: 'unknown' } } })
  renderWithProviders(<MemoryRecordsEditor store="member-review" privateMemory />)
  await screen.findByText('Contact 0: old@example.com')
  fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'keyboard' } })
  const recall = await screen.findByRole('button', { name: 'Recall for this task' })
  await waitFor(() => expect(recall).toBeEnabled())
  fireEvent.click(recall)
  const evidence = within(await screen.findByTestId('memory-recall-evidence'))
  expect(evidence.getByText('Memory matching is not verified for this model. Check the memories selected for this task.')).toBeVisible()
  if (contents === 'rules') {
    expect(evidence.queryByText('No matching memories')).not.toBeInTheDocument()
    expect(evidence.getByText('Lessons are included in Source and retrieval details.')).toBeVisible()
    expect(evidence.getByText('ALWAYS verify keyboard navigation.')).not.toBeVisible()
    fireEvent.click(evidence.getByText('Source and retrieval details'))
    expect(evidence.getByText('ALWAYS verify keyboard navigation.')).toBeVisible()
  } else {
    expect(evidence.getByText('No matching memories')).toBeVisible()
  }
})

it('keeps historical conflict proposals readable without offering them as currently pending', async () => {
  records = [record(0)]
  api.memoryRecordHistory.mockResolvedValue({ current_revision: 3, entries: [{ id: 'old-proposal', revision: 2, base_revision: 1, status: 'conflict', operation: 'set', source: 'consolidation', before_json: null, after_json: JSON.stringify({ value_json: JSON.stringify('Historical proposed contact') }), metadata_json: '{}', created_at: '2026-09-07T12:00:00Z' }], has_more: false })
  mount(); await screen.findByText(records[0].text)
  fireEvent.click(screen.getByRole('button', { name: 'View details', exact: true }))
  const dialog = screen.getByRole('dialog', { name: 'Memory details' })
  await within(dialog).findByText('Proposed change')
  expect(within(dialog).queryByRole('button', { name: 'Use proposed value' })).not.toBeInTheDocument()
  const disclosure = within(dialog).getByText('Proposed change').closest('summary')!
  fireEvent.click(disclosure)
  expect(within(dialog).getByText('Historical proposed contact')).toBeInTheDocument()
})


it.each(['set', 'forget'])('previews keeping the current value as an explicit %s proposal resolution before applying', async operation => {
  records = [record(0, { metadata: { pending_conflicts: 1, revision: 1 } })]
  api.memoryRecordHistory.mockResolvedValue({ current_revision: 1, entries: [{ id: 'pending-proposal', revision: 2, base_revision: 1, status: 'conflict', operation, source: 'consolidation', before_json: JSON.stringify({ value_json: records[0].value_json }), after_json: JSON.stringify(operation === 'forget' ? { value_json: records[0].value_json, is_deleted: 1 } : { value_json: JSON.stringify('Unconfirmed proposed contact') }), metadata_json: '{}', created_at: '2026-09-07T12:00:00Z' }], has_more: false })
  api.memoryEditPreview.mockResolvedValue({ preview_id: 'resolve-preview', expires_at: '2026-12-31T12:00:00Z', matched_count: 1, changed_count: 1, unchanged_count: 0, entries: [{ before: records[0], after: records[0], operation: 'resolve' }], preview_offset: 0, preview_limit: 25, preview_has_more: false, warnings: [] })
  api.memoryEditApply.mockResolvedValue({ ok: true, changed_count: 1 })
  mount('member-review')
  fireEvent.click(await screen.findByRole('button', { name: 'Review proposals (1)' }))
  const detail = screen.getByRole('dialog', { name: 'Memory details' })
  fireEvent.click(await within(detail).findByRole('button', { name: 'Keep current value' }))
  const preview = screen.getByRole('dialog', { name: 'Edit memory' })
  await within(preview).findByText('Keep the current value. Applying records this decision and closes its pending proposals.')
  expect(api.memoryEditPreview).toHaveBeenCalledWith('member-review', { items: [{ kind: 'fact', id: records[0].id, revision: records[0].revision }] }, { type: 'set', value: 'Contact 0: old@example.com' })
  expect(api.memoryEditApply).not.toHaveBeenCalled()
  expect(within(preview).getByRole('button', { name: 'Apply changes (1)' })).toBeEnabled()
  fireEvent.click(within(preview).getByRole('button', { name: 'Apply changes (1)' }))
  await waitFor(() => expect(api.memoryEditApply).toHaveBeenCalledWith('member-review', 'resolve-preview'))
  expect(await screen.findByText('Updated records: 1')).toBeVisible()
})


it.each(['default', 'member-review'])('preserves %s query, cross-page selection and reviewed preview across both shell breakpoints', async store => {
  const view = () => <SidePanelLayout title="Settings" tabs={[{ key: 'overview', label: 'Overview', icon: null }]} basePath="/settings" paneOwnsHeader={store !== 'default'}>
    {() => <MemoryRecordsEditor store={store} />}
  </SidePanelLayout>
  const rendered = renderWithProviders(view(), { route: '/settings/overview?view=memory' })
  await screen.findByText('Contact 0: old@example.com')
  fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'Contact' } })
  await waitFor(() => expect(api.memoryRecords).toHaveBeenLastCalledWith(store, { q: 'Contact', kind: 'all' }, 0, 50))
  await screen.findByText('Contact 0: old@example.com')
  fireEvent.click(checkbox(0))
  fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
  await screen.findByText('Contact 50: old@example.com')
  fireEvent.click(checkbox(50))
  const dialog = await replaceForm()
  viewport.mobile = true
  rendered.rerender(view())
  expect(screen.getByRole('dialog', { name: 'Edit selected memories' })).toBe(dialog)
  expect(screen.getByRole('textbox', { name: 'Search memory' })).toHaveValue('Contact')
  expect(screen.getByText('Selected: 2')).toBeVisible()
  expect(screen.getByText('51–65 of 65')).toBeVisible()
  viewport.mobile = false
  rendered.rerender(view())
  expect(screen.getByRole('dialog', { name: 'Edit selected memories' })).toBe(dialog)
  expect(within(dialog).getByText('Will change: 65')).toBeVisible()
  expect(api.memoryEditPreview).toHaveBeenCalledTimes(1)
  fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes (65)' }))
  await waitFor(() => expect(api.memoryEditApply).toHaveBeenCalledWith(store, 'signed-preview'))
})
