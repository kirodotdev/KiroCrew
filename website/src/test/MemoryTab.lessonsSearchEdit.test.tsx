import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, within, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import userEvent from '@testing-library/user-event'

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

vi.mock('../pages/overview/VectorMemoryCard', () => ({
  default: () => <div data-testid="vector-card" />,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div data-testid="embed-card" /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div data-testid="records-editor" /> }))
vi.mock('../pages/overview/MemoryStoreCard', () => ({
  default: () => <div data-testid="memory-store-card" />,
  MEMORY_QUERY_PREFIXES: [],
  MemoryScopeNotice: () => null,
  useMemoryStores: () => ({
    data: { stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }], active: 'default' },
    isPending: false,
    error: null,
  }),
}))
vi.mock('../pages/overview/MemoryDocCard', () => ({ default: () => <div data-testid="memory-doc-card" /> }))
vi.mock('../pages/overview/MemoryCarveCard', () => ({ default: () => <div data-testid="memory-carve-card" /> }))
vi.mock('../pages/overview/MemoryRetiredCard', () => ({ default: () => <div data-testid="memory-retired-card" /> }))
vi.mock('../pages/overview/MemoryBackupsCard', () => ({ default: () => <div data-testid="memory-backups-card" /> }))

const MemoryTab = (await import('../pages/overview/MemoryTab')).default

const MOCK_LESSONS = [
  { rule: 'Always use tabs, not spaces', category: 'preference', ts: '2026-01-01T10:00:00Z', repo_scope: '' },
  { rule: 'Run tests before pushing to git', category: 'knowledge', ts: '2026-01-02T10:00:00Z', repo_scope: 'src/repo', applies: 'standing' },
  { rule: 'Use react hooks at top level', category: 'tool', ts: '2026-01-03T10:00:00Z', repo_scope: '', scope: 'workspace' as const, workspace: 'ws-react', negative: 'do not use class components' },
  { rule: 'Prefer tabs — NOT: spaces', bare_rule: 'Prefer tabs', negative: 'spaces', category: 'preference', ts: '2026-01-05T10:00:00Z', repo_scope: '' },
  { rule: 'Damaged unusable rule', category: 'knowledge', ts: '2026-01-04T10:00:00Z', repo_scope: null },
]

