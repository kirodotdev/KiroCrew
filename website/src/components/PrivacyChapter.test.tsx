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

  it('renders nothing while closed', () => {
    renderWithProviders(<PrivacyChapter open={false} onContinue={vi.fn()} />)

    expect(screen.queryByRole('heading', { name: 'Performance metrics are on' })).not.toBeInTheDocument()
  })
})
