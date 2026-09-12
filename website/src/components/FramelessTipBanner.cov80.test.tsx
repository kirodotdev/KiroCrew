import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import FramelessTipBanner from './FramelessTipBanner'

// Match the FramePrefsState shape declared in FramelessTipBanner.tsx.
type FramePrefsState = {
  platform: string
  isFrameless: boolean
  isWayland: boolean
  frameDecisionReason: string
  tipShown: { frameless: boolean }
}

const getState = vi.fn<[], Promise<FramePrefsState>>()
const markTipShown = vi.fn<[string], Promise<{ ok: boolean }>>()
const enableFrames = vi.fn<[], Promise<{ restartRequired: boolean }>>()
const restartApp = vi.fn<[], Promise<{ ok: boolean }>>()

/** Installs (or removes) the preload bridge the component reads off `window`. */
function bridge(present: boolean) {
  const w = window as unknown as { framePrefsAPI?: unknown }
  if (present) w.framePrefsAPI = { getState, markTipShown, enableFrames, restartApp }
  else delete w.framePrefsAPI
}

function state(over: Partial<FramePrefsState> = {}): FramePrefsState {
  return {
    platform: 'linux',
    isFrameless: true,
    isWayland: true,
    frameDecisionReason: 'wayland-csd',
    tipShown: { frameless: false },
    ...over,
  }
}

describe('FramelessTipBanner', () => {
  beforeEach(() => {
    getState.mockReset()
    markTipShown.mockReset()
    enableFrames.mockReset()
    restartApp.mockReset()
    getState.mockResolvedValue(state())
    markTipShown.mockResolvedValue({ ok: true })
    enableFrames.mockResolvedValue({ restartRequired: true })
    restartApp.mockResolvedValue({ ok: true })
    bridge(true)
  })
  afterEach(() => bridge(false))

  // ── Conditional rendering ──

  it('renders when Linux + Wayland + frameless + not dismissed', async () => {
    renderWithProviders(<FramelessTipBanner />)
    // The banner's title text is a translation key by default in tests, so
    // assert on the primary CTA button label which is stable across setups.
    expect(await screen.findByRole('button', { name: /enable/i })).toBeInTheDocument()
  })

  it('never renders without the desktop bridge, and never probes for one', async () => {
    bridge(false)
    renderWithProviders(<FramelessTipBanner />)
    await waitFor(() => expect(getState).not.toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
  })

  it('stays silent on non-Linux platforms (macOS/Windows do not have CSD to fix)', async () => {
    getState.mockResolvedValue(state({ platform: 'darwin', isWayland: false, isFrameless: false }))
    renderWithProviders(<FramelessTipBanner />)
    await waitFor(() => expect(getState).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
  })

  it('stays silent on X11 Linux (bar is visible on OS-drawn frame — no discoverability problem)', async () => {
    getState.mockResolvedValue(state({ isWayland: false, isFrameless: false }))
    renderWithProviders(<FramelessTipBanner />)
    await waitFor(() => expect(getState).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
  })

  it('stays silent on Wayland if the compositor draws SSD frames (not-frameless)', async () => {
    getState.mockResolvedValue(state({ isFrameless: false }))
    renderWithProviders(<FramelessTipBanner />)
    await waitFor(() => expect(getState).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
  })

  it('stays silent once the user has previously dismissed the tip', async () => {
    getState.mockResolvedValue(state({ tipShown: { frameless: true } }))
    renderWithProviders(<FramelessTipBanner />)
    await waitFor(() => expect(getState).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
  })

  // ── Error surfacing on mount-time IPC failure (GPT 5.6 F2 gate) ──

  it('surfaces a getState rejection through ErrorNotice even while phase is hidden', async () => {
    // The `phase === 'hidden'` early-return branch renders an error-only
    // shell when `error` is captured — GPT 5.6 blocker on 16d827598 that
    // was fixed on e3f35f84a. Regression-pin the fix.
    getState.mockRejectedValue(new Error('frame-prefs:get failed: lsof unavailable'))
    renderWithProviders(<FramelessTipBanner />)
    // The ErrorNotice component renders role="alert" via testing-library.
    await screen.findByRole('alert')
  })

  // ── Primary action: Enable window borders ──

  it('clicking Enable window borders writes the config and shows the restart prompt', async () => {
    renderWithProviders(<FramelessTipBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await waitFor(() => expect(enableFrames).toHaveBeenCalled())
    // Restart-prompt phase surfaces two new buttons; the primary is "Restart".
    expect(await screen.findByRole('button', { name: /restart/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /later/i })).toBeInTheDocument()
  })

  it('surfaces an enableFrames failure through ErrorNotice instead of silently proceeding', async () => {
    enableFrames.mockRejectedValue(new Error('config write failed'))
    renderWithProviders(<FramelessTipBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await screen.findByRole('alert')
    // Banner is still visible — no silent transition to restart-prompt.
    expect(screen.getByRole('button', { name: /enable/i })).toBeInTheDocument()
  })

  // ── Restart-prompt sub-phase ──

  it('clicking Restart now calls restartApp', async () => {
    renderWithProviders(<FramelessTipBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await waitFor(() => expect(enableFrames).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /restart/i }))
    await waitFor(() => expect(restartApp).toHaveBeenCalled())
  })

  it('clicking Later dismisses the banner without restarting', async () => {
    renderWithProviders(<FramelessTipBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await waitFor(() => expect(enableFrames).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /later/i }))
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /restart/i })).not.toBeInTheDocument()
    })
    expect(restartApp).not.toHaveBeenCalled()
  })

  it('surfaces a restartApp failure through ErrorNotice rather than silently dismissing', async () => {
    restartApp.mockRejectedValue(new Error('app.quit refused'))
    renderWithProviders(<FramelessTipBanner />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await waitFor(() => expect(enableFrames).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /restart/i }))
    await screen.findByRole('alert')
  })

  // ── Secondary action: Don't remind me ──

  it("clicking Don't remind me writes the dismissal flag and hides the banner", async () => {
    renderWithProviders(<FramelessTipBanner />)
    // The dismiss button contains an X icon + label; match by the localized
    // "remind" fragment which is stable across the label variants.
    const enableBtn = await screen.findByRole('button', { name: /enable/i })
    const dismissBtn = enableBtn.parentElement?.querySelectorAll('button')[1] as HTMLElement
    fireEvent.click(dismissBtn)
    await waitFor(() => expect(markTipShown).toHaveBeenCalledWith('frameless'))
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
    })
  })

  it('surfaces a markTipShown failure through ErrorNotice rather than silently dismissing', async () => {
    markTipShown.mockRejectedValue(new Error('store write failed'))
    renderWithProviders(<FramelessTipBanner />)
    const enableBtn = await screen.findByRole('button', { name: /enable/i })
    const dismissBtn = enableBtn.parentElement?.querySelectorAll('button')[1] as HTMLElement
    fireEvent.click(dismissBtn)
    await screen.findByRole('alert')
    // Banner is still visible — the failed dismissal did not take effect.
    expect(screen.getByRole('button', { name: /enable/i })).toBeInTheDocument()
  })
})
