import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { PrivacyPanel } from '../pages/settings/PrivacyPanel'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      beaconStatus: vi.fn(),
      patchConfig: vi.fn(),
    },
  }
})

const beaconStatus = vi.mocked(api.beaconStatus)
const patchConfig = vi.mocked(api.patchConfig)

const ON = {
  enabled: true,
  would_send: true,
  reason: 'ready',
  endpoint_configured: true,
  env_override: false,
  env_var: 'KIROCREW_TELEMETRY_DISABLED',
  overlay_override: false,
}

const HEARTBEAT_DISCLOSURE = "Random installation ID · app version · release channel · operating system · CPU architecture · Python minor version · install channel · governance posture · first-run flag"

const HEARTBEAT_FIELDS = [
  'Random installation ID',
  'app version',
  'release channel',
  'operating system',
  'CPU architecture',
  'Python minor version',
  'install channel',
  'governance posture',
  'first-run flag',
] as const

const EXCLUSION_DISCLOSURE = "Prompts, responses, file contents or paths, repository names, credentials, hostnames, usernames. Your IP is not stored."

const CONTROL_COMMANDS = [
  'kirocrew telemetry status',
  'kirocrew telemetry disable',
  'export KIROCREW_TELEMETRY_DISABLED=1',
  "$env:KIROCREW_TELEMETRY_DISABLED = '1'",
  'set KIROCREW_TELEMETRY_DISABLED=1',
] as const

const SHELL_COMMANDS = [
  ['macOS / Linux', 'export KIROCREW_TELEMETRY_DISABLED=1'],
  ['Windows PowerShell', "$env:KIROCREW_TELEMETRY_DISABLED = '1'"],
  ['Windows Command Prompt', 'set KIROCREW_TELEMETRY_DISABLED=1'],
] as const

const TOGGLE_LABEL = 'Send anonymous usage heartbeat'

describe('PrivacyPanel', () => {
  beforeEach(() => {
    beaconStatus.mockReset()
    beaconStatus.mockResolvedValue({ ...ON })
    patchConfig.mockReset()
    patchConfig.mockResolvedValue({})
  })

  it('uses semantic headings for every disclosure section', () => {
    renderWithProviders(<PrivacyPanel />)

    const panel = screen.getByLabelText('Privacy')
    const headings = within(panel).getAllByRole('heading', { level: 3 })

    expect(headings.map(heading => heading.textContent)).toEqual([
      'Anonymous daily heartbeat',
      'Never sent',
      'Stays on your device',
      'Telemetry controls',
    ])
  })

  it('discloses exactly the fixed nine-field heartbeat payload', () => {
    renderWithProviders(<PrivacyPanel />)

    const disclosure = screen.getByText(HEARTBEAT_DISCLOSURE)
    expect(HEARTBEAT_FIELDS).toHaveLength(9)
    for (const field of HEARTBEAT_FIELDS) {
      expect(disclosure).toHaveTextContent(field)
    }
  })

  it('pins the excluded data and IP-retention disclosure', () => {
    renderWithProviders(<PrivacyPanel />)

    expect(screen.getByText(EXCLUSION_DISCLOSURE)).toBeInTheDocument()
  })

  it('shows persistent and labelled cross-platform telemetry controls', () => {
    renderWithProviders(<PrivacyPanel />)

    const controlsHeading = screen.getByRole('heading', { name: 'Telemetry controls' })
    const controlsCard = controlsHeading.parentElement
    expect(controlsCard).not.toBeNull()

    const commands = Array.from(controlsCard!.querySelectorAll('code'))
      .map(command => command.textContent)
    expect(commands).toEqual(CONTROL_COMMANDS)

    for (const [label, command] of SHELL_COMMANDS) {
      const labelElement = within(controlsCard!).getByText(label)
      expect(labelElement.parentElement).toHaveTextContent(command)
    }
  })

  it('reflects the stored beacon state on the opt-out switch', async () => {
    renderWithProviders(<PrivacyPanel />)

    const toggle = await screen.findByRole('switch', { name: TOGGLE_LABEL })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  })

  it('writes telemetry.beacon_enabled=false when switched off', async () => {
    renderWithProviders(<PrivacyPanel />)

    const toggle = await screen.findByRole('switch', { name: TOGGLE_LABEL })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    await userEvent.click(toggle)

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith('telemetry.beacon_enabled', false))
  })

  it('surfaces a save failure and restores the previous state', async () => {
    patchConfig.mockRejectedValue(new Error('nope'))
    renderWithProviders(<PrivacyPanel />)

    const toggle = await screen.findByRole('switch', { name: TOGGLE_LABEL })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    await userEvent.click(toggle)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Couldn't save your telemetry choice. Try again.",
    )
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  })

  it('disables the switch and explains when the env var pins telemetry off', async () => {
    beaconStatus.mockResolvedValue({
      ...ON,
      enabled: false,
      would_send: false,
      reason: 'opted out via KIROCREW_TELEMETRY_DISABLED',
      env_override: true,
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText(/KIROCREW_TELEMETRY_DISABLED is set in this environment/),
    ).toBeInTheDocument()

    const toggle = screen.getByRole('switch', { name: TOGGLE_LABEL })
    await userEvent.click(toggle)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('disables the switch and explains when config.local.json pins the value', async () => {
    // The overlay deep-merges over the file the toggle writes, so a write would
    // be accepted and then silently undone.
    beaconStatus.mockResolvedValue({ ...ON, overlay_override: true })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText(/config\.local\.json overrides this setting/),
    ).toBeInTheDocument()

    const toggle = screen.getByRole('switch', { name: TOGGLE_LABEL })
    await userEvent.click(toggle)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('warns when the stored flag is on but nothing is actually being sent', async () => {
    beaconStatus.mockResolvedValue({
      ...ON,
      would_send: false,
      reason: 'non-default KIROCREW_HOME (dev home / pod / preview)',
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText(/No heartbeat is being sent right now/),
    ).toHaveTextContent('non-default KIROCREW_HOME')
  })
})
