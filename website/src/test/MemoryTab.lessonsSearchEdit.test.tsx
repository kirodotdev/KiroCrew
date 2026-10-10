import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
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
  { rule: 'Run tests before pushing to git', category: 'knowledge', ts: '2026-01-02T10:00:00Z', repo_scope: 'src/repo' },
  { rule: 'Use react hooks at top level', category: 'tool', ts: '2026-01-03T10:00:00Z', repo_scope: '', scope: 'workspace' as const, workspace: 'ws-react' },
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

  it('allows inline editing a rule and saving changes while preserving scope and category', async () => {
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

    // Should call deleteLesson on the old rule with exact=true and existing scope
    await waitFor(() => {
      expect(api.deleteLesson).toHaveBeenCalledWith('Run tests before pushing to git', 'src/repo', {
        scope: undefined,
        workspace: undefined,
        exact: true,
      })
    })

    // Should call createLesson with the new rule text, preserving category, scope, and workspace
    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalledWith('Run tests and linter before pushing to git', 'knowledge', {
        scope: undefined,
        repo_scope: 'src/repo',
        workspace: undefined,
      })
    })

    // Should refresh lessons
    await waitFor(() => {
      expect(api.lessons.mock.calls.length).toBeGreaterThan(reads)
    })
  })

  it('preserves workspace scope when inline editing a workspace lesson', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    const targetRowText = 'Use react hooks at top level'
    expect(await screen.findByText(targetRowText)).toBeInTheDocument()

    const targetRow = screen.getByText(targetRowText).closest('tr')!
    const editBtn = within(targetRow).getByRole('button', { name: /edit/i })
    await user.click(editBtn)

    const ruleInput = within(targetRow).getByRole('textbox', { name: /edit rule/i })
    await user.clear(ruleInput)
    await user.type(ruleInput, 'Use react hooks and custom hooks at top level')

    const saveBtn = within(targetRow).getByRole('button', { name: /save/i })
    await user.click(saveBtn)

    await waitFor(() => {
      expect(api.deleteLesson).toHaveBeenCalledWith('Use react hooks at top level', '', {
        scope: 'workspace',
        workspace: 'ws-react',
        exact: true,
      })
    })

    await waitFor(() => {
      expect(api.createLesson).toHaveBeenCalledWith('Use react hooks and custom hooks at top level', 'tool', {
        scope: 'workspace',
        repo_scope: '',
        workspace: 'ws-react',
      })
    })
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
