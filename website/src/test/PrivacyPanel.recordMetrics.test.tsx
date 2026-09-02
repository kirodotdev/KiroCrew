/**
 * Privacy panel: the local metric-recording switch.
 *
 * This is the only in-product control over `telemetry.enabled`, a setting that
 * defaults off, so three properties are load-bearing and pinned here: it writes
 * the key the collector actually reads, it reports the EFFECTIVE state (the env
 * var overrides the config flag inside the collector), and it refuses to offer a
 * write that something else has pinned.
 *
 * It lives in a `pages/settings/*` panel rather than on the Telemetry page it
 * feeds because only panels the settings extractor scans get a registry entry —
 * see `resolveSettingRef.test.ts` for the assertion that the entry exists, which
 * is what turns the Telemetry banner's reference into a deep link.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import { PrivacyPanel } from '../pages/settings/PrivacyPanel'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      beaconStatus: vi.fn(),
      collectionStatus: vi.fn(),
      patchConfig: vi.fn(),
    },
  }
})

const beaconStatus = vi.mocked(api.beaconStatus)
const collectionStatus = vi.mocked(api.collectionStatus)
const patchConfig = vi.mocked(api.patchConfig)

const BEACON_OFF = {
  enabled: false,
  would_send: false,
  reason: 'disabled',
  endpoint_configured: true,
  env_override: false,
  env_var: 'KIROCREW_TELEMETRY_DISABLED',
  overlay_override: false,
}

const collection = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  env_pinned: false,
  env_var: 'KIROCREW_TELEMETRY',
  overlay_override: false,
  otlp_configured: false,
  metrics_dir: '/home/u/.kiro/crew/metrics',
  ...over,
})

const LABEL = 'Record metrics'

function mount(over: Record<string, unknown> = {}) {
  beaconStatus.mockResolvedValue(BEACON_OFF as never)
  collectionStatus.mockResolvedValue(collection(over) as never)
  patchConfig.mockResolvedValue({} as never)
  renderWithProviders(<PrivacyPanel />)
}

describe('PrivacyPanel metric-recording switch', () => {
  beforeEach(() => vi.clearAllMocks())

  it('writes telemetry.enabled — the key the collector reads', async () => {
    mount()
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    await userEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('telemetry.enabled', true))
  })

  it('turns collection back off, so enabling is not a one-way door', async () => {
    mount({ enabled: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    await userEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('telemetry.enabled', false))
  })

  it('reads back the effective state when the env var overrides the config flag', async () => {
    // Collector-side the env var wins, so a switch showing the stored `false`
    // would deny collection that is actually running.
    mount({ enabled: true, env_pinned: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
  })

  it('disables itself and names the variable when an env var pins the setting', async () => {
    mount({ env_pinned: true, env_var: 'KIROCREW_TELEMETRY' })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-disabled', 'true'))
    // Anchored on the note's own wording: the panel also lists the beacon's
    // KIROCREW_TELEMETRY_DISABLED shell commands, which a bare variable-name
    // match would hit instead.
    expect(screen.getByText(/KIROCREW_TELEMETRY environment variable is pinning/)).toBeInTheDocument()
    await userEvent.click(sw)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('disables itself when a config overlay would make the write snap back', async () => {
    mount({ overlay_override: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByText(/config\.local\.json/)).toBeInTheDocument()
    await userEvent.click(sw)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('shows one note, not two, when both the env var and an overlay are set', async () => {
    // The env var is resolved inside the collector and the overlay is merged
    // before the collector ever reads it, so stacking both would offer a remedy
    // that the outer pin makes pointless.
    mount({ env_pinned: true, overlay_override: true })
    await screen.findByRole('switch', { name: LABEL })
    // findByText, not getByText: the note only exists once the status query has
    // settled, and the switch renders before that.
    expect(
      await screen.findByText(/KIROCREW_TELEMETRY environment variable is pinning/),
    ).toBeInTheDocument()
    expect(screen.queryByText(/config\.local\.json/)).not.toBeInTheDocument()
  })

  it('reports a failed save instead of leaving the optimistic position', async () => {
    mount()
    patchConfig.mockRejectedValue(new Error('boom'))
    const sw = await screen.findByRole('switch', { name: LABEL })
    await userEvent.click(sw)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: LABEL })).toHaveAttribute('aria-checked', 'false'),
    )
  })

  it('carries the config key so the deep-link highlight can find the control', async () => {
    // `useSettingHighlight` resolves via data-setting-key; without it the
    // Telemetry banner's link would land on the tab and flash nothing.
    mount()
    await screen.findByRole('switch', { name: LABEL })
    expect(document.querySelector('[data-setting-key="telemetry.enabled"]')).not.toBeNull()
  })

  it('disables ENABLING when an OTLP endpoint makes recording non-local', async () => {
    // `_build_recorder` attaches an OTLP reader when telemetry.otlp_endpoint is
    // set, so turning recording on here would export — which the config route
    // refuses with 409. The switch must not offer that write at all.
    mount({ otlp_configured: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-disabled', 'true'))
    expect(await screen.findByText(/telemetry\.otlp_endpoint is set/)).toBeInTheDocument()
    await userEvent.click(sw)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('still lets an exporting host turn recording OFF', async () => {
    // Reachable: an endpoint added after enabling, or enabled via the CLI/env with
    // one already set. Disabling is exactly what such a host most needs, and the
    // config route allows it — so the control must not be dead in that state.
    mount({ enabled: true, otlp_configured: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(sw).not.toHaveAttribute('aria-disabled', 'true')
    await userEvent.click(sw)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('telemetry.enabled', false))
  })

  it('says metrics are being exported while recording on an endpoint-configured host', async () => {
    // The state where export is actually happening is the one a reader most needs
    // named, so the note is not conditional on the switch being disabled.
    mount({ enabled: true, otlp_configured: true })
    await screen.findByRole('switch', { name: LABEL })
    expect(await screen.findByText(/being sent off this machine/)).toBeInTheDocument()
  })

  it('never claims locality in the description on a host that exports', async () => {
    // The description is the full-weight line a skimming user reads before
    // flipping the switch; "on this machine" there would be a false claim.
    mount({ otlp_configured: true })
    await screen.findByRole('switch', { name: LABEL })
    // Wait for the status query to land before asserting on the swap.
    expect(await screen.findByText(/send them to the collector/)).toBeInTheDocument()
    expect(
      screen.queryByText('Collect performance metrics on this machine.'),
    ).not.toBeInTheDocument()
  })

  it('shows the egress fact even when an env var also pins the switch', async () => {
    // A pin reason and the egress fact answer different questions. Collapsing them
    // into one priority chain let the pin hide the fact that metrics leave the box.
    mount({ enabled: true, env_pinned: true, env_var: 'KIROCREW_TELEMETRY', otlp_configured: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    // Both notes are announced, not just the first. Assert through the idrefs
    // rather than by text: the panel also prints KIROCREW_TELEMETRY_DISABLED in
    // its CLI section, so a text query would be ambiguous.
    await waitFor(() => {
      const ids = (sw.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
      expect(ids).toHaveLength(2)
    })
    const ids = (sw.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
    const texts = ids.map(id => document.getElementById(id)?.textContent ?? '')
    expect(texts.some(t => /KIROCREW_TELEMETRY/.test(t))).toBe(true)
    expect(texts.some(t => /being sent off this machine/.test(t))).toBe(true)
  })

  it('explains a 409 refusal instead of telling the user to try again', async () => {
    // "Try again" can never succeed against the egress gate.
    patchConfig.mockRejectedValueOnce(new ApiError(409, 'telemetry.otlp_endpoint is set'))
    mount({})
    const sw = await screen.findByRole('switch', { name: LABEL })
    await userEvent.click(sw)
    expect(await screen.findByRole('alert')).toHaveTextContent(/would start sending metrics/)
    expect(screen.queryByText(/Try again/)).not.toBeInTheDocument()
  })

  it('announces WHY it is disabled, not just that it is', async () => {
    // A disabled switch whose explanation is not associated with it reads to a
    // screen reader as a dead control with no reason given.
    mount({ env_pinned: true })
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-disabled', 'true'))
    const describedBy = sw.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    const note = document.getElementById(describedBy as string)
    expect(note?.textContent ?? '').toMatch(/KIROCREW_TELEMETRY environment variable/)
  })

  it('drops the description link when nothing is pinning the switch', async () => {
    mount()
    const sw = await screen.findByRole('switch', { name: LABEL })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    expect(sw).not.toHaveAttribute('aria-describedby')
  })
})
