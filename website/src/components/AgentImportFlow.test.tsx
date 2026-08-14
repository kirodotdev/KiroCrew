import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AgentImportApplyResponse,
  AgentImportScanResponse,
} from '../api/client'
import { api } from '../api/client'
import { renderWithProviders } from '../test/helpers'
import AgentImportFlow from './AgentImportFlow'

vi.mock('../api/client', () => ({
  api: {
    onboardingImportScan: vi.fn(),
    onboardingImportApply: vi.fn(),
    onboardingImportState: vi.fn(),
  },
}))

const SCAN_RESPONSE: AgentImportScanResponse = {
  sources: [
    {
      id: 'claude_code',
      name: 'Claude Code',
      detected: true,
      detail: '~/.claude',
      categories: [
        { id: 'skills', label: 'Skills', count: 4, description: 'Reusable workflows' },
        { id: 'mcp_servers', label: 'MCP servers', count: 2 },
        { id: 'hooks', label: 'Hooks', count: 3 },
        { id: 'raw_instructions', label: 'Raw instructions', count: 1 },
      ],
    },
    {
      id: 'meshclaw',
      name: 'MeshClaw',
      detected: true,
      detail: '~/.meshclaw',
      categories: [
        { id: 'skills', label: 'Skills', count: 5 },
        { id: 'workspaces', label: 'Workspaces', count: 6 },
        { id: 'rules', label: 'Rules', count: 7 },
        { id: 'personas', label: 'Agent personas', count: 2 },
      ],
    },
    {
      id: 'cursor',
      name: 'Cursor',
      detected: true,
      detail: '~/.cursor',
      categories: [{ id: 'skills', label: 'Skills', count: 8 }],
    },
    {
      id: 'quick',
      name: 'Quick',
      detected: true,
      categories: [{ id: 'skills', label: 'Skills', count: 9 }],
    },
    {
      id: 'codex',
      name: 'Codex',
      detected: false,
      categories: [{ id: 'skills', label: 'Skills', count: 0 }],
    },
  ],
  skipped: [
    { source: 'Claude Code', category: 'Credentials', reason: 'Secrets are never imported', count: 2 },
    { source: 'MeshClaw', category: 'Hooks', reason: 'Unsupported category', count: 3 },
  ],
  merge_only: true,
}

const APPLY_RESPONSE: AgentImportApplyResponse = {
  ok: true,
  conflict_strategy: 'skip',
  summary: {
    imported: 10,
    deduplicated: 1,
    skipped: 2,
    conflicts: 0,
    resolvable_conflicts: 0,
  },
}

const CONFLICTED_APPLY_RESPONSE: AgentImportApplyResponse = {
  ok: true,
  conflict_strategy: 'skip',
  summary: {
    imported: 0,
    deduplicated: 0,
    skipped: 2,
    conflicts: 2,
    resolvable_conflicts: 2,
  },
}

function mockSuccessfulRequests(scan = SCAN_RESPONSE) {
  vi.mocked(api.onboardingImportScan).mockResolvedValue(scan)
  vi.mocked(api.onboardingImportApply).mockResolvedValue(APPLY_RESPONSE)
  vi.mocked(api.onboardingImportState).mockResolvedValue({ ok: true })
}

async function goToCategories() {
  await screen.findByRole('heading', { name: 'Choose sources' })
  await userEvent.click(screen.getByRole('button', { name: 'Continue' }))
  return screen.findByRole('heading', { name: 'Select items to import' })
}

async function goToReview() {
  await goToCategories()
  await userEvent.click(screen.getByRole('button', { name: 'Continue' }))
  return screen.findByRole('heading', { name: 'Review import' })
}

async function startImport() {
  await goToReview()
  await userEvent.click(screen.getByRole('button', { name: 'Import selected' }))
}

