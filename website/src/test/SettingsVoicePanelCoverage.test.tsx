/**
 * Coverage pass over Settings ▸ Voice (`pages/settings/VoicePanel.tsx`).
 *
 * Nothing mounted this panel before, so every branch was unexecuted: the load
 * error + retry arm, the skeleton arm, both provider field sets (the built-in
 * engine and Amazon Polly), the optimistic `voice-config-changed` broadcast, the rollback
 * on a failed save, the engine-compatibility fold when a voice changes, and the
 * live Polly catalogue vs. the offline fallback list.
 *
 * Harness notes:
 *  - `api` is a plain object literal, so `vi.spyOn` on the three voice methods
 *    is enough; the module stays real.
 *  - `SttSettings` is stubbed. It is a sibling panel with its own queries and
 *    its own tests (`SttSettings.streaming.test.tsx`); mounting it here would
 *    add unrelated fetches and duplicate copy to every query in this file.
 *  - `renderWithProviders` supplies a QueryClient with `retry: false`, so a
 *    rejected query surfaces on the first attempt.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { initI18n } from '../i18n'
import { api } from '../api/client'
import { VoicePanel } from '../pages/settings/VoicePanel'

vi.mock('../pages/settings/SttSettings', () => ({
  default: () => <div data-testid="stt-settings-stub" />,
}))

/* ── timers ───────────────────────────────────────────────────────────────── */

// Radix Select (used by every SettingsSelect here) schedules deferred work on
// open/close. On the real clock those callbacks can fire after vitest tears the
// environment down and throw "window is not defined" as an UNHANDLED error —
// every test passing and the run still exiting non-zero. `shouldAdvanceTime`
// keeps the clock moving so `findBy*` behaves as it does with real timers.
beforeEach(async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  await initI18n('en')
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/* ── fixtures ─────────────────────────────────────────────────────────────── */

type VoiceConfig = Awaited<ReturnType<typeof api.voiceConfig>>

function config(over: Record<string, unknown> = {}) {
  return {
    enabled: false,
    provider: 'system',
    voice: 'Ruth',
    engine: 'generative',
    rate: '100%',
    autoSpeak: false,
    aws_profile: '',
    region: '',
    system_voice: '',
    ...over,
  } as VoiceConfig
}

/** Two voices whose engine lists differ, so the compatibility fold is reachable. */
const CATALOGUE = {
  voices: [
    { id: 'Ruth', name: 'Ruth', language: 'English (US)', languageCode: 'en-US', gender: 'Female', engines: ['generative', 'neural'] },
    { id: 'Brian', name: 'Brian', language: 'English (UK)', languageCode: 'en-GB', gender: 'Male', engines: ['standard'] },
  ],
}

/** The host engine's listing, as `/api/voice/system-voices` returns it. */
const SYSTEM_VOICES = {
  available: true,
  voices: [
    { id: 'Alex', name: 'Alex', language: 'en-US' },
    { id: 'Tingting', name: 'Tingting', language: 'zh-CN' },
  ],
}

interface SeedOpts {
  /** Reject the config query instead of resolving it. */
  configFails?: boolean
  /** Never settle the config query, leaving the skeleton on screen. */
  configPending?: boolean
  /** Reject the Polly catalogue query. */
  voicesFail?: boolean
  /** Reject the save, driving the `onError` rollback arm. */
  saveFails?: boolean
  /** Report a host with no built-in speech engine. */
  systemUnavailable?: boolean
  /** Reject the host-voice probe, driving the inline notice + retry. */
  systemVoicesFail?: boolean
}

function seed(over: Record<string, unknown> = {}, opts: SeedOpts = {}) {
  const cfg = config(over)

  const load = vi.spyOn(api, 'voiceConfig')
  if (opts.configFails) load.mockRejectedValue(new Error('config unavailable'))
  else if (opts.configPending) load.mockReturnValue(new Promise<VoiceConfig>(() => {}))
  else load.mockResolvedValue(cfg)

  const voices = vi.spyOn(api, 'voiceVoices')
  if (opts.voicesFail) voices.mockRejectedValue(new Error('no aws cli'))
  else voices.mockResolvedValue(CATALOGUE)

  const systemVoices = vi.spyOn(api, 'voiceSystemVoices')
  if (opts.systemVoicesFail) systemVoices.mockRejectedValue(new Error('probe failed'))
  else systemVoices.mockResolvedValue(
    opts.systemUnavailable ? { available: false, voices: [] } : SYSTEM_VOICES,
  )

  const save = vi.spyOn(api, 'updateVoiceConfig')
  if (opts.saveFails) save.mockRejectedValue(new Error('write refused'))
  else save.mockImplementation(async (patch: object) => ({ ...cfg, ...patch }) as VoiceConfig)

  const view = renderWithProviders(<VoicePanel />)
  return { ...view, cfg, load, voices, systemVoices, save }
}

/** A field's label proves the success branch rendered. */
const field = (name: string | RegExp) => screen.findByText(name)

/** Locate a `SettingsSelect` by its accessible name (several are on screen). */
const select = (name: RegExp) => screen.getByRole('combobox', { name })

/** Radix Select ignores a `change` event on the trigger — open, then click. */
async function pick(name: RegExp, option: string | RegExp) {
  fireEvent.click(select(name))
  const opt = await screen.findByRole('option', { name: option }, { timeout: 5_000 })
  fireEvent.click(opt)
}

/* ── load states ──────────────────────────────────────────────────────────── */

describe('VoicePanel load states', () => {
  it('shows the failure text with a retry that refetches', async () => {
    const { load } = seed({}, { configFails: true })
    await screen.findByText('Failed to load voice config.', undefined, { timeout: 5_000 })

    const calls = load.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(load.mock.calls.length).toBeGreaterThan(calls))
  })

  it('renders the skeleton while the config is in flight', async () => {
    seed({}, { configPending: true })
    // Section headers come from the panel itself, not the loaded branch.
    await screen.findByRole('heading', { name: 'Text-to-Speech' })
    expect(screen.queryByRole('switch', { name: 'Auto-speak Responses' })).toBeNull()
    expect(screen.queryByRole('combobox', { name: /Provider/ })).toBeNull()
  })

  it('mounts the speech-to-text section alongside text-to-speech', async () => {
    seed()
    expect(await screen.findByTestId('stt-settings-stub')).toBeTruthy()
    await screen.findByRole('heading', { name: 'Speech-to-Text' })
  })
})

