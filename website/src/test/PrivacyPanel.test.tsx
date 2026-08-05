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

const HEARTBEAT_DISCLOSURE = "Random installation ID · app version · Python minor version · install channel · first-run flag"

const HEARTBEAT_FIELDS = [
  'Random installation ID',
  'app version',
  'Python minor version',
  'install channel',
  'first-run flag',
] as const

const RECEIPT_DISCLOSURE = "Official app slug · per-app anonymous token (not linkable across apps) · fresh/update flag · Kiro Crew version"

const RECEIPT_FIELDS = [
  'Official app slug',
  'per-app anonymous token',
  'not linkable across apps',
  'fresh/update flag',
  'Kiro Crew version',
] as const

// Fields the payload excludes. Asserted ABSENT, not just omitted from the list
// above: this text is the product's transparency commitment, so a re-added wire
// field that nobody documented — or documentation that outlives the field — is
// the failure this pins. Mirrors the key-set assertion on `beacon._fields` in
// test/test_beacon.py.
const REMOVED_FIELDS = [
  'release channel',
  'operating system',
  'CPU architecture',
  'governance posture',
] as const

const EXCLUSION_DISCLOSURE = "Prompts, responses, files, credentials, hostnames, usernames, or your operating system. Your IP is not stored."

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
      'Official app install receipts',
      'Never sent',
      'Stays on your device',
      'Telemetry controls',
    ])
  })

  it('discloses exactly the fixed five-field heartbeat payload', () => {
    renderWithProviders(<PrivacyPanel />)

    const disclosure = screen.getByText(HEARTBEAT_DISCLOSURE)
    expect(HEARTBEAT_FIELDS).toHaveLength(5)
    for (const field of HEARTBEAT_FIELDS) {
      expect(disclosure).toHaveTextContent(field)
    }
  })

  it('discloses the official-app receipt and its privacy boundary', () => {
    renderWithProviders(<PrivacyPanel />)

    const disclosure = screen.getByText(RECEIPT_DISCLOSURE)
    for (const field of RECEIPT_FIELDS) {
      expect(disclosure).toHaveTextContent(field)
    }
    expect(
      screen.getByText(
        /External registries, local apps, and self-registered apps are never reported/,
      ),
    ).toBeInTheDocument()
  })

  it('no longer claims to send the four removed fields', () => {
    renderWithProviders(<PrivacyPanel />)

    const panel = screen.getByLabelText('Privacy')
    for (const field of REMOVED_FIELDS) {
      // Scoped to the heartbeat disclosure line, not the whole panel: the
      // never-sent copy legitimately NAMES "operating system" as excluded, so a
      // panel-wide absence assertion would contradict the improvement it states.
      expect(screen.getByText(HEARTBEAT_DISCLOSURE)).not.toHaveTextContent(field)
    }
    expect(panel).toBeInTheDocument()
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

  it('disables the switch and names the administrator when policy pins it off', async () => {
    // An enterprise ceiling (capabilities.telemetry). Unlike the env var and the
    // overlay, the user cannot lift this one, and the PATCH route answers 403 —
    // so the control must be inert rather than merely annotated.
    beaconStatus.mockResolvedValue({
      ...ON,
      enabled: false,
      would_send: false,
      reason: 'disabled by governance policy (capabilities.telemetry)',
      governance_override: true,
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText(/administrator's security policy turns this off/),
    ).toBeInTheDocument()

    const toggle = screen.getByRole('switch', { name: TOGGLE_LABEL })
    await userEvent.click(toggle)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('shows only the policy note when policy and the env var both pin it', async () => {
    // Strongest-first: stacking remedies would tell the user to unset an env var
    // that would change nothing while the ceiling stands.
    beaconStatus.mockResolvedValue({
      ...ON,
      enabled: false,
      would_send: false,
      governance_override: true,
      env_override: true,
      overlay_override: true,
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText(/administrator's security policy turns this off/),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/is set in this environment/),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByText(/config\.local\.json overrides this setting/),
    ).not.toBeInTheDocument()
  })

  it('warns when the stored flag is on but nothing is actually being sent', async () => {
    beaconStatus.mockResolvedValue({
      ...ON,
      would_send: false,
      reason: 'non-default KIROCREW_HOME (dev home / pod / preview)',
      reason_code: 'non_default_home',
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText(/non-default data directory/),
    ).toBeInTheDocument()
  })

  it('renders the translated code, never the raw backend reason', async () => {
    // `reason` is untranslated operator prose. Interpolating it would put a
    // developer diagnostic ("already sent today (2026-08-04)") on screen in all
    // 10 languages, so the panel must render the catalog string for the code.
    beaconStatus.mockResolvedValue({
      ...ON,
      would_send: false,
      reason: 'already sent today (2026-08-04)',
      reason_code: 'already_sent_today',
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText("Today's heartbeat has already been sent."),
    ).toBeInTheDocument()
    expect(screen.queryByText(/2026-08-04/)).not.toBeInTheDocument()
    expect(screen.queryByText(/already sent today \(/)).not.toBeInTheDocument()
  })

  it('falls back to a generic note for an unrecognized reason code', async () => {
    // A backend that adds a suppression before the catalog has its key must not
    // render a raw dotted i18n key into the UI.
    beaconStatus.mockResolvedValue({
      ...ON,
      would_send: false,
      reason: 'some future suppression',
      reason_code: 'not_a_known_code',
    })
    renderWithProviders(<PrivacyPanel />)

    expect(
      await screen.findByText('No heartbeat is being sent right now.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/privacyDisclosure\./)).not.toBeInTheDocument()
  })

  it('shows the first-run gate note before the disclosure is acknowledged', async () => {
    // The gateway withholds the very first heartbeat until the user has seen the
    // opt-out; the panel says so rather than looking inertly "on but silent".
    beaconStatus.mockResolvedValue({
      ...ON,
      would_send: false,
      reason: 'the first-run privacy disclosure has not been shown yet',
      reason_code: 'awaiting_privacy_ack',
    })
    renderWithProviders(<PrivacyPanel />)

    expect(await screen.findByText(/Nothing has been sent yet/)).toBeInTheDocument()
  })
})
