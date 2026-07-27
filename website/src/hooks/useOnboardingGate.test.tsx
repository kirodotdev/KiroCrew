import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { useOnboardingGate } from './useOnboardingGate'
import { renderWithProviders } from '../test/helpers'
import AgentImportFlow from '../components/AgentImportFlow'
import OnboardingFlow from '../components/OnboardingFlow'

vi.mock('../api/client', () => ({
  api: {
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
    updateThemeConfig: vi.fn(),
    onboardingImportScan: vi.fn(),
    onboardingImportApply: vi.fn(),
    onboardingImportState: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

// Mirrors how App.tsx wires the gate to the two real first-run flows.
function Harness() {
  const { showAgentImport, showOnboarding, onImportComplete, onOnboardingComplete } =
    useOnboardingGate()
  return (
    <>
      <AgentImportFlow initialOpen={showAgentImport} onComplete={onImportComplete} />
      <OnboardingFlow initialOpen={showOnboarding} onComplete={onOnboardingComplete} />
    </>
  )
}

const SCAN = {
  sources: [
    {
      id: 'meshclaw',
      name: 'MeshClaw',
      detected: true,
      detail: '~/.meshclaw',
      categories: [{ id: 'skills', label: 'Skills', count: 5 }],
    },
  ],
  skipped: [],
  merge_only: true,
}

function seedApi(boot: { onboarded: boolean; import_onboarded: boolean }) {
  vi.mocked(api.themes).mockResolvedValue({ themes: [] } as never)
  vi.mocked(api.themeDetail).mockResolvedValue({} as never)
  vi.mocked(api.updateThemeConfig).mockResolvedValue({ ok: true } as never)
  vi.mocked(api.themeBoot).mockResolvedValue(boot as never)
  vi.mocked(api.onboardingImportScan).mockResolvedValue(SCAN as never)
  vi.mocked(api.onboardingImportState).mockResolvedValue({ ok: true } as never)
  vi.mocked(api.kirocrewConfig).mockResolvedValue({ dashboard: {} } as never)
  vi.mocked(api.patchConfig).mockResolvedValue({ ok: true } as never)
}

async function skipImport() {
  await screen.findByRole('button', { name: 'Skip for now' })
  await userEvent.click(screen.getByRole('button', { name: 'Skip for now' }))
}

describe('useOnboardingGate — import-skip continues into onboarding (Option A)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('new user: skipping first-run import opens the theme tour', async () => {
    seedApi({ onboarded: false, import_onboarded: false })
    renderWithProviders(<Harness />)

    await skipImport()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Pick your look' })).toBeInTheDocument(),
    )
  })

  it('onboarded=true but import not done: skipping first-run import still opens the tour', async () => {
    // Regression: previously the boot seed effect re-fired on the import flag
    // flip and, together with the `!onboarded` hand-off gate, suppressed the
    // tour entirely — so "Skip for now" looked like it skipped every step.
    seedApi({ onboarded: true, import_onboarded: false })
    renderWithProviders(<Harness />)

    await skipImport()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Pick your look' })).toBeInTheDocument(),
    )
  })

  it('Settings replay (plain event): skipping the re-run importer does NOT reopen the tour', async () => {
    // Fully onboarded user re-importing from Settings must not be dragged back
    // through the theme tour.
    seedApi({ onboarded: true, import_onboarded: true })
    renderWithProviders(<Harness />)

    // First-run gate stays closed for an already-import-onboarded user.
    await waitFor(() => expect(api.themeBoot).toHaveBeenCalled())
    expect(screen.queryByRole('dialog', { name: 'Import agent setup' })).not.toBeInTheDocument()

    // Settings dispatches a plain Event (no continueOnboarding).
    window.dispatchEvent(new Event('mc-start-import'))
    await skipImport()

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Import agent setup' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('heading', { name: 'Pick your look' })).not.toBeInTheDocument()
  })

  it('slash /onboarding replay (continueOnboarding): skipping the importer opens the tour', async () => {
    seedApi({ onboarded: true, import_onboarded: true })
    renderWithProviders(<Harness />)

    await waitFor(() => expect(api.themeBoot).toHaveBeenCalled())
    window.dispatchEvent(
      new CustomEvent('mc-start-import', { detail: { continueOnboarding: true } }),
    )
    await skipImport()

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Pick your look' })).toBeInTheDocument(),
    )
  })
})
