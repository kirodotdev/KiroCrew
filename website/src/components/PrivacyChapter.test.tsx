import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import PrivacyChapter from './PrivacyChapter'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
      beaconStatus: vi.fn().mockResolvedValue({
        enabled: true,
        would_send: true,
        reason: 'ready',
        endpoint_configured: true,
        env_override: false,
        env_var: 'KIROCREW_TELEMETRY_DISABLED',
      }),
    },
  }
})

const patchConfig = vi.mocked(api.patchConfig)

describe('PrivacyChapter', () => {
  beforeEach(() => {
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({})
  })

  it('renders the disclosure and the opt-out control', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(screen.getByRole('heading', { name: 'Privacy' })).toBeInTheDocument()
    expect(screen.getByText('Anonymous daily heartbeat')).toBeInTheDocument()
    expect(screen.getByText('Official app install receipts')).toBeInTheDocument()
    expect(screen.getByText('Never sent')).toBeInTheDocument()
    expect(screen.getByText('Stays on your device')).toBeInTheDocument()
    expect(
      await screen.findByRole('switch', { name: 'Send anonymous usage heartbeat' }),
    ).toBeInTheDocument()
  })

  it('shows the chapter name with no step counter', () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    // A single-screen chapter: the eyebrow is the name alone, never "1 of 1".
    const eyebrow = screen.getByText('Privacy', { selector: 'p' })
    expect(eyebrow).toBeInTheDocument()
    expect(eyebrow.textContent).not.toMatch(/\bof\b|·/)
  })

  it('is mandatory: no skip affordance and Escape does not dismiss', () => {
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(screen.queryByRole('button', { name: /skip/i })).not.toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('Continue is always enabled and hands off without requiring a choice', () => {
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    const button = screen.getByRole('button', { name: 'Continue' })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    expect(onContinue).toHaveBeenCalledTimes(1)
  })

  it('uses the same left panel copy as the Import setup chapter', () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    // Identical aside copy is what keeps the shared shell's mascots from
    // re-animating across the hand-off.
    expect(screen.getByText('Bring your crew with you.')).toBeInTheDocument()
    expect(
      screen.getByText('Merge-only setup · credentials stay where they are'),
    ).toBeInTheDocument()
  })

  it('opting out writes the beacon flag', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    const toggle = await screen.findByRole('switch', {
      name: 'Send anonymous usage heartbeat',
    })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(toggle)

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith('telemetry.beacon_enabled', false))
  })

  it('renders nothing while closed', () => {
    renderWithProviders(<PrivacyChapter open={false} onContinue={vi.fn()} />)

    expect(screen.queryByRole('heading', { name: 'Privacy' })).not.toBeInTheDocument()
  })
})