describe('MemoryTab lessons search and inline edit (#17982)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.lessons.mockResolvedValue({ lessons: MOCK_LESSONS })
    api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
    api.createLesson.mockResolvedValue({ ok: true, outcome: 'inserted', reason: '' })
    api.deleteLesson.mockResolvedValue({ ok: true })
    api.memoryStores.mockResolvedValue({
      stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }],
      active: 'default',
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders search input and filters rows by rule substring match', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    expect(await screen.findByText('Always use tabs, not spaces')).toBeInTheDocument()
    expect(screen.getByText('Run tests before pushing to git')).toBeInTheDocument()
    expect(screen.getByText('Use react hooks at top level')).toBeInTheDocument()

    const searchInput = screen.getByRole('textbox', { name: /search lessons/i })
    expect(searchInput).toBeInTheDocument()

    // Filter by "tabs"
    await user.type(searchInput, 'tabs')
    expect(screen.getByText('Always use tabs, not spaces')).toBeInTheDocument()
    expect(screen.queryByText('Run tests before pushing to git')).not.toBeInTheDocument()
    expect(screen.queryByText('Use react hooks at top level')).not.toBeInTheDocument()

    // Clear search filter
    await user.clear(searchInput)
    expect(screen.getByText('Always use tabs, not spaces')).toBeInTheDocument()
    expect(screen.getByText('Run tests before pushing to git')).toBeInTheDocument()
    expect(screen.getByText('Use react hooks at top level')).toBeInTheDocument()
  })

  it('shows FilteredEmpty when search has no matching rows and allows clearing', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    expect(await screen.findByText('Always use tabs, not spaces')).toBeInTheDocument()

    const searchInput = screen.getByRole('textbox', { name: /search lessons/i })
    await user.type(searchInput, 'nonexistent query')

    expect(screen.queryByText('Always use tabs, not spaces')).not.toBeInTheDocument()
    const clearBtn = screen.getByTestId('filtered-empty-clear')
    expect(clearBtn).toBeInTheDocument()

    await user.click(clearBtn)
    expect(await screen.findByText('Always use tabs, not spaces')).toBeInTheDocument()
  })

  it('allows inline editing a rule and saving changes while preserving scope, category, and applies', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Run tests before pushing to git'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()
    const reads = api.lessons.mock.calls.length

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    // Row is now in edit mode
    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    expect(ruleInput).toHaveValue(targetRowText)

    // Edit the text
    await user.clear(ruleInput)
    await user.type(ruleInput, 'Run tests and linter before pushing to git')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    // Should call createLesson first with the new rule text, preserving category, scope, and applies
    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalledWith('Run tests and linter before pushing to git', 'knowledge', {
        scope: undefined,
        repo_scope: 'src/repo',
        workspace: undefined,
        applies: 'standing',
        replaces: 'Run tests before pushing to git',
      })
    })

    // Should call deleteLesson on the old rule only after createLesson succeeds
    await waitFor(() => {
      expect(api.deleteLesson).toHaveBeenCalledWith('Run tests before pushing to git', 'src/repo', {
        scope: undefined,
        workspace: undefined,
        exact: true,
      })
    })

    // Should refresh lessons
    await waitFor(() => {
      expect(api.lessons.mock.calls.length).toBeGreaterThan(reads)
    })
  })

  it('offers edit button for JSONL-tier rows (scope present) and preserves stored attributes including negative', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const jsonlRowText = 'Use react hooks at top level'
    expect(await screen.findByText(jsonlRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(jsonlRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    expect(editBtn).toBeInTheDocument()
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.clear(ruleInput)
    await user.type(ruleInput, 'Use react hooks only at top level')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalledWith('Use react hooks only at top level', 'tool', {
        scope: 'workspace',
        repo_scope: '',
        workspace: 'ws-react',
        applies: undefined,
        negative: 'do not use class components',
        replaces: 'Use react hooks at top level',
      })
    })
  })

  it('safely keeps original lesson without calling deleteLesson when createLesson reports deduped', async () => {
    const user = userEvent.setup()
    api.createLesson.mockResolvedValueOnce({
      ok: false,
      outcome: 'deduped',
      reason: 'substring_covered',
      winner: 'Run tests before pushing to git',
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Run tests before pushing to git'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.clear(ruleInput)
    await user.type(ruleInput, 'Run tests')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalled()
    })

    // deleteLesson MUST NOT be called! Original is preserved.
    expect(api.deleteLesson).not.toHaveBeenCalled()
    expect(await screen.findByText(/substring_covered/i)).toBeInTheDocument()
  })

  it('populates bare rule when editing a vector lesson with a rendered NOT-clause', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const rowText = 'Prefer tabs — NOT: spaces'
    expect(await screen.findByText(rowText)).toBeInTheDocument()

    const targetRow = screen.getByText(rowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    // The editor must receive the bare rule ('Prefer tabs'), not the composite text with NOT-clause
    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    expect(ruleInput).toHaveValue('Prefer tabs')

    await user.clear(ruleInput)
    await user.type(ruleInput, 'Always prefer tabs')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalledWith('Always prefer tabs', 'preference', {
        scope: undefined,
        repo_scope: '',
        workspace: undefined,
        applies: undefined,
        negative: 'spaces',
        replaces: 'Prefer tabs',
      })
    })
  })

  it('skips explicit delete when createLesson supersedes the old rule on vector store', async () => {
    const user = userEvent.setup()
    api.createLesson.mockResolvedValueOnce({
      ok: true,
      outcome: 'inserted',
      reason: '',
      superseded: ['Run tests before pushing to git'],
    })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Run tests before pushing to git'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.clear(ruleInput)
    await user.type(ruleInput, 'Run tests and linter before pushing to git')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalled()
    })

    // deleteLesson should be skipped because createLesson superseded it
    expect(api.deleteLesson).not.toHaveBeenCalled()
  })

  it('does not trigger save on Enter during IME composition', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Run tests before pushing to git'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })

    // 1. Enter during active composition
    fireEvent.compositionStart(ruleInput)
    fireEvent.keyDown(ruleInput, { key: 'Enter' })

    expect(api.createLesson).not.toHaveBeenCalled()
    expect(ruleInput).toBeInTheDocument()

    // 2. Enter with keyCode 229
    fireEvent.compositionEnd(ruleInput)
    fireEvent.keyDown(ruleInput, { key: 'Enter', keyCode: 229 })

    expect(api.createLesson).not.toHaveBeenCalled()
    expect(ruleInput).toBeInTheDocument()

    // 3. Escape during active composition does not cancel edit
    fireEvent.compositionStart(ruleInput)
    fireEvent.keyDown(ruleInput, { key: 'Escape' })
    expect(ruleInput).toBeInTheDocument()
  })

  it('shows edit-specific refresh failure message when list reload fails after save', async () => {
    const user = userEvent.setup()
    api.createLesson.mockResolvedValueOnce({ ok: true, outcome: 'inserted', reason: '' })
    api.deleteLesson.mockResolvedValueOnce({ ok: true })
    let loadCount = 0
    api.lessons.mockImplementation(async () => {
      loadCount++
      if (loadCount > 2) {
        throw new Error('Network offline')
      }
      return { lessons: MOCK_LESSONS }
    })

    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Run tests before pushing to git'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.type(ruleInput, ' and check diff')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    expect(await screen.findByText(/Lesson updated, but the list could not be refreshed/i)).toBeInTheDocument()
  })

  it('leaves the original lesson in place without deleting when createLesson is refused or fails', async () => {
    const user = userEvent.setup()
    api.createLesson.mockResolvedValueOnce({ ok: false, outcome: 'refused', reason: 'contradicts existing rule' })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Always use tabs, not spaces'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.clear(ruleInput)
    await user.type(ruleInput, 'Always use 4 spaces')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalled()
    })

    // deleteLesson MUST NOT be called!
    expect(api.deleteLesson).not.toHaveBeenCalled()

    // Error notice shown, editor stays open
    expect(await screen.findByText(/contradicts existing rule/i)).toBeInTheDocument()
    expect(ruleInput).toBeInTheDocument()
  })

  it('does not offer edit button for unusable-scope rows (repo_scope: null)', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const unusableRowText = 'Damaged unusable rule'
    expect(await screen.findByText(unusableRowText)).toBeInTheDocument()

    const row = screen.getByText(unusableRowText).closest('tr')!
    expect(within(row).queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /delete/i })).toBeInTheDocument()
  })

  it('cancels inline editing when clicking cancel button or pressing Escape', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Always use tabs, not spaces'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.type(ruleInput, ' extra modified text')

    const cancelBtn = within(targetRow).getByRole('button', { name: /cancel/i })
    await user.click(cancelBtn)

    // Back in view mode, no API calls made
    expect(screen.getByText(targetRowText)).toBeInTheDocument()
    expect(api.deleteLesson).not.toHaveBeenCalled()
    expect(api.createLesson).not.toHaveBeenCalled()
  })
})