/* ── System (built-in engine, the default provider) ───────────────────────── */

describe('VoicePanel system fields', () => {
  it('renders the built-in field set and no other provider fields', async () => {
    seed({ provider: 'system' })
    expect(await screen.findByRole('combobox', { name: /^Voice$/ })).toBeTruthy()
    expect(select(/^Speed$/)).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: /^Engine$/ })).toBeNull()
    expect(screen.queryByText('AWS Profile (Amazon Polly)')).toBeNull()
  })

  it('does not fetch the Polly catalogue for a built-in-engine user', async () => {
    const { voices } = seed({ provider: 'system' })
    await screen.findByRole('combobox', { name: /^Voice$/ })
    expect(voices).not.toHaveBeenCalled()
  })

  it('does not probe the host engine while another provider is selected', async () => {
    const { systemVoices } = seed({ provider: 'polly' })
    await field('AWS Profile (Amazon Polly)')
    expect(systemVoices).not.toHaveBeenCalled()
  })

  it('offers the OS default alongside each enumerated voice', async () => {
    seed({ provider: 'system' })
    await screen.findByRole('combobox', { name: /^Voice$/ })
    await waitFor(() => expect(screen.getByText('Operating system default')).toBeTruthy())
    fireEvent.click(select(/^Voice$/))
    expect(await screen.findByRole('option', { name: 'Alex (en-US)' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Tingting (zh-CN)' })).toBeTruthy()
  })

  it('saves the picked built-in voice', async () => {
    const { save } = seed({ provider: 'system' })
    await screen.findByRole('combobox', { name: /^Voice$/ })
    await pick(/^Voice$/, 'Tingting (zh-CN)')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ system_voice: 'Tingting' }))
  })

  it('saves speed as the shared rate percentage, not length_scale', async () => {
    const { save } = seed({ provider: 'system' })
    await screen.findByRole('combobox', { name: /^Voice$/ })
    await pick(/^Speed$/, '120%')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ rate: '120%' }))
  })

  it('reports a missing engine neutrally, not as an error, and offers a neutral re-check rather than a failure Retry', async () => {
    // A probe that SUCCEEDED and found no engine is status, not a failure, so
    // it must not render through the error surface. The re-check is what makes
    // the "install espeak-ng" instruction actionable: the voices query caches
    // for an hour, so following it would otherwise change nothing until a
    // full reload.
    const { systemVoices } = seed({ provider: 'system' }, { systemUnavailable: true })
    const line = await screen.findByTestId('system-engine-absent', undefined, { timeout: 5_000 })
    expect(line.textContent).toMatch(/no built-in speech engine/i)
    expect(line.querySelector('[role="alert"]')).toBeNull()
    await waitFor(() => {
      expect(select(/^Voice$/).getAttribute('data-disabled')).not.toBeNull()
    })
    const calls = systemVoices.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(systemVoices.mock.calls.length).toBeGreaterThan(calls))
  })

  it('disables the re-fetch button while the probe is in flight', async () => {
    // The probe spawns a subprocess, so it takes real time. Leaving the button
    // idle and the notice unchanged makes the click look ignored and invites
    // re-clicks, each spawning another probe.
    const { systemVoices } = seed({ provider: 'system' }, { systemUnavailable: true })
    await screen.findByTestId('system-engine-absent', undefined, { timeout: 5_000 })
    let release: (() => void) | undefined
    systemVoices.mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ available: false, voices: [] }) }),
    )
    const btn = screen.getByRole('button', { name: 'Check again' })
    expect(btn.hasAttribute('disabled')).toBe(false)
    fireEvent.click(btn)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Check again' }).hasAttribute('disabled')).toBe(true),
    )
    release?.()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Check again' }).hasAttribute('disabled')).toBe(false),
    )
  })

  it('surfaces a failed voice probe instead of a silent default-only picker', async () => {
    // Without this the dropdown collapses to one entry and reads as "this host
    // has a single voice" rather than as a failure.
    const { systemVoices } = seed({ provider: 'system' }, { systemVoicesFail: true })
    await screen.findByText(/Could not read the host's voice list/i, undefined, { timeout: 5_000 })
    // A real failure DOES render as an error, and hands off to the agent: this
    // branch mounts no editable field, so the navigation destroys no draft.
    expect(screen.getByRole('button', { name: /ask.*agent/i })).toBeTruthy()
    // One label for one operation: both branches trigger the same refetch, so
    // a returning user does not learn two words for the same act.
    const calls = systemVoices.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(systemVoices.mock.calls.length).toBeGreaterThan(calls))
  })

  it('switches provider to Amazon Polly from the dropdown', async () => {
    const { save } = seed({ provider: 'system' })
    await screen.findByRole('combobox', { name: /^Voice$/ })
    await pick(/^Provider$/, 'Amazon Polly (cloud)')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ provider: 'polly' }))
  })

  it('offers only the built-in engine and Amazon Polly', async () => {
    // Piper was removed (#23154); the dropdown must not offer a provider the
    // gateway no longer accepts.
    seed({ provider: 'system' })
    await screen.findByRole('combobox', { name: /^Voice$/ })
    fireEvent.click(select(/^Provider$/))
    const options = await screen.findAllByRole('option', undefined, { timeout: 5_000 })
    expect(options.map(o => o.textContent)).toEqual(['System (built-in)', 'Amazon Polly (cloud)'])
  })
})