describe('AgentImportFlow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows only detected supported sources in the first stage', async () => {
    mockSuccessfulRequests()

    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    const dialog = await screen.findByRole('dialog', { name: 'Import agent setup' })
    // The checkbox renders before the useEffect that pre-selects all eligible
    // sources fires, so wait for the checked state rather than asserting inline.
    await waitFor(() => {
      expect(within(dialog).getByRole('checkbox', { name: /Claude Code/ })).toBeChecked()
    })
    expect(within(dialog).getByRole('checkbox', { name: /MeshClaw/ })).toBeChecked()
    expect(within(dialog).queryByText('Codex')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('Cursor')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('Quick')).not.toBeInTheDocument()
    expect(api.onboardingImportScan).toHaveBeenCalledOnce()
  })

  it('closes when persisted onboarding state resolves after the first render', async () => {
    mockSuccessfulRequests()
    const { rerender } = renderWithProviders(
      <AgentImportFlow initialOpen onComplete={vi.fn()} />,
    )

    expect(await screen.findByRole('dialog', { name: 'Import agent setup' }))
      .toBeInTheDocument()

    rerender(<AgentImportFlow initialOpen={false} onComplete={vi.fn()} />)

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Import agent setup' }))
        .not.toBeInTheDocument()
    })
  })

  it('shows real category counts without unsupported import toggles', async () => {
    mockSuccessfulRequests()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await goToCategories()

    const claude = screen.getByRole('group', { name: 'Claude Code categories' })
    const meshclaw = screen.getByRole('group', { name: 'MeshClaw categories' })
    expect(within(claude).getByRole('checkbox', { name: /Skills.*4/ })).toBeChecked()
    expect(within(claude).getByRole('checkbox', { name: /MCP servers.*2/ })).toBeChecked()
    expect(within(meshclaw).getByRole('checkbox', { name: /Skills.*5/ })).toBeChecked()
    expect(within(meshclaw).getByRole('checkbox', { name: /Workspaces.*6/ })).toBeChecked()
    expect(screen.queryByRole('checkbox', { name: /Quick/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Rules/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Hooks/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /personas/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Raw instructions/i })).not.toBeInTheDocument()
  })

  it('reviews merge-only handling and sends the selected source categories', async () => {
    mockSuccessfulRequests()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await goToCategories()
    await userEvent.click(screen.getByRole('checkbox', { name: /MCP servers.*2/ }))
    await userEvent.click(screen.getByRole('checkbox', { name: /Workspaces.*6/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }))

    expect(
      screen.getByText(/existing Kiro Crew setup is never overwritten by default/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/matching items are deduplicated/i)).toBeInTheDocument()
    expect(screen.getByText(/Secrets are never imported/i)).toBeInTheDocument()
    expect(screen.getByText(/Unsupported category/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Import selected' }))
    await waitFor(() => {
      expect(api.onboardingImportApply).toHaveBeenCalledWith({
        sources: [
          { id: 'claude_code', categories: ['skills'] },
          { id: 'meshclaw', categories: ['skills'] },
        ],
        // The first apply is always the non-destructive strategy; rename and
        // overwrite require an explicit click on the conflict notice.
        conflict_strategy: 'skip',
      })
    })
  })

  it('keeps a failed apply visible and retries the same import', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportApply)
      .mockRejectedValueOnce(new Error('Import service unavailable'))
      .mockResolvedValueOnce(APPLY_RESPONSE)
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await startImport()

    expect(await screen.findByRole('alert')).toHaveTextContent('Import service unavailable')
    await userEvent.click(screen.getByRole('button', { name: 'Import selected' }))
    expect(await screen.findByText('Import complete')).toBeInTheDocument()
    expect(api.onboardingImportApply).toHaveBeenCalledTimes(2)
    expect(screen.getByText('10')).toBeInTheDocument()
  })

  it('keeps Skip and Escape disabled while an import is applying', async () => {
    let resolveApply: ((value: AgentImportApplyResponse) => void) | undefined
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportApply).mockReturnValue(
      new Promise(resolve => { resolveApply = resolve }),
    )
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await startImport()
    expect(screen.getByRole('button', { name: 'Skip all setup and onboarding' })).toBeDisabled()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(api.onboardingImportState).not.toHaveBeenCalled()

    resolveApply?.(APPLY_RESPONSE)
    expect(await screen.findByText('Import complete')).toBeInTheDocument()
  })

  it('treats Escape as "Skip all", not "Skip import"', async () => {
    mockSuccessfulRequests()
    const onComplete = vi.fn()
    const onSkipAll = vi.fn()
    renderWithProviders(
      <AgentImportFlow initialOpen onComplete={onComplete} onSkipAll={onSkipAll} />,
    )
    await screen.findByRole('dialog', { name: 'Import agent setup' })

    fireEvent.keyDown(document, { key: 'Escape' })

    // Skipping the WHOLE flow, so the host is told to skip the remaining
    // chapters (and route through the mandatory Privacy chapter) rather than
    // continue into Customize as a plain "Skip import" would.
    await waitFor(() => expect(onSkipAll).toHaveBeenCalledTimes(1))
    expect(onComplete).not.toHaveBeenCalled()
  })

  // Both "Skip all" paths (the header control and Escape) write through
  // skipAllMutation, whose failure was previously rendered nowhere — the dialog
  // just stayed open in silence.
  it('surfaces a rejected "Skip all" write instead of failing silently', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportState).mockRejectedValueOnce(
      new Error('Onboarding state service unavailable'),
    )
    const onSkipAll = vi.fn()
    renderWithProviders(
      <AgentImportFlow initialOpen onComplete={vi.fn()} onSkipAll={onSkipAll} />,
    )
    await screen.findByRole('dialog', { name: 'Import agent setup' })

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(await screen.findByRole('alert'))
      .toHaveTextContent('Onboarding state service unavailable')
    // The flow stays open and does NOT report a skip it failed to persist.
    expect(onSkipAll).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Import agent setup' })).toBeInTheDocument()
  })

  it('wraps Shift+Tab when focus starts on the dialog heading', async () => {
    mockSuccessfulRequests()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    const dialog = await screen.findByRole('dialog', { name: 'Import agent setup' })
    const heading = await within(dialog).findByRole('heading', { name: 'Choose sources' })
    await waitFor(() => expect(heading).toHaveFocus())

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })

    const focusable = within(dialog).getAllByRole('button')
    expect(document.activeElement).toBe(focusable[focusable.length - 1])
  })

  it('waits for completion state to persist before calling onComplete', async () => {
    mockSuccessfulRequests()
    let resolveState: ((value: { ok: boolean }) => void) | undefined
    vi.mocked(api.onboardingImportState).mockReturnValue(
      new Promise(resolve => { resolveState = resolve }),
    )
    const onComplete = vi.fn()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={onComplete} />)

    await startImport()
    await screen.findByText('Import complete')
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }))

    expect(api.onboardingImportState).toHaveBeenCalledWith({ completed: true })
    expect(onComplete).not.toHaveBeenCalled()
    resolveState?.({ ok: true })
    await waitFor(() => expect(onComplete).toHaveBeenCalledOnce())
  })

  it('keeps state errors visible and does not complete until retry succeeds', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportState)
      .mockRejectedValueOnce(new Error('Could not save onboarding state'))
      .mockResolvedValueOnce({ ok: true })
    const onComplete = vi.fn()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={onComplete} />)

    await startImport()
    await screen.findByText('Import complete')
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save onboarding state')
    expect(onComplete).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(onComplete).toHaveBeenCalledOnce())
  })

  it('replays from a fresh scan when the app reopens the flow', async () => {
    const firstScan = { ...SCAN_RESPONSE, sources: [SCAN_RESPONSE.sources[0]] }
    const replayScan = { ...SCAN_RESPONSE, sources: [SCAN_RESPONSE.sources[1]] }
    vi.mocked(api.onboardingImportScan)
      .mockResolvedValueOnce(firstScan)
      .mockResolvedValueOnce(replayScan)
    vi.mocked(api.onboardingImportState).mockResolvedValue({ ok: true })
    const { rerender, unmount } = renderWithProviders(
      <AgentImportFlow initialOpen={false} onComplete={vi.fn()} />,
    )
    rerender(<AgentImportFlow initialOpen onComplete={vi.fn()} />)
    expect(await screen.findByRole('checkbox', { name: /Claude Code/ })).toBeInTheDocument()
    unmount()

    const { rerender: replayRerender } = renderWithProviders(
      <AgentImportFlow initialOpen={false} onComplete={vi.fn()} />,
    )
    replayRerender(<AgentImportFlow initialOpen onComplete={vi.fn()} />)
    expect(await screen.findByRole('checkbox', { name: /MeshClaw/ })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Claude Code/ })).not.toBeInTheDocument()
  })

  it('automatically completes when no supported setup is detected', async () => {
    mockSuccessfulRequests({
      sources: [
        { id: 'codex', name: 'Codex', detected: false, categories: [] },
        {
          id: 'quick',
          name: 'Quick',
          detected: true,
          categories: [{ id: 'skills', label: 'Skills', count: 3 }],
        },
      ],
      skipped: [],
      merge_only: true,
    })
    const onComplete = vi.fn()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={onComplete} />)

    await waitFor(() => {
      expect(api.onboardingImportState).toHaveBeenCalledWith({ completed: true })
      expect(onComplete).toHaveBeenCalledOnce()
    })
    expect(screen.queryByRole('dialog', { name: 'Import agent setup' })).not.toBeInTheDocument()
  })

  it('keeps the import gate open when skip state cannot be persisted', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportState).mockRejectedValue(
      new Error('Could not save onboarding state'),
    )
    const onComplete = vi.fn()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={onComplete} />)

    await screen.findByRole('heading', { name: 'Choose sources' })
    await userEvent.click(screen.getByRole('button', { name: 'Skip import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not save onboarding state',
    )
    expect(screen.getByRole('dialog', { name: 'Import agent setup' })).toBeInTheDocument()
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('offers a resolution when items conflict, and does not by default', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportApply).mockResolvedValue(CONFLICTED_APPLY_RESPONSE)

    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)
    await startImport()
    await screen.findByText('Import complete')

    // The user is told nothing changed, and given both ways out.
    await screen.findByText('2 items already exist with different content.')
    const keepBoth = screen.getByRole('button', { name: 'Keep both' })
    expect(screen.getByRole('button', { name: 'Replace mine' })).toBeInTheDocument()

    await userEvent.click(keepBoth)

    // The retry re-sends the SAME selection with the chosen strategy. It must be
    // the FIRST call after the click: passing the strategy through the mutation
    // (rather than relying on a re-render landing first) is what stops the click
    // from repeating the unresolved 'skip' import.
    await waitFor(() => {
      expect(api.onboardingImportApply).toHaveBeenCalledTimes(2)
    })
    expect(api.onboardingImportApply).toHaveBeenLastCalledWith(
      expect.objectContaining({ conflict_strategy: 'rename' }),
    )
    expect(
      vi.mocked(api.onboardingImportApply).mock.calls.filter(
        ([body]) => body?.conflict_strategy === 'skip',
      ),
    ).toHaveLength(1)
  })

  it('shows no conflict resolution when nothing conflicted', async () => {
    mockSuccessfulRequests()

    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)
    await startImport()
    await screen.findByText('Import complete')

    expect(screen.queryByRole('button', { name: 'Keep both' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Replace mine' })).not.toBeInTheDocument()
  })
})
