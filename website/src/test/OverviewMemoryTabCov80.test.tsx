import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import userEvent from '@testing-library/user-event'

/**
 * Companion to integration/MemoryTab.integration.test.tsx (which drives the tab
 * against the MSW fixtures). This file stubs the api client directly so the
 * write paths — every Save, the lesson add/delete, and the manual consolidation
 * including its partial-failure branch — are assertable by their calls.
 */
// `vi.hoisted`, not a plain const: `./helpers` imports the Redux store, which
// imports `api/client`, and `vi.mock` is hoisted above a plain declaration -- so
// the factory would run before `api` was initialized and the whole suite would
// fail to load with "Cannot access 'api' before initialization".
const { api } = vi.hoisted(() => ({
  api: {
    lessons: vi.fn(),
    memoryPreferences: vi.fn(),
    memoryProjects: vi.fn(),
    memoryHistory: vi.fn(),
    memorySettings: vi.fn(),
    saveMemorySettings: vi.fn(),
    saveMemoryPreferences: vi.fn(),
    saveMemoryProjects: vi.fn(),
    saveMemoryHistory: vi.fn(),
    createLesson: vi.fn(),
    deleteLesson: vi.fn(),
    sessions: vi.fn(),
    consolidateMemory: vi.fn(),
    // The store picker and the three store-scoped cards the tab now mounts. Stubbed
    // here even though this file asserts none of them: an absent method is called as
    // `undefined` by its queryFn, which surfaces as the picker's refusal notice in
    // every test rather than as a missing-stub error naming the cause.
    memoryStores: vi.fn(),
    memoryRetired: vi.fn(),
    memoryBackups: vi.fn(),
    memoryCarve: vi.fn(),
    memoryBackupNow: vi.fn(),
    memoryRestoreBackup: vi.fn(),
    memoryRestoreRetired: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))
// The failed-session link opens the chat through the router; the destination is
// what the test reads, so `useNavigate` is the one router hook replaced.
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})
// Both cards own their own queries and their own tests; here they are seams that
// report the vector/migration state this tab branches on.
vi.mock('../pages/overview/VectorMemoryCard', () => ({
  default: ({ diagnosticsOnly }: { diagnosticsOnly?: boolean }) => <div data-testid="vector-card" data-diagnostics-only={diagnosticsOnly ? 'true' : 'false'} />,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div data-testid="embed-card" /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div data-testid="records-editor" /> }))

const MemoryTab = (await import('../pages/overview/MemoryTab')).default

const LESSONS = [
  { rule: 'zzq-rule-beta', category: 'tool', ts: '2026-01-02T00:00:00Z', repo_scope: '' },
  { rule: 'zzq-rule-alpha', category: 'knowledge', ts: '2026-01-01T00:00:00Z', repo_scope: '' },
]

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  api.lessons.mockResolvedValue({ lessons: LESSONS })
  api.memoryPreferences.mockResolvedValue({ content: 'zzq-prefs-body' })
  api.memoryProjects.mockResolvedValue({ content: 'zzq-projects-body' })
  api.memoryHistory.mockResolvedValue({ content: 'zzq-history-body' })
  api.memorySettings.mockResolvedValue({
    history_idle_hours: 4, history_max_days: 30, migrated: false,
  })
  api.saveMemorySettings.mockResolvedValue({ ok: true })
  api.saveMemoryPreferences.mockResolvedValue({ ok: true })
  api.saveMemoryProjects.mockResolvedValue({ ok: true })
  api.saveMemoryHistory.mockResolvedValue({ ok: true })
  api.createLesson.mockResolvedValue({ ok: true, outcome: 'inserted', reason: '' })
  api.deleteLesson.mockResolvedValue({ ok: true })
  api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }] })
  api.consolidateMemory.mockResolvedValue({ ok: true })
  // One declared store, the default. That keeps this file's subject the tab's own
  // write paths: the picker has nothing to switch to, so `store` stays at the
  // default and every read below is the storeless one these tests already assert.
  api.memoryStores.mockResolvedValue({
    stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }],
  })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
})

afterEach(() => {
  vi.useRealTimers()
})

/** The Save button inside the card whose heading contains `heading`.
 *
 * `getAllByText`, not `getByText`: the tab carries a disclosure line naming which
 * cards do NOT follow the store picker, and it says "Memory settings" in prose —
 * so a heading pattern legitimately matches twice and the single-match form throws
 * before reaching the button. The card is identified by CONTAINING a Save button
 * rather than by being the first match, which is the property the caller wants.
 */