/* ── auto-speak toggle ────────────────────────────────────────────────────── */

describe('VoicePanel auto-speak', () => {
  it('turns voice on implicitly when auto-speak is enabled', async () => {
    const { save } = seed({ autoSpeak: false, enabled: false })
    const toggle = await screen.findByRole('switch', { name: 'Auto-speak Responses' })
    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ autoSpeak: true, enabled: true }))
  })

  it('leaves `enabled` alone when auto-speak is switched off', async () => {
    const { save } = seed({ autoSpeak: true, enabled: true })
    const toggle = await screen.findByRole('switch', { name: 'Auto-speak Responses' })
    fireEvent.click(toggle)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ autoSpeak: false }))
  })

  it('broadcasts the optimistic config on voice-config-changed', async () => {
    const heard: unknown[] = []
    const onChanged = (e: Event) => heard.push((e as CustomEvent).detail)
    window.addEventListener('voice-config-changed', onChanged)
    try {
      seed({ autoSpeak: false })
      const toggle = await screen.findByRole('switch', { name: 'Auto-speak Responses' })
      fireEvent.click(toggle)
      await waitFor(() => expect(heard.length).toBeGreaterThan(0))
      expect(heard[0]).toMatchObject({ autoSpeak: true, enabled: true })
    } finally {
      window.removeEventListener('voice-config-changed', onChanged)
    }
  })
})

/* ── Amazon Polly ─────────────────────────────────────────────────────────── */

