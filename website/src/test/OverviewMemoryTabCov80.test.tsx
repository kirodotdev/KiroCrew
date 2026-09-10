import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, fireEvent } from '@testing-library/react'
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
// Both cards own their own queries and their own tests; here they are seams that
// report the vector/migration state this tab branches on.
vi.mock('../pages/overview/VectorMemoryCard', () => ({
  default: ({ diagnosticsOnly }: { diagnosticsOnly?: boolean }) => <div data-testid="vector-card" data-diagnostics-only={diagnosticsOnly ? 'true' : 'false'} />,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div data-testid="embed-card" /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div data-testid="records-editor" /> }))

const MemoryTab = (await import('../pages/overview/MemoryTab')).default

const LESSONS = [
  { rule: 'zzq-rule-beta', category: 'tool', ts: '2026-01-02T00:00:00Z' },
  { rule: 'zzq-rule-alpha', category: 'knowledge', ts: '2026-01-01T00:00:00Z' },
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

    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-rule-beta'))
    await waitFor(() => expect(api.lessons.mock.calls.length).toBeGreaterThan(reads))
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
    expect(await screen.findByText(/Consolidated/)).toBeInTheDocument()
  })

  it('reports a partial failure instead of claiming success', async () => {
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq-consolidate-failed'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const msg = await screen.findByText(/failed/i)
    expect(msg).toBeInTheDocument()
    // The warning tone, not the success one.
    expect((msg.closest('span') as HTMLElement).className).toContain('text-danger')
  })

  it('says there is nothing to consolidate when no session exists', async () => {
    api.sessions.mockResolvedValue({ sessions: [] })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))
    expect(await screen.findByText(/No sessions to consolidate/i)).toBeInTheDocument()
    expect(api.consolidateMemory).not.toHaveBeenCalled()
  })

  it('treats an unreadable session list as nothing to do rather than crashing', async () => {
    api.sessions.mockRejectedValue(new Error('zzq-sessions-unreachable'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))
    expect(await screen.findByText(/No sessions to consolidate/i)).toBeInTheDocument()
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
})