function saveIn(heading: RegExp): HTMLButtonElement {
  // A real `.card-glow` ancestor is REQUIRED, with no widening fallback. The prose
  // match has none, so `closest('div').parentElement` resolves to the tab's own
  // container — which holds every card, and therefore holds a Save button. That
  // returns the FIRST Save on the page and the assertion then waits forever on a
  // save the click never reached.
  for (const title of screen.getAllByText(heading)) {
    const card = title.closest('.card-glow')
    if (!card) continue
    const button = Array.from(card.querySelectorAll('button'))
      .find((b) => /save/i.test(b.textContent ?? ''))
    if (button) return button as HTMLButtonElement
  }
  throw new Error(`no card matching ${heading} carries a Save button`)
}

/** The lessons table's body rows.
 *
 * Scoped to the table carrying the "Rule" header rather than to `document`: the
 * store-scoped cards below the lessons card render their own tables, and each
 * mounts an empty-state row immediately — so a document-wide `tbody tr` query
 * silently picks up "Nothing has been retired" as if it were a lesson.
 */
function lessonRows(): HTMLTableRowElement[] {
  const header = screen.getByText(/^Rule$/)
  const table = header.closest('table')
  return Array.from((table as HTMLTableElement).querySelectorAll('tbody tr'))
}

describe('MemoryTab — settings', () => {
  it('keeps bulk management lazy while the legacy browser remains visible', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    expect(screen.queryByTestId('records-editor')).toBeNull()
    expect(await screen.findByText(/^Memory Settings$/i)).toBeInTheDocument()

    const ordered = [
      screen.getByRole('heading', { name: /^Memory Settings \?$/i }),
      screen.getByTestId('vector-card'),
      screen.getByTestId('embed-card'),
      screen.getByText(/^Edit saved memories$/i),
      screen.getByRole('heading', { name: /^Preferences\b/i }),
      screen.getByRole('heading', { name: /^Lessons\b/i }),
    ]
    for (let index = 0; index < ordered.length - 1; index += 1) {
      expect(ordered[index].compareDocumentPosition(ordered[index + 1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }

    await userEvent.click(screen.getByText(/^Edit saved memories$/i))
    expect(screen.getByTestId('records-editor')).toBeInTheDocument()
    expect(screen.getByTestId('vector-card')).toHaveAttribute('data-diagnostics-only', 'false')
  })

  it('loads the saved retention settings and writes both fields back', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const inputs = await waitFor(() => {
      const found = screen.getAllByRole('spinbutton') as HTMLInputElement[]
      expect(found[0].value).toBe('4')
      return found
    })
    expect(inputs[1].value).toBe('30')

    fireEvent.change(inputs[0], { target: { value: '6' } })
    fireEvent.change(inputs[1], { target: { value: '45' } })
    await userEvent.click(saveIn(/Memory Settings/i))

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      history_idle_hours: 6, history_max_days: 45,
    }))
    expect(await screen.findByText(/Saved/)).toBeInTheDocument()
  })

  it('hides the retention field, and the text-file editors, once memory is migrated', async () => {
    api.memorySettings.mockResolvedValue({
      history_idle_hours: 3, history_max_days: 90, migrated: true,
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(screen.getAllByRole('spinbutton')).toHaveLength(1))
    expect(screen.getByText(/read-only/i)).toBeInTheDocument()
  })

  it('clears the transient Saved marker on its own timer', async () => {
    vi.useFakeTimers()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await act(async () => {})
    const save = saveIn(/Memory Settings/i)
    fireEvent.click(save)
    await act(async () => {})
    expect(save.textContent).toContain('Saved')

    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(save.textContent).not.toContain('Saved')
  })
})