describe('VoicePanel polly fields', () => {
  it('renders the Polly field set and fetches the catalogue', async () => {
    const { voices } = seed({ provider: 'polly' })
    await field('AWS Profile (Amazon Polly)')
    await field('AWS Region (Amazon Polly)')
    expect(select(/^Voice$/)).toBeTruthy()
    expect(select(/^Engine$/)).toBeTruthy()
    await waitFor(() => expect(voices).toHaveBeenCalled())
  })

  it('labels catalogue voices as name (locale gender-initial)', async () => {
    seed({ provider: 'polly' })
    await field('AWS Profile (Amazon Polly)')
    fireEvent.click(select(/^Voice$/))
    expect(await screen.findByRole('option', { name: 'Brian (en-GB M)' }, { timeout: 5_000 })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Ruth (en-US F)' })).toBeTruthy()
  })

  it('offers the offline fallback list when the catalogue call fails', async () => {
    seed({ provider: 'polly' }, { voicesFail: true })
    await field('AWS Profile (Amazon Polly)')
    fireEvent.click(select(/^Voice$/))
    expect(await screen.findByRole('option', { name: 'Matthew (US M)' }, { timeout: 5_000 })).toBeTruthy()
  })

  it('folds the engine to the first supported one when the new voice cannot do it', async () => {
    // Brian supports only `standard`, so keeping `generative` would send a
    // combination Polly rejects.
    const { save } = seed({ provider: 'polly', voice: 'Ruth', engine: 'generative' })
    await field('AWS Profile (Amazon Polly)')
    await pick(/^Voice$/, 'Brian (en-GB M)')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ voice: 'Brian', engine: 'standard' }))
  })

  it('keeps the engine when the new voice still supports it', async () => {
    const { save } = seed({ provider: 'polly', voice: 'Brian', engine: 'neural' })
    await field('AWS Profile (Amazon Polly)')
    await pick(/^Voice$/, 'Ruth (en-US F)')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ voice: 'Ruth' }))
  })

  it('limits the engine dropdown to what the selected voice supports', async () => {
    seed({ provider: 'polly', voice: 'Brian', engine: 'standard' })
    await field('AWS Profile (Amazon Polly)')
    fireEvent.click(select(/^Engine$/))
    expect(await screen.findByRole('option', { name: 'standard' }, { timeout: 5_000 })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'generative' })).toBeNull()
  })

  it('saves an engine change', async () => {
    const { save } = seed({ provider: 'polly', voice: 'Ruth', engine: 'generative' })
    await field('AWS Profile (Amazon Polly)')
    await pick(/^Engine$/, 'neural')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ engine: 'neural' }))
  })

  it('saves a speech-rate change', async () => {
    const { save } = seed({ provider: 'polly' })
    await field('AWS Profile (Amazon Polly)')
    await pick(/^Speed$/, '120%')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ rate: '120%' }))
  })

  it('saves trimmed profile and region on blur', async () => {
    const { save } = seed({ provider: 'polly' })
    await field('AWS Profile (Amazon Polly)')

    const profile = screen.getByPlaceholderText('default')
    fireEvent.change(profile, { target: { value: ' work ' } })
    fireEvent.blur(profile)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ aws_profile: 'work' }))

    const region = screen.getByPlaceholderText('us-east-1')
    fireEvent.change(region, { target: { value: ' us-west-2 ' } })
    fireEvent.blur(region)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ region: 'us-west-2' }))
  })
})

/* ── consent-gate re-probe on credential change ───────────────────────────── */

describe('VoicePanel polly consent-gate invalidation', () => {
  // The AwsConsentGate reads its resolved account from ['awsConsent','polly'].
  // Saving a new profile/region must re-probe it, or the gate keeps showing the
  // old account and Confirm 409s on the stale value.
  const consentKey = ['awsConsent', 'polly']
  const seedPolly = () => {
    vi.spyOn(api, 'awsConsent').mockResolvedValue({
      service: 'polly', serviceLabel: 'Amazon Polly', profile: '', credentialSource: '',
      region: '', account: '', arn: '', identityResolved: false, identityDetail: '',
      granted: false, reason: '', revokedOnAccountChange: false, grant: null,
    })
    return seed({ provider: 'polly' })
  }
  const invalidatedKeys = (spy: ReturnType<typeof vi.spyOn>) =>
    spy.mock.calls.map(c => JSON.stringify((c[0] as { queryKey?: unknown[] })?.queryKey))

  it('re-probes the consent gate when the profile changes', async () => {
    const view = seedPolly()
    await field('AWS Profile (Amazon Polly)')
    const spy = vi.spyOn(view.queryClient, 'invalidateQueries')
    const profile = screen.getByPlaceholderText('default')
    fireEvent.change(profile, { target: { value: 'work' } })
    fireEvent.blur(profile)
    await waitFor(() => expect(invalidatedKeys(spy)).toContain(JSON.stringify(consentKey)))
  })

  it('re-probes the consent gate when the region changes', async () => {
    const view = seedPolly()
    await field('AWS Profile (Amazon Polly)')
    const spy = vi.spyOn(view.queryClient, 'invalidateQueries')
    const region = screen.getByPlaceholderText('us-east-1')
    fireEvent.change(region, { target: { value: 'us-west-2' } })
    fireEvent.blur(region)
    await waitFor(() => expect(invalidatedKeys(spy)).toContain(JSON.stringify(consentKey)))
  })

  it('does not re-probe the consent gate on an unrelated save', async () => {
    const view = seedPolly()
    await field('AWS Profile (Amazon Polly)')
    const spy = vi.spyOn(view.queryClient, 'invalidateQueries')
    await pick(/^Speed$/, '120%')
    await waitFor(() => expect(view.save).toHaveBeenCalledWith({ rate: '120%' }))
    expect(invalidatedKeys(spy)).not.toContain(JSON.stringify(consentKey))
  })
})

