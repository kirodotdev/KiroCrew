import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import OnboardingFlow from '../components/OnboardingFlow'
import { api } from '../api/client'

// Partial api mock: profile read/write + theme boot. Everything else keeps its
// real implementation (ThemeProvider's ancillary fetches no-op in jsdom).
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn().mockResolvedValue({
        dashboard: { user_role: '', user_technical_level: '' },
      }),
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
    },
  }
})

const patchConfig = vi.mocked(api.patchConfig)
const kirocrewConfig = vi.mocked(api.kirocrewConfig)

const advanceToStep2 = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
}

describe('OnboardingFlow — About You step', () => {
  beforeEach(() => {
    // Full reset + re-arm defaults so per-test overrides (mockRejectedValue)
    // can't leak across tests.
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({})
    kirocrewConfig.mockReset()
    kirocrewConfig.mockResolvedValue({
      dashboard: { user_role: '', user_technical_level: '' },
    })
  })

  it('step 1 → Next shows the About You modal with role and technical options', () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    expect(screen.getByText('Pick your look')).toBeInTheDocument()
    advanceToStep2()
    expect(screen.getByText('Your role')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'UX Designer' })).toBeInTheDocument()
    expect(screen.getByText('How technical are you?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'I write code' })).toBeInTheDocument()
  })

  it('persists selected role + technical level on Next and advances to the tour', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Somewhat' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => {
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'designer')
      expect(patchConfig).toHaveBeenCalledWith(
        'dashboard.user_technical_level',
        'somewhat-technical',
      )
    })
    // Tour popover (step 3) is up next
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
  })

  it('does not write config when nothing was selected', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('deselecting an answer before Next results in no write', async () => {
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    const chip = screen.getByRole('button', { name: 'Developer' })
    fireEvent.click(chip)
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(chip) // toggle off — back to the initial ''
    expect(chip).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('Skip on the About You step still persists answers already selected', async () => {
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Product Manager' }))
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    await waitFor(() => {
      expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'product-manager')
    })
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
  })

  it('Skip with a failing save keeps the modal open; a second Skip discards explicitly', async () => {
    const onComplete = vi.fn()
    patchConfig.mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    // First Skip: save fails → informed, NOT dismissed
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Skip again/)
    expect(onComplete).not.toHaveBeenCalled()
    expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
    // Second Skip: explicit discard → dismissed
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
  })

  it('Escape on the About You step dismisses the flow (modal a11y)', async () => {
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
  })

  it('preselects previously saved answers for /onboarding replays', async () => {
    kirocrewConfig.mockResolvedValue({
      dashboard: { user_role: 'developer', user_technical_level: 'codes' },
    })
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Developer' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
      expect(screen.getByRole('button', { name: 'I write code' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
    })
    // Unchanged answers → Next writes nothing
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('failed write keeps the modal open with an error and never advances', async () => {
    patchConfig.mockRejectedValueOnce(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    // Error surfaces, still on step 2, tour NOT shown
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn't save/)
    expect(screen.getByText('Tell Kiro about you')).toBeInTheDocument()
    expect(screen.queryByText('Work that runs on time')).not.toBeInTheDocument()
  })

  it('Next retries a failed write and advances once it succeeds', async () => {
    patchConfig.mockRejectedValueOnce(new Error('gateway down'))
    renderWithProviders(<OnboardingFlow initialOpen onComplete={vi.fn()} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('alert')
    // Baseline must NOT have advanced on failure — retry re-sends the field.
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).toHaveBeenCalledTimes(2)
    expect(patchConfig).toHaveBeenLastCalledWith('dashboard.user_role', 'designer')
  })

  it('freezes chips, segments, and Skip while a save is in flight', async () => {
    // Hold the PATCH open so the in-flight window is observable.
    let release: (v: unknown) => void = () => {}
    patchConfig.mockImplementationOnce(
      () => new Promise(res => { release = res }),
    )
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    // In-flight: every input frozen — changing a chip now would advance the
    // flow with a stale value persisted (GPT round-3 race).
    await screen.findByRole('button', { name: 'Saving…' })
    const designerChip = screen.getByRole('button', { name: 'UX Designer' })
    expect(designerChip).toBeDisabled()
    fireEvent.click(designerChip) // no-op while frozen
    expect(designerChip).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: /Skip/ })).toBeDisabled()
    fireEvent.keyDown(document, { key: 'Escape' }) // dismissal frozen too
    expect(onComplete).not.toHaveBeenCalled()
    // Release the PATCH → flow advances with the snapshotted value.
    release({})
    expect(await screen.findByText('Work that runs on time')).toBeInTheDocument()
    expect(patchConfig).toHaveBeenCalledTimes(1)
    expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'developer')
  })

  it('freezes inputs during the Skip-path save too (round-4 race)', async () => {
    let release: (v: unknown) => void = () => {}
    patchConfig.mockImplementationOnce(
      () => new Promise(res => { release = res }),
    )
    const onComplete = vi.fn()
    renderWithProviders(<OnboardingFlow initialOpen onComplete={onComplete} />)
    advanceToStep2()
    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    // Skip's save is in flight: chips must be frozen so the completion that
    // follows can't silently drop an edit made mid-flight.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'UX Designer' })).toBeDisabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'UX Designer' })) // no-op
    expect(screen.getByRole('button', { name: 'UX Designer' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    release({})
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
    expect(patchConfig).toHaveBeenCalledTimes(1)
    expect(patchConfig).toHaveBeenCalledWith('dashboard.user_role', 'developer')
  })
})