describe('MemoryTab — the three text stores', () => {
  it.each(['pending', 'failed'] as const)(
    'cannot overwrite a document while its initial read is %s',
    async state => {
      if (state === 'pending') {
        api.memoryPreferences.mockImplementation(() => new Promise(() => {}))
      } else {
        api.memoryPreferences.mockRejectedValue(Object.assign(new Error('zzq-read-failed'), { status: 403 }))
      }
      renderWithProviders(<MemoryTab refreshTrigger={0} />)
      await screen.findByDisplayValue('zzq-projects-body')
      if (state === 'failed') await screen.findByText('zzq-read-failed')

      const save = saveIn(/^Preferences$/)
      expect(save).toBeDisabled()
      fireEvent.click(save)
      expect(api.saveMemoryPreferences).not.toHaveBeenCalled()
    },
  )

  it('can save a genuinely empty document after its read succeeds', async () => {
    api.memoryPreferences.mockResolvedValue({ content: '' })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByDisplayValue('zzq-projects-body')
    const save = saveIn(/^Preferences$/)
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await waitFor(() => expect(api.saveMemoryPreferences).toHaveBeenCalledWith('', undefined))
  })

  it('shows a redacted document read-only and never sends its masked body', async () => {
    api.memoryPreferences.mockResolvedValue({
      content: 'keep [REDACTED: credential] hidden',
      content_redacted: true,
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const preferences = await screen.findByDisplayValue('keep [REDACTED: credential] hidden')
    expect(preferences).toBeDisabled()
    expect(screen.getByText(/Sensitive values are hidden/)).toBeInTheDocument()
    const save = saveIn(/^Preferences$/)
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(api.saveMemoryPreferences).not.toHaveBeenCalled()
  })

  it('preserves a draft but blocks stale cached content during a redacted refetch', async () => {
    const view = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const preferences = await screen.findByDisplayValue('zzq-prefs-body')
    fireEvent.change(preferences, { target: { value: 'recover this unrelated draft' } })
    let finishRead!: (value: unknown) => void
    api.memoryPreferences.mockImplementation(
      () => new Promise(resolve => { finishRead = resolve }),
    )

    act(() => {
      void view.queryClient.invalidateQueries({ queryKey: ['memory-doc', 'preferences', ''] })
    })
    const save = saveIn(/^Preferences$/)
    await waitFor(() => expect(save).toBeDisabled())
    fireEvent.click(save)
    expect(api.saveMemoryPreferences).not.toHaveBeenCalled()

    finishRead({ content: '[REDACTED: credential]', content_redacted: true })
    expect(await screen.findByText(/Sensitive values are hidden/)).toBeInTheDocument()
    expect(preferences).toHaveValue('recover this unrelated draft')
    expect(preferences).toBeDisabled()
    expect(save).toBeDisabled()
  })

  it('does not save cached clean content after its confirming refetch fails', async () => {
    const view = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByDisplayValue('zzq-prefs-body')
    api.memoryPreferences.mockRejectedValueOnce(
      Object.assign(new Error('fresh read failed'), { status: 403 }),
    )

    await act(async () => {
      await view.queryClient.refetchQueries({ queryKey: ['memory-doc', 'preferences', ''] })
    })

    expect(await screen.findByText('fresh read failed')).toBeInTheDocument()
    const save = saveIn(/^Preferences$/)
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(api.saveMemoryPreferences).not.toHaveBeenCalled()
  })

  // The saves assert `undefined` as the second argument on purpose. A save carries
  // the picked store, and `undefined` is what "no store named" has to look like on
  // the wire: the gateway reads an ABSENT ?store= as the global store and
  // applies the owner gate only to a parameter that is present, so a save that sent
  // `store=default` here would turn a write every session can make into an
  // owner-only one. Asserting the arity is what pins that.
  it('loads each store and saves the edited text back to its own endpoint', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const prefs = await screen.findByRole('textbox', { name: /preferences/i }) as HTMLTextAreaElement
    await waitFor(() => expect(prefs.value).toBe('zzq-prefs-body'))

    fireEvent.change(prefs, { target: { value: 'zzq-prefs-edited' } })
    await userEvent.click(saveIn(/^Preferences$/))
    await waitFor(() => expect(api.saveMemoryPreferences).toHaveBeenCalledWith('zzq-prefs-edited', undefined))

    const projects = screen.getByRole('textbox', { name: /projects/i }) as HTMLTextAreaElement
    fireEvent.change(projects, { target: { value: 'zzq-projects-edited' } })
    await userEvent.click(saveIn(/^Projects$/))
    await waitFor(() => expect(api.saveMemoryProjects).toHaveBeenCalledWith('zzq-projects-edited', undefined))

    const history = screen.getByRole('textbox', { name: /daily history/i }) as HTMLTextAreaElement
    fireEvent.change(history, { target: { value: 'zzq-history-edited' } })
    await userEvent.click(saveIn(/Daily History/i))
    await waitFor(() => expect(api.saveMemoryHistory).toHaveBeenCalledWith('zzq-history-edited', undefined))
  })

  it('re-reads every store when the parent bumps the refresh trigger', async () => {
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(api.memoryPreferences).toHaveBeenCalled())
    const before = api.memoryPreferences.mock.calls.length
    const lessonsBefore = api.lessons.mock.calls.length
    rerender(<MemoryTab refreshTrigger={1} />)
    await waitFor(() =>
      expect(api.memoryPreferences.mock.calls.length).toBeGreaterThan(before))
    expect(api.lessons.mock.calls.length).toBeGreaterThan(lessonsBefore)
  })

  it('tolerates an empty payload rather than rendering undefined', async () => {
    api.memoryPreferences.mockResolvedValue({})
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const prefs = await screen.findByRole('textbox', { name: /preferences/i }) as HTMLTextAreaElement
    await waitFor(() => expect(prefs.value).toBe(''))
  })
})

