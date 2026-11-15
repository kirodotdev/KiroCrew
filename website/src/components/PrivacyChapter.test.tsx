import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import PrivacyChapter from './PrivacyChapter'
import { LocalOverlayRoot } from './overlayRoot'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      patchConfig: vi.fn().mockResolvedValue({}),
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: false }),
      beaconStatus: vi.fn(),
      collectionStatus: vi.fn(),
    },
  }
})

const patchConfig = vi.mocked(api.patchConfig)

/** The buttons wait for the status reads; resolve with the button once usable. */
async function readyButton(name: string) {
  const button = await screen.findByRole('button', { name })
  await waitFor(() => expect(button).toBeEnabled())
  return button
}
const beaconStatus = vi.mocked(api.beaconStatus)
const collectionStatus = vi.mocked(api.collectionStatus)

const BEACON_ON = {
  enabled: true,
  would_send: true,
  reason: 'ready',
  reason_code: 'ready',
  endpoint_configured: true,
  env_override: false,
  env_var: 'KIROCREW_TELEMETRY_DISABLED',
  governance_override: false,
}

// The gateway as the config writes leave it: Turn off flips both switches, so
// the status re-read after the writes reports what was written.
let stored = { recording: true, beacon: true }
function statusFromStore() {
  beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, enabled: stored.beacon }))
  collectionStatus.mockImplementation(async () => ({ enabled: stored.recording, env_pinned: false, env_var: 'KIROCREW_TELEMETRY' }))
  patchConfig.mockImplementation(async (path: string, value: unknown) => {
    if (path === 'telemetry.enabled') stored.recording = value as boolean
    if (path === 'telemetry.beacon_enabled') stored.beacon = value as boolean
    return {}
  })
}
function statusOn() {
  stored = { recording: true, beacon: true }
  statusFromStore()
}
function renderChapter(props: { passWhenOff?: boolean; onContinue?: ReturnType<typeof vi.fn> } = {}) {
  const onContinue = props.onContinue ?? vi.fn()
  renderWithProviders(<PrivacyChapter open onContinue={onContinue} passWhenOff={props.passWhenOff} />)
  return { onContinue }
}

