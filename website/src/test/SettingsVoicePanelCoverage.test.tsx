/**
 * Coverage pass over Settings ▸ Voice (`pages/settings/VoicePanel.tsx`).
 *
 * Nothing mounted this panel before, so every branch was unexecuted: the load
 * error + retry arm, the skeleton arm, both provider field sets (Piper and
 * Amazon Polly), the optimistic `voice-config-changed` broadcast, the rollback
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
    provider: 'piper',
    voice: 'Ruth',
    engine: 'generative',
    rate: '100%',
    autoSpeak: false,
    aws_profile: '',
    region: '',
    piper_binary: '',
    piper_model: '',
    piper_model_config: '',
    piper_length_scale: 1.0,
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

interface SeedOpts {
  /** Reject the config query instead of resolving it. */
  configFails?: boolean
  /** Never settle the config query, leaving the skeleton on screen. */
  configPending?: boolean
  /** Reject the Polly catalogue query. */
  voicesFail?: boolean
  /** Reject the save, driving the `onError` rollback arm. */
  saveFails?: boolean
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

  const save = vi.spyOn(api, 'updateVoiceConfig')
  if (opts.saveFails) save.mockRejectedValue(new Error('write refused'))
  else save.mockImplementation(async (patch: object) => ({ ...cfg, ...patch }) as VoiceConfig)

  const view = renderWithProviders(<VoicePanel />)
  return { ...view, cfg, load, voices, save }
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

/* ── Piper (default provider) ─────────────────────────────────────────────── */

describe('VoicePanel piper fields', () => {
  it('renders the piper field set and no Polly-only fields', async () => {
    seed({ piper_model: '/m.onnx', piper_binary: '/usr/bin/piper' })
    await field('Piper Model')
    await field('Piper Binary')
    expect(select(/^Speed$/)).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: /^Voice$/ })).toBeNull()
    expect(screen.queryByRole('combobox', { name: /^Engine$/ })).toBeNull()
    expect(screen.queryByText('AWS Profile (Amazon Polly)')).toBeNull()
  })

  it('does not fetch the Polly catalogue for a piper user', async () => {
    const { voices } = seed()
    await field('Piper Model')
    expect(voices).not.toHaveBeenCalled()
  })

  it('seeds the local input state from the loaded config', async () => {
    seed({ piper_model: '/models/en.onnx', piper_binary: '/opt/piper' })
    await field('Piper Model')
    await waitFor(() => {
      expect((screen.getByDisplayValue('/models/en.onnx') as HTMLInputElement).tagName).toBe('INPUT')
    })
    expect(screen.getByDisplayValue('/opt/piper')).toBeTruthy()
  })

  it('saves a trimmed piper model path on blur', async () => {
    const { save } = seed()
    await field('Piper Model')
    const input = screen.getByPlaceholderText('~/piper/en_US-lessac-medium.onnx')
    fireEvent.change(input, { target: { value: '  /models/new.onnx  ' } })
    fireEvent.blur(input)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ piper_model: '/models/new.onnx' }))
  })

  it('saves a trimmed piper binary path on blur', async () => {
    const { save } = seed()
    await field('Piper Binary')
    const input = screen.getByPlaceholderText('(auto-detect)')
    fireEvent.change(input, { target: { value: ' /usr/local/bin/piper ' } })
    fireEvent.blur(input)
    await waitFor(() => expect(save).toHaveBeenCalledWith({ piper_binary: '/usr/local/bin/piper' }))
  })

  it('writes the numeric length_scale behind the friendly speed label', async () => {
    const { save } = seed()
    await field('Piper Model')
    await pick(/^Speed$/, 'Fastest')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ piper_length_scale: 0.7 }))
  })

  it('switches provider to Amazon Polly from the dropdown', async () => {
    const { save } = seed()
    await field('Piper Model')
    await pick(/^Provider$/, 'Amazon Polly (cloud)')
    await waitFor(() => expect(save).toHaveBeenCalledWith({ provider: 'polly' }))
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
    expect(screen.queryByText('Piper Model')).toBeNull()
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