describe('MemoryTab — lessons', () => {
  it('lists the stored lessons, newest first by default', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const rules = lessonRows().map((tr) => tr.querySelector('td:first-child'))
      .map((td) => td.textContent)
    expect(rules).toEqual(['zzq-rule-beta', 'zzq-rule-alpha'])
  })

  it('re-sorts on a header click', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    await userEvent.click(screen.getByText(/^Rule$/))
    const rules = lessonRows().map((tr) => tr.querySelector('td:first-child'))
      .map((td) => td.textContent)
    expect(rules).toEqual(['zzq-rule-alpha', 'zzq-rule-beta'])

    await userEvent.click(screen.getByText(/^Category$/))
    const cats = lessonRows().map((tr) => tr.querySelector('td:nth-child(2)'))
      .map((td) => td.textContent)
    expect(cats).toEqual(['knowledge', 'tool'])
  })

  it('adds a lesson with the chosen category, then re-reads the list', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const input = screen.getByPlaceholderText(/Rule/) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzq-new-rule' } })
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))

    await waitFor(() => expect(api.createLesson).toHaveBeenCalledWith('zzq-new-rule', 'knowledge'))
    await waitFor(() => expect(api.lessons.mock.calls.length).toBeGreaterThan(reads))
    expect(input.value).toBe('')
  })

  it('keeps a refused lesson editable and reports the backend reason', async () => {
    api.createLesson.mockResolvedValue({
      ok: false, outcome: 'refused', reason: 'blocked_not_clause',
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const input = screen.getByPlaceholderText(/Rule/) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzq-refused-rule' } })
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Lesson not saved.*blocked_not_clause.*Edit it and try again/i,
    )
    expect(input.value).toBe('zzq-refused-rule')
    expect(api.lessons).toHaveBeenCalledTimes(reads)
  })

  it('keeps a deduped lesson editable instead of implying it was added', async () => {
    api.createLesson.mockResolvedValue({
      ok: false, outcome: 'deduped', reason: 'substring',
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const input = screen.getByPlaceholderText(/Rule/) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzq-covered-rule' } })
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      /existing lesson already covers this.*substring/i,
    )
    expect(input.value).toBe('zzq-covered-rule')
    expect(api.lessons).toHaveBeenCalledTimes(reads)
  })

  it('clears an unchanged resubmission but says it was already stored', async () => {
    api.createLesson.mockResolvedValue({
      ok: true, outcome: 'unchanged', reason: 'identical',
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const input = screen.getByPlaceholderText(/Rule/) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzq-existing-rule' } })
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))

    expect(await screen.findByRole('status')).toHaveTextContent(/already stored/i)
    expect(input.value).toBe('')
  })

  it('refuses to add an empty rule', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    expect(api.createLesson).not.toHaveBeenCalled()
  })

  it('deletes the lesson its row names, then re-reads the list', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const row = screen.getByText('zzq-rule-beta').closest('tr') as HTMLElement
    const del = Array.from(row.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    await userEvent.click(del)

    // The global row's own selector ("") rides along: a lesson's identity is
    // (rule, repo_scope), so a bare rule would delete every scope's row. The
    // fixture rows name no JSONL tier, so none is forwarded.
    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-rule-beta', '', { scope: undefined, workspace: undefined, exact: true }))
    await waitFor(() => expect(api.lessons.mock.calls.length).toBeGreaterThan(reads))
  })

  it('tells two same-rule rows apart by scope and deletes only the clicked one (#10651)', async () => {
    api.lessons.mockResolvedValue({
      lessons: [
        { rule: 'zzq-same-rule', category: 'tool', ts: '2026-01-02T00:00:00Z', repo_scope: '' },
        { rule: 'zzq-same-rule', category: 'tool', ts: '2026-01-02T00:00:00Z', repo_scope: 'src/pkg' },
        // Stored scope present but unusable: the list reports null, and the
        // only delete that reaches such a row is the unselective one.
        { rule: 'zzq-broken-rule', category: 'tool', ts: '2026-01-03T00:00:00Z', repo_scope: null },
      ],
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('src/pkg')
    // Two rows share the rule; the Scope column is what tells them apart, and
    // each of the three selector values reads differently.
    const sameRule = screen.getAllByText('zzq-same-rule').map((td) => td.closest('tr') as HTMLElement)
    expect(sameRule).toHaveLength(2)
    expect(screen.getByRole('columnheader', { name: /Scope/ })).toBeInTheDocument()
    const scoped = sameRule.find((tr) => tr.textContent?.includes('src/pkg')) as HTMLElement
    const global = sameRule.find((tr) => !tr.textContent?.includes('src/pkg')) as HTMLElement
    expect(global).toHaveTextContent(/Global/)
    const broken = screen.getByText('zzq-broken-rule').closest('tr') as HTMLElement
    expect(broken).toHaveTextContent(/Unusable scope/)
    const deleteIn = (tr: HTMLElement) => Array.from(tr.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    const dialogTitle = /Delete this lesson in every scope\?/

    // A scoped or global row deletes without a prompt: its selector reaches
    // exactly that row.
    await userEvent.click(deleteIn(scoped))
    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-same-rule', 'src/pkg', { scope: undefined, workspace: undefined, exact: true }))
    expect(api.deleteLesson).not.toHaveBeenCalledWith('zzq-same-rule', '', { scope: undefined, workspace: undefined, exact: true })
    await userEvent.click(deleteIn(global))
    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-same-rule', '', { scope: undefined, workspace: undefined, exact: true }))
    expect(screen.queryByText(dialogTitle)).not.toBeInTheDocument()

    // The null row's delete is the unselective one, so it asks first through
    // the shared dialog, whose confirm button restates the act. Cancel sends
    // nothing; confirming sends the null through (the client drops the key).
    await userEvent.click(deleteIn(broken))
    expect(await screen.findByText(dialogTitle)).toBeInTheDocument()
    expect(screen.getByText(/every lesson with exactly this text will be removed/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))
    await waitFor(() => expect(screen.queryByText(dialogTitle)).not.toBeInTheDocument())
    expect(api.deleteLesson).not.toHaveBeenCalledWith('zzq-broken-rule', null, { scope: undefined, workspace: undefined, exact: true })

    await userEvent.click(deleteIn(broken))
    await userEvent.click(await screen.findByRole('button', { name: /^Delete in every scope$/ }))
    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-broken-rule', null, { scope: undefined, workspace: undefined, exact: true }))
  })

  it('reports a rejected delete beside the table instead of swallowing it (#10651)', async () => {
    api.deleteLesson.mockRejectedValueOnce(new Error('zzq-delete-refused'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const row = screen.getByText('zzq-rule-beta').closest('tr') as HTMLElement
    const del = Array.from(row.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    await userEvent.click(del)

    const notice = await screen.findByText('zzq-delete-refused')
    expect(notice).toBeInTheDocument()
    expect(screen.getByText(/Could not delete the lesson/)).toBeInTheDocument()
    // The row is still there and the list was not re-read as if it had gone.
    expect(screen.getByText('zzq-rule-beta')).toBeInTheDocument()
    expect(api.lessons.mock.calls.length).toBe(reads)

    // Dismissable, and a later successful delete clears it on its own.
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByText('zzq-delete-refused')).not.toBeInTheDocument())
  })

  it('reports a failed re-read after a successful delete (#10651)', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    api.lessons.mockRejectedValueOnce(new Error('zzq-refresh-refused'))
    const row = screen.getByText('zzq-rule-beta').closest('tr') as HTMLElement
    const del = Array.from(row.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    await userEvent.click(del)

    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-rule-beta', '', { scope: undefined, workspace: undefined, exact: true }))
    expect(await screen.findByText('zzq-refresh-refused')).toBeInTheDocument()
    // Titled for what actually failed: the row is gone, the list is stale.
    expect(screen.getByText(/Lesson deleted, but the list could not be refreshed/)).toBeInTheDocument()
    expect(screen.queryByText(/Could not delete the lesson/)).not.toBeInTheDocument()
  })

  it('sends a workspace-tier row back to its own file on delete (#10651)', async () => {
    api.lessons.mockResolvedValue({
      lessons: [
        { rule: 'zzq-tier-rule', category: 'tool', ts: '2026-01-02T00:00:00Z', repo_scope: '', scope: 'global' },
        { rule: 'zzq-tier-rule', category: 'tool', ts: '2026-01-02T00:00:00Z', repo_scope: '', scope: 'workspace', workspace: 'ws-1' },
      ],
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const rows = (await screen.findAllByText('zzq-tier-rule')).map((td) => td.closest('tr') as HTMLElement)
    expect(rows).toHaveLength(2)
    // The tier shows in the Scope cell, so the two same-text rows read apart.
    expect(rows[0]).not.toHaveTextContent(/Workspace ws-1/)
    expect(rows[1]).toHaveTextContent(/Workspace ws-1/)
    const deleteIn = (tr: HTMLElement) => Array.from(tr.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    await userEvent.click(deleteIn(rows[1]))
    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-tier-rule', '', { scope: 'workspace', workspace: 'ws-1', exact: true }))
    await userEvent.click(deleteIn(rows[0]))
    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-tier-rule', '', { scope: 'global', workspace: undefined, exact: true }))
  })

  it('says so when the delete matched no stored row (#10651)', async () => {
    api.deleteLesson.mockResolvedValueOnce({ ok: false })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const row = screen.getByText('zzq-rule-beta').closest('tr') as HTMLElement
    const del = Array.from(row.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    await userEvent.click(del)
    expect(await screen.findByText(/No stored lesson matched this row/)).toBeInTheDocument()
    expect(screen.getByText(/Could not delete the lesson/)).toBeInTheDocument()
  })

  it('shows an empty state rather than a bare table', async () => {
    api.lessons.mockResolvedValue({ lessons: [] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    expect(await screen.findByText(/No lessons yet/i)).toBeInTheDocument()
  })

  it('tolerates a response with no lessons key', async () => {
    api.lessons.mockResolvedValue({})
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    expect(await screen.findByText(/No lessons yet/i)).toBeInTheDocument()
  })
})

describe('MemoryTab — manual consolidation', () => {
  it('consolidates every known session and reports the count', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    await waitFor(() => expect(api.consolidateMemory).toHaveBeenCalledTimes(2))
    expect(api.consolidateMemory).toHaveBeenCalledWith('zzq-s1', true)
    expect((await screen.findByText(/Consolidated/)).textContent).toContain('Consolidated 2 sessions')
  })

  it('reports a partial failure instead of claiming success', async () => {
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq-consolidate-failed'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    // A failed request is an error, so the failed tally renders through
    // ErrorNotice (role="alert"): the tally as its lead, a plain-words
    // localized message that says what happened AND what to do (press the
    // button again), the failed session named in the footer over the failed
    // request's own string as secondary detail in the mono font (and the
    // journal lookup key behind `report`), no hand-off (the tab holds unsaved
    // drafts), persistent.
    const notice = await screen.findByRole('alert')
    expect(notice.textContent).toContain('1/2 sessions (1 failed)')
    expect(notice.textContent).toContain('The server rejected a summarize request')
    expect(notice.textContent).toContain('press Summarize now again')
    expect(within(notice).getByTestId('consolidate-failed-session').textContent).toBe('1 failed session: zzq-s2')
    const detail = within(notice).getByTestId('consolidate-failed-detail')
    expect(detail.textContent).toBe('zzq-consolidate-failed')
    expect(detail.className).toContain('font-mono')
    // The raw reply is labelled as the server's own words -- one localized
    // line, the reply inside it in mono: the backend still says "consolidat*"
    // where every label here says "Summarize", and without the label the
    // reader doubted the line concerned the button they pressed.
    expect(within(notice).getByTestId('consolidate-failed-reply').textContent).toBe("Server's reply: zzq-consolidate-failed")
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.queryByText(/Summarized/, { selector: 'span' })).toBeNull()
  })

  it('names the failed session by its title and opens it on click', async () => {
    // "chat-103" was a dead end: the reader could not find that chat in the
    // sidebar, which names sessions by title, nor tell whether the text did
    // anything. The line names the session the way the sidebar does and IS the
    // way to it -- through the tab's leave guard, since it holds drafts.
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1', title: 'Release notes' }, { key: 'zzq-s2', title: 'Draft reply to Mudhar' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq-consolidate-failed'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const notice = await screen.findByRole('alert')
    const line = within(notice).getByTestId('consolidate-failed-session')
    expect(line.textContent).toBe('1 failed session: Draft reply to Mudhar')
    expect(line.textContent).not.toContain('zzq-s2')
    const link = within(line).getByRole('button', { name: 'Draft reply to Mudhar' })
    expect(link.className).toContain('underline')
    await userEvent.click(link)
    expect(mockNavigate).toHaveBeenCalledWith('/chat?sid=zzq-s2')
    // Opening the session is not a dismissal: the notice stays for the reader
    // who comes back.
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('names the first failed session and counts the rest', async () => {
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }, { key: 'zzq-s3' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq-first-failure'))
      .mockRejectedValueOnce(new Error('zzq-second-failure'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    // The reader was "stuck" on which session failed: the footer names the
    // first failed request's session key, says how many failed in all, and
    // shows that first request's reply -- the same request, not a mix.
    const notice = await screen.findByRole('alert')
    expect(notice.textContent).toContain('1/3 sessions (2 failed)')
    expect(within(notice).getByTestId('consolidate-failed-session').textContent).toBe('2 failed sessions, the first: zzq-s2')
    expect(within(notice).getByTestId('consolidate-failed-detail').textContent).toBe('zzq-first-failure')
  })

  it('says what the button does, under it, before it is ever pressed', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByRole('button', { name: /Summarize now/i })
    // Load-bearing for the blind reader: hesitant over "summarize WHAT" without
    // it, willing to press with it. Static text under the row, not part of the
    // button's name.
    const help = screen.getByTestId('summarize-now-help')
    expect(help.textContent).toBe(
      'Summarize now writes the facts, decisions and lessons from your conversations into memory; the conversations themselves stay untouched.',
    )
    expect(help.closest('button')).toBeNull()
  })

  it('keeps the failure notice until it is dismissed', async () => {
    vi.useFakeTimers()
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq-consolidate-failed'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: /Summarize now/i }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.getByRole('alert')).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('alert')).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  /** The route's refusal for a Temporary or Incognito target, as `j()` rejects it:
   *  an ApiError-shaped rejection whose raw body carries the backend `code`. A body
   *  without `mode` is the shape an older backend answered; the tally then keeps
   *  the either/or wording. */
  const restrictedTarget = (mode?: 'temporary' | 'incognito') => Object.assign(
    new Error(`Consolidation is not allowed for a ${mode ?? 'temporary'} session: it leaves no durable memory.`),
    { status: 403, body: JSON.stringify({ error: `Consolidation is not allowed for a ${mode ?? 'temporary'} session: it leaves no durable memory.`, code: 'restricted_target_session', ...(mode ? { mode } : {}) }) },
  )

  it('names the one mode every skipped session was in', async () => {
    // "skipped as temporary or incognito" left the reader unsure whether that
    // was two kinds of private chat or one thing with two names, and which
    // theirs was. The route's body names the mode; when every skip shares it,
    // the tally says so.
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget('incognito'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const msg = await screen.findByText(/Summarized/)
    expect(msg.textContent).toContain('1/2 sessions (1 skipped: incognito session)')
    expect(msg.textContent).not.toContain('temporary or incognito')
  })

  it('pluralizes the named mode and keeps it beside a genuine failure', async () => {
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }, { key: 'zzq-s3' }, { key: 'zzq-s4' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget('temporary'))
      .mockRejectedValueOnce(restrictedTarget('temporary'))
      .mockRejectedValueOnce(Object.assign(new Error('zzq-consolidate-failed'), { status: 500, body: '{"error": "boom"}' }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const notice = await screen.findByRole('alert')
    expect(notice.textContent).toContain('1/4 sessions (1 failed)')
    // The skip is the tally's other half and renders BESIDE the notice, in the
    // success tone, as the tally it is when it stands alone -- never inside the
    // danger box, where the reader could not tell whether it was part of the
    // problem or a separate reassuring note.
    const skip = screen.getByTestId('consolidate-msg')
    expect(skip.textContent).toContain('2 skipped: temporary sessions')
    expect(skip.className).toContain('text-ok')
    expect(skip.className).not.toContain('text-danger')
    expect(notice.contains(skip)).toBe(false)
    expect(notice.textContent).not.toContain('skipped')
  })

  it('dismissing the failure notice takes the skip tally beside it away too', async () => {
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }, { key: 'zzq-s3' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget())
      .mockRejectedValueOnce(new Error('zzq-consolidate-failed'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const notice = await screen.findByRole('alert')
    expect(screen.getByTestId('consolidate-msg').textContent).toContain('1 private session skipped')
    // The two report one press: the skip beside a failure is not on the
    // four-second timer (it would vanish while the failure stays), so the
    // notice's dismiss clears both.
    await userEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('consolidate-msg')).toBeNull()
  })

  it('counts each mode when the skipped sessions were in different modes', async () => {
    // Mixed modes cannot name one. The lane's reader could not tell what made a
    // session "temporary" versus "incognito", or which of theirs were which, so
    // the category leads and the parenthetical counts each mode (the lane's
    // wording: "2 private sessions skipped (1 temporary, 1 incognito)").
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }, { key: 'zzq-s3' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget('temporary'))
      .mockRejectedValueOnce(restrictedTarget('incognito'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const msg = await screen.findByText(/Summarized/)
    expect(msg.textContent).toContain('1/3 sessions — 2 private sessions skipped (1 temporary, 1 incognito)')
    expect(msg.textContent).not.toContain('temporary or incognito')
  })

  it('counts each mode with its own number', async () => {
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }, { key: 'zzq-s3' }, { key: 'zzq-s4' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget('incognito'))
      .mockRejectedValueOnce(restrictedTarget('temporary'))
      .mockRejectedValueOnce(restrictedTarget('incognito'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const msg = await screen.findByText(/Summarized/)
    expect(msg.textContent).toContain('1/4 sessions — 3 private sessions skipped (1 temporary, 2 incognito)')
  })

  it('counts a restricted_target_session refusal as skipped, not failed', async () => {
    // `api.sessions` lists the temporary session too; the route refuses it by
    // design, so the press must read as ok with the skip named, never as a
    // failure on every "Summarize now". The body names no mode (the shape an
    // older backend answered), so the parenthetical keeps the either/or wording
    // -- per-mode counts need every skip's mode.
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget())
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const msg = await screen.findByText(/Summarized/)
    expect(msg.textContent).toContain('1/2 sessions — 1 private session skipped (temporary or incognito)')
    expect(msg.textContent).not.toContain('failed')
    // The success tone, and no error surface for a refusal the user asked for.
    expect((msg.closest('span') as HTMLElement).className).toContain('text-ok')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps a genuine failure failed beside a skipped refusal', async () => {
    api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }, { key: 'zzq-s3' }] })
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(restrictedTarget())
      .mockRejectedValueOnce(Object.assign(new Error('zzq-consolidate-failed'), { status: 500, body: '{"error": "boom"}' }))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const notice = await screen.findByRole('alert')
    expect(notice.textContent).toContain('1/3 sessions (1 failed)')
    expect(screen.getByTestId('consolidate-msg').textContent).toContain('1 private session skipped (temporary or incognito)')
    // The refused one is a skip, not a failure: one failed, one skipped, one summarized.
    expect(within(notice).getByTestId('consolidate-failed-session').textContent).toBe('1 failed session: zzq-s3')
    expect(within(notice).getByTestId('consolidate-failed-detail').textContent).toBe('zzq-consolidate-failed')
  })

  it('says there is nothing to summarize when no session exists', async () => {
    api.sessions.mockResolvedValue({ sessions: [] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))
    expect(await screen.findByText(/No sessions to summarize/i)).toBeInTheDocument()
    expect(api.consolidateMemory).not.toHaveBeenCalled()
  })

  it('reports a session list the server would not give as a failure, not as no sessions', async () => {
    // A gateway that is down or answers 500 used to fall through `sessions: []`
    // into "start a chat first" with no error surface at all, while a failed
    // per-session request got the full notice. The list is the press's first
    // request: its failure is the press's failure.
    api.sessions.mockRejectedValueOnce(new Error('zzq-list-down'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const notice = await screen.findByRole('alert')
    expect(notice.textContent).toContain('Could not list the sessions to summarize')
    expect(notice.textContent).toContain('so nothing was summarized')
    expect(notice.textContent).toContain('press Summarize now again')
    expect(within(notice).getByTestId('consolidate-failed-detail').textContent).toBe('zzq-list-down')
    expect(within(notice).getByTestId('consolidate-failed-reply').textContent).toBe("Server's reply: zzq-list-down")
    // No session to name: the list itself is what failed.
    expect(within(notice).queryByTestId('consolidate-failed-session')).toBeNull()
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.queryByText(/No sessions to summarize/i)).toBeNull()
    expect(screen.queryByText(/Summarized/, { selector: 'span' })).toBeNull()
    expect(api.consolidateMemory).not.toHaveBeenCalled()
    // Dismissible like every failure; the button is usable again.
    expect(screen.getByRole('button', { name: /Summarize now/i })).toBeEnabled()
  })

  it('clears the outcome message on its own timer', async () => {
    vi.useFakeTimers()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: /Summarize now/i }))
    await act(async () => {})
    expect(screen.getByText(/Consolidated/)).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.queryByText(/Consolidated/)).toBeNull()
  })

  it('keeps a second press\'s tally for its own four seconds', async () => {
    // The first press's clear timer must not wipe the tally of a press that
    // landed within four seconds of it.
    vi.useFakeTimers()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: /Summarize now/i }))
    await act(async () => {})
    await act(async () => { vi.advanceTimersByTime(3000) })
    fireEvent.click(screen.getByRole('button', { name: /Summarize now/i }))
    await act(async () => {})
    expect(screen.getByText(/Consolidated/)).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(1500) })
    expect(screen.getByText(/Consolidated/)).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(3000) })
    expect(screen.queryByText(/Consolidated/)).toBeNull()
  })
})