describe('PrivacyChapter', () => {
  beforeEach(() => {
    patchConfig.mockReset()
    statusOn()
  })

  it('says usage data is on and lists what is and is never collected', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: 'Performance metrics are on' })).toBeInTheDocument()
    expect(screen.getByText(/records performance metrics on this machine/)).toBeInTheDocument()
    expect(screen.getByText('What is collected')).toBeInTheDocument()
    expect(screen.getByText('A random install ID that is not linked to you')).toBeInTheDocument()
    expect(screen.getByText('What is never collected')).toBeInTheDocument()
    expect(screen.getByText("Your prompts or the model's replies")).toBeInTheDocument()
    expect(screen.getByText('Where it goes')).toBeInTheDocument()
    expect(screen.getByText(/performance metrics are the Record metrics switch, and the heartbeat is the Send anonymous usage heartbeat switch/)).toBeInTheDocument()
    // The shared detail sections stay below the notice.
    expect(screen.getByText('Anonymous daily heartbeat')).toBeInTheDocument()
    expect(screen.getByText('Never sent')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Keep on' })).toBeEnabled())
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeEnabled()
  })

  it('keeps both buttons disabled until the status is known', () => {
    beaconStatus.mockImplementation(() => new Promise(() => {}))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Keep on' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeDisabled()
  })

  it('shows the chapter name with no step counter', () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    // A single-screen chapter: the eyebrow is the name alone, never "1 of 1".
    const eyebrow = screen.getByText('Privacy', { selector: 'p' })
    expect(eyebrow).toBeInTheDocument()
    expect(eyebrow.textContent).not.toMatch(/\bof\b|·/)
  })

  it("gives the left panel the Privacy copy, not the Import step's", () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    // With nothing to import, Import passes by itself after its scan, so Privacy
    // is the first screen a user reads: its panel must not advertise Import.
    expect(screen.getByRole('heading', { name: 'See what is collected.' })).toBeInTheDocument()
    expect(screen.getByText('What Kiro Crew records on this machine, what it sends, and what it never collects.')).toBeInTheDocument()
    expect(screen.getByText('The same details are in Settings > Privacy.')).toBeInTheDocument()
    expect(screen.queryByText('Bring your crew with you.')).not.toBeInTheDocument()
  })

  it('is mandatory: no skip affordance and Escape does not dismiss', () => {
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(screen.queryByRole('button', { name: /skip/i })).not.toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('traps Tab while showing, and leaves it to the Crew in front while hidden', async () => {
    const shown = renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    // The buttons are the ring; they enable once the status is read.
    await readyButton('Turn off')
    ;(document.activeElement as HTMLElement | null)?.blur()
    // From outside the dialog the trap claims Tab and wraps into it.
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    shown.unmount()

    renderWithProviders(
      <LocalOverlayRoot hidden><PrivacyChapter open onContinue={vi.fn()} /></LocalOverlayRoot>,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: 'Turn off', hidden: true })).toBeEnabled())
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(true)
  })

  it('Keep on records the ack and the notice version, and writes no telemetry switch', async () => {
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    fireEvent.click(await readyButton('Keep on'))
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true }))
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('with only the heartbeat on it says so, lists no recording, and records no notice version', async () => {
    stored = { recording: false, beacon: true }
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(await screen.findByRole('heading', { name: 'A daily heartbeat is on' })).toBeInTheDocument()
    // The intro does not repeat what "Where it goes" says just below it.
    expect(screen.queryByText(/Performance metrics are not recorded on this machine/)).not.toBeInTheDocument()
    expect(screen.getByText(/No performance metrics are recorded on this machine. Once a day/)).toBeInTheDocument()
    expect(screen.queryByText('What is collected')).not.toBeInTheDocument()
    // Recording is already off here, so the scope line does not name it.
    expect(screen.getByTestId('privacy-turn-off-scope')).toHaveTextContent(
      'Turn off stops the daily heartbeat and official app install receipts.',
    )
    fireEvent.click(await readyButton('Keep on'))
    // The version means "told that recording is on"; this text did not say so.
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: false }))
  })

  it('Turn off over the heartbeat-only text records no notice version', async () => {
    stored = { recording: false, beacon: true }
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByRole('heading', { name: 'A daily heartbeat is on' })
    fireEvent.click(await readyButton('Turn off'))
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: false }))
    expect(patchConfig).toHaveBeenCalledWith('telemetry.beacon_enabled', false)
  })

  it('Turn off keeps the chapter open when something outside config.json holds a switch on', async () => {
    // An env var or config.local.json: the writes succeed, the effective state does not move.
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, overlay_override: true }))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    fireEvent.click(await readyButton('Turn off'))
    expect(await screen.findByText(/config.local.json in the .* data folder \(~\/\.kiro\/crew unless KIROCREW_HOME is set\) keeps it on/)).toBeInTheDocument()
    // No host to hide the chapter: a chat hand-off would land out of reach.
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).not.toBeInTheDocument()
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('hides the chapter without recording a choice when the error goes to the agent', async () => {
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, overlay_override: true }))
    const onContinue = vi.fn()
    const onAskAgent = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} onAskAgent={onAskAgent} />)

    await screen.findByText('What is collected')
    fireEvent.click(await readyButton('Turn off'))
    fireEvent.click(await screen.findByRole('button', { name: 'Ask the agent' }))
    await waitFor(() => expect(onAskAgent).toHaveBeenCalledTimes(1))
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('does not say nothing changed when the off writes landed but the read-back failed', async () => {
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    collectionStatus.mockImplementation(async () => { throw new Error('read-back failed') })
    fireEvent.click(await readyButton('Turn off'))
    expect(await screen.findByText(/Turn off could not be finished or confirmed/)).toBeInTheDocument()
    expect(screen.queryByText(/nothing changed/)).not.toBeInTheDocument()
    expect(patchConfig).toHaveBeenCalledWith('telemetry.beacon_enabled', false)
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('Turn off names the env var that keeps recording on', async () => {
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: true, env_var: 'KIROCREW_TELEMETRY' }))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByText('What is collected')
    fireEvent.click(await readyButton('Turn off'))
    expect(await screen.findByText(/the KIROCREW_TELEMETRY environment variable keeps it on/)).toBeInTheDocument()
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('says so when the status cannot be read, and still offers both buttons', async () => {
    beaconStatus.mockRejectedValue(new Error('500'))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/Could not check what is on right now/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'What can be collected' })).toBeInTheDocument()
    // No on/off claim anywhere, and the primary button does not say "Keep on".
    expect(screen.getByText(/Here are the performance metrics and the heartbeat .* can collect/)).toBeInTheDocument()
    expect(screen.getByText(/Performance metrics stay on this machine unless a metrics destination is set up for it. When the heartbeat is on/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Keep on' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue without changes' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeEnabled()
  })

  it('the Stays on your device line states the recording it read, not the default', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    expect(
      await screen.findByText('Usage and context records. Performance metrics are being recorded on this device now.'),
    ).toBeInTheDocument()
  })

  it('the off variant says nothing is sent and lists no collection', async () => {
    stored = { recording: false, beacon: false }
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: 'Performance metrics and the heartbeat are off on this machine' })).toBeInTheDocument()
    expect(screen.getByText('No performance metrics are recorded on this machine and no heartbeat is sent.')).toBeInTheDocument()
    expect(screen.queryByText('What is collected')).not.toBeInTheDocument()
    expect(screen.getByTestId('privacy-off-reason')).toHaveTextContent('They were turned off earlier.')
    expect(screen.getByTestId('privacy-details-off')).toHaveTextContent('Off on this machine')
    // Usage and context records are still written locally, so the note must not say "nothing".
    expect(screen.getByTestId('privacy-details-off')).toHaveTextContent('Usage and context records still stay on this device')
    expect(screen.getByTestId('privacy-details-off')).not.toHaveTextContent(/nothing described below/)
    expect(screen.queryByTestId('privacy-turn-off-scope')).not.toBeInTheDocument()
  })

  it('keeps the off form when usage data is pinned off, recording nothing on its own', async () => {
    beaconStatus.mockResolvedValue({ ...BEACON_ON, enabled: false, env_override: true, env_var: 'DO_NOT_TRACK' })
    collectionStatus.mockResolvedValue({ enabled: false, env_pinned: true, env_var: 'DO_NOT_TRACK' })
    const { onContinue } = renderChapter({ passWhenOff: true })
    expect(await readyButton('Continue')).toBeTruthy()
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('keeps the off form when a config.local.json entry holds usage data off', async () => {
    beaconStatus.mockResolvedValue({ ...BEACON_ON, enabled: false, overlay_override: true })
    collectionStatus.mockResolvedValue({ enabled: false, env_pinned: false })
    const { onContinue } = renderChapter({ passWhenOff: true })
    expect(await readyButton('Continue')).toBeTruthy()
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('passes on its own when the user turned usage data off, recording the ack', async () => {
    beaconStatus.mockResolvedValue({ ...BEACON_ON, enabled: false })
    collectionStatus.mockResolvedValue({ enabled: false, env_pinned: false })
    const { onContinue } = renderChapter({ passWhenOff: true })
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: false }))
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
  })

  it('shows the off form with its error when the ack for an off install fails', async () => {
    beaconStatus.mockResolvedValue({ ...BEACON_ON, enabled: false })
    collectionStatus.mockResolvedValue({ enabled: false, env_pinned: false })
    renderChapter({ passWhenOff: true, onContinue: vi.fn().mockRejectedValue(new Error('500')) })
    expect(await screen.findByRole('button', { name: 'Continue' })).toBeTruthy()
  })

  it('counts the heartbeat fields it points to below', async () => {
    // "Where it goes" names a count; keep it equal to the list it refers to.
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/with the five fields listed under Anonymous daily heartbeat below/)).toBeInTheDocument()
    const fields = screen.getByText(/^Random installation ID · /).textContent ?? ''
    expect(fields.split(' · ')).toHaveLength(5)
  })

  it('says what Turn off stops, next to the buttons', async () => {
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByTestId('privacy-turn-off-scope')).toHaveTextContent(
      'Turn off stops performance metrics, the daily heartbeat, and official app install receipts.',
    )
  })

  it('Turn off writes both switches off BEFORE the ack', async () => {
    const order: string[] = []
    patchConfig.mockImplementation(async (path: string, value: unknown) => {
      order.push(`${path}=${String(value)}`)
      if (path === 'telemetry.enabled') stored.recording = value as boolean
      if (path === 'telemetry.beacon_enabled') stored.beacon = value as boolean
      return {}
    })
    const onContinue = vi.fn(async () => { order.push('ack') })
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(await readyButton('Turn off'))
    await waitFor(() => expect(onContinue).toHaveBeenCalledTimes(1))
    expect(order).toEqual(['telemetry.enabled=false', 'telemetry.beacon_enabled=false', 'ack'])
    expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true })
  })

  it('a failed off write keeps the chapter open with an error and records no ack', async () => {
    patchConfig.mockRejectedValueOnce(new Error('500'))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(await readyButton('Turn off'))
    // A failed Turn off claims neither "off" nor "nothing changed": a write may
    // have landed before the error.
    expect(await screen.findByText(/Turn off could not be finished or confirmed/)).toBeInTheDocument()
    expect(screen.queryByText(/nothing changed/)).not.toBeInTheDocument()
    expect(onContinue).not.toHaveBeenCalled()
    // Still on screen, and the buttons are usable again for a retry.
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeEnabled()
  })

  it('a failed ack write keeps the chapter open with an error', async () => {
    const onContinue = vi.fn().mockRejectedValue(new Error('500'))
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(await readyButton('Keep on'))
    expect(await screen.findByText(/Your choice could not be saved/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep on' })).toBeEnabled()
  })

  describe('a way out once a save failed', () => {
    it('offers no Close, and Escape does nothing, while a choice can still be saved', async () => {
      const onClose = vi.fn()
      renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} onClose={onClose} />)

      await readyButton('Keep on')
      expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('after a failed Keep on, Close hides it without recording a choice', async () => {
      const onContinue = vi.fn().mockRejectedValue(new Error('403'))
      const onClose = vi.fn()
      renderWithProviders(<PrivacyChapter open onContinue={onContinue} onClose={onClose} />)

      fireEvent.click(await readyButton('Keep on'))
      fireEvent.click(await screen.findByRole('button', { name: 'Close' }))
      expect(onClose).toHaveBeenCalledTimes(1)
      // The one call is the failed save itself; closing records nothing more.
      expect(onContinue).toHaveBeenCalledTimes(1)
    })

    it('after a failed Turn off, Escape hides it without recording a choice', async () => {
      // The gateway refuses the write (a caller that is not the owner, say).
      patchConfig.mockRejectedValue(new Error('403'))
      const onContinue = vi.fn()
      const onClose = vi.fn()
      renderWithProviders(<PrivacyChapter open onContinue={onContinue} onClose={onClose} />)

      fireEvent.click(await readyButton('Turn off'))
      await screen.findByText(/Turn off could not be finished or confirmed/)
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)
      expect(onContinue).not.toHaveBeenCalled()
    })

    it('offers no Close when Turn off is held by a pin, since Keep on still saves', async () => {
      collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, overlay_override: true }))
      const onClose = vi.fn()
      renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} onClose={onClose} />)

      fireEvent.click(await readyButton('Turn off'))
      await screen.findByText(/keeps it on/)
      expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })
  })

  it('says it is off, with the reason and one Continue, when DO_NOT_TRACK pins it', async () => {
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, env_override: true, env_var: 'DO_NOT_TRACK', would_send: false }))
    collectionStatus.mockImplementation(async () => ({ enabled: false, env_pinned: true, env_var: 'DO_NOT_TRACK' }))
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    expect(
      await screen.findByRole('heading', { name: 'Performance metrics and the heartbeat are off on this machine' }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('privacy-off-reason')).toHaveTextContent('DO_NOT_TRACK')
    expect(screen.queryByRole('button', { name: 'Turn off' })).not.toBeInTheDocument()
    fireEvent.click(await readyButton('Continue'))
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: false }))
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('names the administrator policy when governance pins it off', async () => {
    stored = { recording: false, beacon: true }
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, governance_override: true, would_send: false }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByTestId('privacy-off-reason')).toHaveTextContent(
      "Your administrator's security policy turns it off.",
    )
  })

  it('Turn off names config.local.json when the gateway refuses a write the overlay owns', async () => {
    // The overlay holds telemetry.enabled on; its write is refused with 409.
    patchConfig.mockImplementation(async (path: string, value: unknown) => {
      if (path === 'telemetry.enabled') {
        throw new ApiError(409, 'owned', JSON.stringify({ code: 'config_overlay_owned', overlay_override: true }))
      }
      if (path === 'telemetry.beacon_enabled') stored.beacon = value as boolean
      return {}
    })
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, overlay_override: true }))
    const onContinue = vi.fn()
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(await readyButton('Turn off'))
    expect(await screen.findByText(/config.local.json in the .* data folder/)).toBeInTheDocument()
    expect(screen.queryByText(/Your choice could not be saved/)).not.toBeInTheDocument()
    // The heartbeat write config.json accepts is still sent.
    expect(patchConfig).toHaveBeenCalledWith('telemetry.beacon_enabled', false)
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('Turn off finishes when the overlay-owned key is already off', async () => {
    patchConfig.mockImplementation(async (path: string, value: unknown) => {
      if (path === 'telemetry.enabled') {
        stored.recording = false
        throw new ApiError(409, 'owned', JSON.stringify({ code: 'config_overlay_owned' }))
      }
      if (path === 'telemetry.beacon_enabled') stored.beacon = value as boolean
      return {}
    })
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    fireEvent.click(await readyButton('Turn off'))
    await waitFor(() => expect(onContinue).toHaveBeenCalledTimes(1))
  })

  it('does not point to Settings when DO_NOT_TRACK or a policy keeps it off', async () => {
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, env_override: true, env_var: 'DO_NOT_TRACK', would_send: false }))
    collectionStatus.mockImplementation(async () => ({ enabled: false, env_pinned: true, env_var: 'DO_NOT_TRACK' }))
    const shown = renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    expect(await screen.findByTestId('privacy-off-reason')).toBeInTheDocument()
    expect(screen.queryByTestId('privacy-change-later')).not.toBeInTheDocument()
    shown.unmount()

    stored = { recording: false, beacon: true }
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, governance_override: true, would_send: false }))
    collectionStatus.mockImplementation(async () => ({ enabled: false, env_pinned: false, env_var: 'KIROCREW_TELEMETRY' }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    expect(await screen.findByTestId('privacy-off-reason')).toHaveTextContent('administrator')
    expect(screen.queryByTestId('privacy-change-later')).not.toBeInTheDocument()
  })

  it('still points to Settings when usage data was turned off there', async () => {
    stored = { recording: false, beacon: false }
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)
    expect(await screen.findByTestId('privacy-off-reason')).toHaveTextContent('They were turned off earlier.')
    expect(screen.getByTestId('privacy-change-later')).toBeInTheDocument()
  })

  it('says metrics also leave the machine when an export destination is set up', async () => {
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, otlp_configured: true }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/also sent to the metrics destination set up for it. Once a day/)).toBeInTheDocument()
    expect(screen.queryByText(/Performance metrics stay on this machine/)).not.toBeInTheDocument()
  })

  it('does not say a heartbeat is sent when recording is on and the heartbeat is off', async () => {
    stored = { recording: true, beacon: false }
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText('Performance metrics stay on this machine. No heartbeat is sent.')).toBeInTheDocument()
  })

  it('names both facts when it exports and the heartbeat is off', async () => {
    beaconStatus.mockImplementation(async () => ({ ...BEACON_ON, enabled: false }))
    collectionStatus.mockImplementation(async () => ({ enabled: true, env_pinned: false, otlp_configured: true }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/also sent to the metrics destination set up for it. No heartbeat is sent./)).toBeInTheDocument()
  })

  it('says metrics stay local while export waits for an explicit opt-in', async () => {
    // On by default only: the provider attaches no export reader until
    // telemetry.enabled: true is written, so nothing is sent yet.
    collectionStatus.mockImplementation(async () => ({
      enabled: true, env_pinned: false, otlp_configured: true, export_permitted: false,
    }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/Performance metrics stay on this machine. Once a day/)).toBeInTheDocument()
    expect(screen.queryByText(/also sent to the metrics destination/)).not.toBeInTheDocument()
  })

  it('names Kiro telemetry when the build sends product usage metrics there', async () => {
    collectionStatus.mockImplementation(async () => ({
      enabled: true,
      env_pinned: false,
      otlp_configured: true,
      export_permitted: true,
      otlp_destinations: [{ name: 'kiro-telemetry', source: 'provider', locked: true }],
    }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByTestId('privacy-kiro-telemetry')).toHaveTextContent(/to Kiro telemetry, labelled with a hashed form of the random install ID/)
    // Named once: the line about a destination set up for the machine is for the
    // operator's own collectors, and there is none here. Never "stay on this machine".
    expect(screen.queryByText(/also sent to the metrics destination set up for it/)).not.toBeInTheDocument()
    expect(screen.getByText(/^Performance metrics are recorded on this machine. Once a day/)).toBeInTheDocument()
    expect(screen.queryByText(/stay on this machine/)).not.toBeInTheDocument()
  })

  it('Keep on records the Kiro telemetry ack only when the text named it, and says Turn off stops it', async () => {
    collectionStatus.mockImplementation(async () => ({
      enabled: true,
      env_pinned: false,
      otlp_configured: true,
      export_permitted: false,
      otlp_destinations: [{ name: 'kiro-telemetry', source: 'provider', locked: true }],
    }))
    const onContinue = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(<PrivacyChapter open onContinue={onContinue} />)

    await screen.findByTestId('privacy-kiro-telemetry')
    expect(screen.getByTestId('privacy-turn-off-scope')).toHaveTextContent('the product usage metrics sent to Kiro telemetry')
    fireEvent.click(await readyButton('Keep on'))
    await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true, kiroTelemetry: true }))
  })

  it('does not name Kiro telemetry for an operator collector of the same name', async () => {
    collectionStatus.mockImplementation(async () => ({
      enabled: true,
      env_pinned: false,
      otlp_configured: true,
      otlp_destinations: [{ name: 'kiro-telemetry', source: 'otlp_endpoints', locked: false }],
    }))
    renderWithProviders(<PrivacyChapter open onContinue={vi.fn()} />)

    expect(await screen.findByText(/also sent to the metrics destination set up for it/)).toBeInTheDocument()
    expect(screen.queryByTestId('privacy-kiro-telemetry')).not.toBeInTheDocument()
  })

  describe('off only by a written false an older build wrote', () => {
    function statusOffer() {
      stored = { recording: false, beacon: true }
      statusFromStore()
      collectionStatus.mockImplementation(async () => ({
        enabled: stored.recording,
        env_pinned: false,
        env_var: 'KIROCREW_TELEMETRY',
        off_offer: !stored.recording,
      }))
    }

    it('names Kiro telemetry, says nothing changes, and offers Turn on / Keep off', async () => {
      statusOffer()
      renderChapter()

      expect(await screen.findByRole('heading', { name: 'Performance metrics are off' })).toBeInTheDocument()
      expect(screen.getByText(/also sends product usage metrics to Kiro telemetry\. While they are off, only the anonymous daily heartbeat and official app install receipts are sent/)).toBeInTheDocument()
      expect(screen.getByTestId('privacy-offer-heartbeat-fields')).toHaveTextContent(/Anonymous daily heartbeat.*Random installation ID/)
      expect(screen.getByTestId('privacy-kiro-telemetry-offer')).toHaveTextContent(/to Kiro telemetry/)
      expect(screen.getByText(/five fields listed under the intro/)).toBeInTheDocument()
      expect(await readyButton('Turn on performance metrics')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Keep off' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Keep on' })).not.toBeInTheDocument()
      expect(screen.queryByTestId('privacy-turn-off-scope')).not.toBeInTheDocument()
    })

    it('Turn on writes the switch, then records the Kiro telemetry ack', async () => {
      statusOffer()
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Turn on performance metrics'))
      await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true, kiroTelemetry: true }))
      expect(patchConfig).toHaveBeenCalledWith('telemetry.enabled', true)
    })

    it('Turn on that does not take effect records nothing and says nothing changed', async () => {
      statusOffer()
      patchConfig.mockResolvedValue({})
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Turn on performance metrics'))
      expect(await screen.findByText('Your choice could not be saved, so nothing changed. Try again.')).toBeInTheDocument()
      expect(onContinue).not.toHaveBeenCalled()
    })

    it('Turn on whose write fails says nothing changed', async () => {
      statusOffer()
      patchConfig.mockRejectedValue(new Error('500'))
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Turn on performance metrics'))
      expect(await screen.findByText('Your choice could not be saved, so nothing changed. Try again.')).toBeInTheDocument()
      expect(onContinue).not.toHaveBeenCalled()
    })

    it('Turn on whose response is lost after the switch landed does not claim nothing changed', async () => {
      statusOffer()
      patchConfig.mockImplementation(async () => {
        stored.recording = true
        throw new Error('response lost')
      })
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Turn on performance metrics'))
      expect(await screen.findByText('Performance metrics are now on, but your choice could not be fully saved. Keep them on, or turn them off.')).toBeInTheDocument()
      expect(onContinue).not.toHaveBeenCalled()
    })

    it('Turn on whose result cannot be read back says metrics may now be on', async () => {
      statusOffer()
      patchConfig.mockRejectedValue(new Error('response lost'))
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      const button = await readyButton('Turn on performance metrics')
      collectionStatus.mockRejectedValue(new Error('status unreadable'))
      fireEvent.click(button)
      expect(await screen.findByText(/Turn on could not be finished or confirmed, so performance metrics may now be on/)).toBeInTheDocument()
      expect(onContinue).not.toHaveBeenCalled()
    })

    it('Turn on whose acknowledgement fails after the switch landed says so', async () => {
      statusOffer()
      const onContinue = vi.fn().mockRejectedValue(new Error('500'))
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Turn on performance metrics'))
      expect(await screen.findByText('Performance metrics are now on, but your choice could not be fully saved. Keep them on, or turn them off.')).toBeInTheDocument()
      expect(screen.queryByText(/so nothing changed/)).not.toBeInTheDocument()
    })

    it('Keep off writes the switch off and records only the notice version', async () => {
      statusOffer()
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Keep off'))
      await waitFor(() => expect(onContinue).toHaveBeenCalledWith({ noticeVersion: true }))
      expect(patchConfig).toHaveBeenCalledWith('telemetry.enabled', false)
      expect(patchConfig).not.toHaveBeenCalledWith('telemetry.enabled', true)
    })

    it('Keep off whose write fails says nothing changed when still off, and names no Turn off', async () => {
      statusOffer()
      patchConfig.mockRejectedValue(new Error('500'))
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Keep off'))
      expect(await screen.findByText('Your choice could not be saved, so nothing changed. Try again.')).toBeInTheDocument()
      expect(screen.queryByText(/Turn off could not be finished/)).not.toBeInTheDocument()
      expect(onContinue).not.toHaveBeenCalled()
    })

    it('Keep off whose result cannot be read back says so without naming a button', async () => {
      statusOffer()
      patchConfig.mockRejectedValue(new Error('500'))
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      const button = await readyButton('Keep off')
      collectionStatus.mockRejectedValue(new Error('status unreadable'))
      fireEvent.click(button)
      expect(await screen.findByText('Your choice could not be saved or confirmed. Try again.')).toBeInTheDocument()
      expect(screen.queryByText(/Turn off could not be finished/)).not.toBeInTheDocument()
    })

    it('a Turn on that half-landed shows the on form, so Turn off is the way back', async () => {
      statusOffer()
      patchConfig.mockImplementation(async (path: string, value: unknown) => {
        if (path === 'telemetry.enabled') stored.recording = value as boolean
        throw new Error('response lost')
      })
      const onContinue = vi.fn().mockResolvedValue(undefined)
      renderChapter({ onContinue })

      fireEvent.click(await readyButton('Turn on performance metrics'))
      expect(await screen.findByText(/Performance metrics are now on, but your choice could not be fully saved/)).toBeInTheDocument()
      expect(await screen.findByRole('button', { name: 'Turn off' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Keep off' })).not.toBeInTheDocument()
      expect(onContinue).not.toHaveBeenCalled()
    })
  })

  it('renders nothing while closed', () => {
    renderWithProviders(<PrivacyChapter open={false} onContinue={vi.fn()} />)

    expect(screen.queryByRole('heading', { name: 'Performance metrics are on' })).not.toBeInTheDocument()
  })
})