/* ── save failure ─────────────────────────────────────────────────────────── */

describe('VoicePanel save failure', () => {
  it('surfaces the save error and rolls the local inputs back', async () => {
    const { save } = seed({ provider: 'polly', aws_profile: 'original' }, { saveFails: true })
    await field('AWS Profile (Amazon Polly)')

    const profile = screen.getByPlaceholderText('default')
    fireEvent.change(profile, { target: { value: 'typo' } })
    fireEvent.blur(profile)

    await waitFor(() => expect(save).toHaveBeenCalled())
    await screen.findByText('Failed to save voice config', undefined, { timeout: 5_000 })
    await waitFor(() => expect(screen.getByPlaceholderText('default')).toHaveValue('original'))
  })

  it('dismisses the save error banner', async () => {
    seed({ provider: 'polly' }, { saveFails: true })
    await field('AWS Profile (Amazon Polly)')

    const region = screen.getByPlaceholderText('us-east-1')
    fireEvent.change(region, { target: { value: 'eu-west-1' } })
    fireEvent.blur(region)

    const banner = await screen.findByText('Failed to save voice config', undefined, { timeout: 5_000 })
    expect(banner).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByText('Failed to save voice config')).toBeNull())
  })
})

/* ── Language (edition providers) ────────────────────────────────────────── */

/** A registered edition provider: the only kind that reads `language_code`. */
const EDITION_PROVIDERS = [
  { name: 'system', label: 'system', builtin: true, available: true, reason: null, action: null },
  { name: 'polly', label: 'polly', builtin: true, available: true, reason: null, action: null },
  { name: 'acme', label: 'Acme Speech', builtin: false, available: true, reason: null, action: null },
]
const LANGUAGE_CODES = ['', 'en-US', 'ja-JP', 'zh-CN']

describe('VoicePanel language picker', () => {
  it('offers the served codes with the provider default first, and saves the pick', async () => {
    const { save } = seed({ provider: 'acme', providers: EDITION_PROVIDERS, language_code: '', language_codes: LANGUAGE_CODES })
    const language = await screen.findByRole('combobox', { name: /^Language$/ }, { timeout: 5_000 })
    expect(language).toHaveTextContent('Provider default')

    await pick(/^Language$/, 'Japanese (ja-JP)')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ language_code: 'ja-JP' }))
  })

  it('carries its own setting id, so search highlights it and not the STT Language row', async () => {
    seed({ provider: 'acme', providers: EDITION_PROVIDERS, language_code: '', language_codes: LANGUAGE_CODES })
    const language = await screen.findByRole('combobox', { name: /^Language$/ }, { timeout: 5_000 })
    expect(language.closest('[data-setting-id]')?.getAttribute('data-setting-id')).toBe('voice.language-2')
  })

  it('shows a stored code as the selection', async () => {
    seed({ provider: 'acme', providers: EDITION_PROVIDERS, language_code: 'zh-CN', language_codes: LANGUAGE_CODES })
    const language = await screen.findByRole('combobox', { name: /^Language$/ }, { timeout: 5_000 })
    expect(language).toHaveTextContent('Chinese (zh-CN)')
  })

  it('leaves when a built-in provider is picked, since those speak the voice\'s own language', async () => {
    const { cfg, load } = seed({ provider: 'acme', providers: EDITION_PROVIDERS, language_code: '', language_codes: LANGUAGE_CODES })
    await screen.findByRole('combobox', { name: /^Language$/ }, { timeout: 5_000 })

    // The refetch after the save must read the saved provider, as the gateway would.
    load.mockResolvedValue({ ...cfg, provider: 'system' })
    await pick(/^Provider$/, 'System (built-in)')
    await screen.findByRole('combobox', { name: /^Voice$/ }, { timeout: 5_000 })
    expect(screen.queryByRole('combobox', { name: /^Language$/ })).toBeNull()
  })
})
