import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AgentImportApplyResponse,
  AgentImportScanResponse,
} from '../api/client'
import { api, ApiError } from '../api/client'
import { renderWithProviders } from '../test/helpers'
import AgentImportFlow from './AgentImportFlow'

// Spread the real module rather than replacing it: the component branches on
// `error instanceof ApiError`, which needs the real class identity.
vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      onboardingImportScan: vi.fn(),
      onboardingImportApply: vi.fn(),
      onboardingImportState: vi.fn(),
    },
  }
})

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
      id: 'cursor',
      name: 'Cursor',
      detected: true,
      detail: '~/.cursor',
      categories: [{ id: 'skills', label: 'Skills', count: 8 }],
    },
    {
      // Deliberately given a SUPPORTED, non-zero category: Quick must be
      // excluded because it is not an import source at all, not incidentally
      // because its categories fail the category filter.
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
    expect(within(dialog).getByRole('checkbox', { name: /Cursor/ })).toBeChecked()
    expect(within(dialog).queryByText('Codex')).not.toBeInTheDocument()
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
    expect(within(claude).getByRole('checkbox', { name: /Skills.*4/ })).toBeChecked()
    expect(within(claude).getByRole('checkbox', { name: /MCP servers.*2/ })).toBeChecked()
    expect(screen.queryByRole('checkbox', { name: /Quick/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Hooks/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Raw instructions/i })).not.toBeInTheDocument()
  })

  it('reviews merge-only handling and sends the selected source categories', async () => {
    mockSuccessfulRequests()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await goToCategories()
    await userEvent.click(screen.getByRole('checkbox', { name: /MCP servers.*2/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }))

    expect(
      screen.getByText(/existing Kiro Crew setup is never overwritten by default/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/matching items are deduplicated/i)).toBeInTheDocument()
    expect(screen.getByText(/Secrets are never imported/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Import selected' }))
    await waitFor(() => {
      expect(api.onboardingImportApply).toHaveBeenCalledWith({
        sources: [
          { id: 'claude_code', categories: ['skills'] },
          { id: 'cursor', categories: ['skills'] },
        ],
        // The first apply is always the non-destructive strategy; rename and
        // overwrite require an explicit click on the conflict notice.
        conflict_strategy: 'skip',
      })
    })
  })

  it('prefers the localized fallback when the refusal carries a backend code', async () => {
    // The backend's own prose for these routes is generic English produced in
    // Python ("request failed") that never passes through the i18n catalog.
    // Once it also sends a `code`, the failure is identified and the catalog
    // string wins. The plain-Error test below is the control: with no code, the
    // message still renders.
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportApply).mockRejectedValueOnce(
      new ApiError(500, 'request failed', '{"error":"request failed","code":"apply_failed"}'),
    )
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await startImport()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The import did not finish. Please try again.')
    expect(alert).not.toHaveTextContent('request failed')
  })

  it('preserves the actionable auth-recovery copy instead of offering a retry', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportApply).mockRejectedValueOnce(
      new ApiError(
        403,
        'Session expired. Run kirocrew token, then use the banner to sign back in.',
        '{"error":"invalid session","code":"invalid_token"}',
        true,
      ),
    )
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await startImport()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Session expired. Run kirocrew token')
    expect(alert).not.toHaveTextContent('The import did not finish. Please try again.')
  })

  it('does not tell an unauthenticated caller that retrying the import can recover', async () => {
    mockSuccessfulRequests()
    vi.mocked(api.onboardingImportApply).mockRejectedValueOnce(
      new ApiError(
        401,
        'authentication required',
        '{"error":"authentication required","code":"auth_required"}',
      ),
    )
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    await startImport()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Sign in again to continue.')
    expect(alert).not.toHaveTextContent('The import did not finish. Please try again.')
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

  it('declines a boundary Tab that belongs to an IME composition', async () => {
    // On WebKit the keydown that commits a candidate arrives AFTER
    // compositionend with `isComposing` already false — unguarded, the trap
    // would yank focus and abort the composition (issue class of the shared
    // hook's own guard).
    mockSuccessfulRequests()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    const dialog = await screen.findByRole('dialog', { name: 'Import agent setup' })
    const heading = await within(dialog).findByRole('heading', { name: 'Choose sources' })
    await waitFor(() => expect(heading).toHaveFocus())

    fireEvent.compositionStart(heading)
    fireEvent.compositionEnd(heading)
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })

    // Declined: focus stays where it was instead of wrapping to the last button.
    expect(document.activeElement).toBe(heading)
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
    expect(await screen.findByRole('checkbox', { name: /Cursor/ })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Claude Code/ })).not.toBeInTheDocument()
  })

  it('automatically completes when no supported setup is detected', async () => {
    mockSuccessfulRequests({
      sources: [
        { id: 'codex', name: 'Codex', detected: false, categories: [] },
        {
          // A real source id, so this exercises the CATEGORY filter in isolation
          // rather than overlapping with the not-a-source denylist.
          id: 'cursor',
          name: 'Cursor',
          detected: true,
          categories: [{ id: 'unsupported_only', label: 'Unsupported', count: 3 }],
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

  it('does not auto-complete when the only detected source could not be read', async () => {
    // Degrade-don't-fail turned a reader crash into zero eligible sources, which
    // the auto-complete effect read as "nothing to import" — marking first-run
    // import done and closing the dialog. The user never learned their setup was
    // detected and was never re-offered it, so a reader bug silently cost them
    // the whole import.
    mockSuccessfulRequests({
      sources: [{ id: 'codex', name: 'Codex', detected: false, categories: [] }],
      skipped: [{ source: 'Codex', category: '', reason: 'source_unreadable' }],
      merge_only: true,
    })
    const onComplete = vi.fn()
    renderWithProviders(<AgentImportFlow initialOpen onComplete={onComplete} />)

    await waitFor(() => {
      expect(screen.getByText(/could not read the files/i)).toBeInTheDocument()
    })
    expect(api.onboardingImportState).not.toHaveBeenCalledWith({ completed: true })
    expect(onComplete).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
    // Retry alone would dead-end: the reader failure is persistent, so the user
    // needs a way to settle this state without forfeiting the whole tour.
    expect(screen.getByRole('button', { name: /skip import/i })).toBeInTheDocument()
    expect(screen.queryByText(/no supported setup found/i)).not.toBeInTheDocument()
    // The heading must not claim the scan failed — it ran and found the source.
    expect(screen.queryByText(/we could not scan agent setup/i)).not.toBeInTheDocument()
  })

  it('names an unreadable source at stage 1 when other sources are readable', async () => {
    // The full-panel state only fires when NOTHING is readable, so with one good
    // source the failed one used to leave no trace on the screen where the user
    // decides what to import — they would read its absence as "unsupported".
    mockSuccessfulRequests({
      sources: [
        {
          id: 'codex',
          name: 'Codex',
          detected: true,
          categories: [{ id: 'instructions', label: 'Instructions', count: 2 }],
        },
      ],
      skipped: [{ source: 'Claude Code', category: '', reason: 'source_unreadable' }],
      merge_only: true,
    })
    renderWithProviders(<AgentImportFlow initialOpen />)

    await waitFor(() => {
      expect(screen.getByText(/could not read Claude Code/i)).toBeInTheDocument()
    })
    // Still a normal picker — one bad source must not block the readable one.
    expect(screen.getByText('Codex')).toBeInTheDocument()
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

  it('renders a source with an unknown id as eligible when detected with supported categories', async () => {
    // No source-id allowlist exists — any source the backend returns is eligible
    // provided it is detected and has at least one supported category with count > 0.
    mockSuccessfulRequests({
      sources: [
        {
          id: 'some-registered-agent',
          name: 'Some Registered Agent',
          detected: true,
          detail: '~/.some-agent',
          categories: [{ id: 'skills', label: 'Skills', count: 3 }],
        },
      ],
      skipped: [],
      merge_only: true,
    })
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    const dialog = await screen.findByRole('dialog', { name: 'Import agent setup' })
    await waitFor(() => {
      expect(within(dialog).getByRole('checkbox', { name: /Some Registered Agent/ })).toBeChecked()
    })
  })

  it('filters out a detected source whose categories are all unsupported or zero-count', async () => {
    // A source with only unsupported category ids (or zero counts) must not appear.
    mockSuccessfulRequests({
      sources: [
        {
          id: 'claude_code',
          name: 'Claude Code',
          detected: true,
          detail: '~/.claude',
          categories: [{ id: 'skills', label: 'Skills', count: 4 }],
        },
        {
          id: 'exotic_agent',
          name: 'Exotic Agent',
          detected: true,
          categories: [
            { id: 'unsupported_cat', label: 'Unsupported', count: 10 },
            { id: 'skills', label: 'Skills', count: 0 },
          ],
        },
      ],
      skipped: [],
      merge_only: true,
    })
    renderWithProviders(<AgentImportFlow initialOpen onComplete={vi.fn()} />)

    const dialog = await screen.findByRole('dialog', { name: 'Import agent setup' })
    await waitFor(() => {
      expect(within(dialog).getByRole('checkbox', { name: /Claude Code/ })).toBeChecked()
    })
    expect(within(dialog).queryByText('Exotic Agent')).not.toBeInTheDocument()
  })
})
