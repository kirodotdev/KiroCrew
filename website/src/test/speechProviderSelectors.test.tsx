/**
 * The STT and TTS provider pickers render from the gateway's served provider
 * list, and fall back to today's hardcoded options when an older gateway serves
 * none.
 *
 * `acme` stands for a provider a composed edition registered. The cases pinned
 * here: the fallback keeps today's options in today's order with no new text; a
 * registered provider appears after the built-ins in both pickers; and an
 * unavailable one stays selectable with its reason and a sign-in hint under the
 * picker.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, within } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { initI18n } from '../i18n'
import { api } from '../api/client'
import { VoicePanel } from '../pages/settings/VoicePanel'
import SttSettings from '../pages/settings/SttSettings'
import { parseSpeechProviders, speechProviderNotice } from '../lib/speechProviders'

vi.mock('../pages/settings/SttSettings', async importOriginal => {
  const real = await importOriginal<typeof import('../pages/settings/SttSettings')>()
  // VoicePanel mounts SttSettings below its own card. Stubbed only inside the
  // VoicePanel cases, so each picker is the only Provider combobox on screen.
  return { default: (props: { cardIndex?: number }) => (stubStt ? <div data-testid="stt-stub" /> : <real.default {...props} />) }
})
let stubStt = false

beforeEach(async () => {
  // Radix Select schedules deferred work on open/close; see SettingsVoicePanelCoverage.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  await initI18n('en')
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { enumerateDevices: async () => [] },
  })
})
afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const ACME_READY = { name: 'acme', label: 'Acme Speech', builtin: false, available: true, reason: null, action: null }
const ACME_SIGNED_OUT = {
  ...ACME_READY,
  available: false,
  reason: 'Not signed in to Acme.',
  action: { kind: 'login', label: 'Acme account' },
}
/** A provider whose check timed out: a failure, not a missing prerequisite. */
const ACME_CHECK_FAILED = { ...ACME_READY, available: false, reason: 'the provider did not answer in time', error: true }
const builtin = (name: string) => ({ name, label: name, builtin: true, available: true, reason: null, action: null })

async function optionNames(picker: RegExp): Promise<string[]> {
  fireEvent.click(screen.getByRole('combobox', { name: picker }))
  const opts = await screen.findAllByRole('option', undefined, { timeout: 5_000 })
  const names = opts.map(o => o.textContent ?? '')
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
  return names
}

/* ── the parser ───────────────────────────────────────────────────────────── */

describe('parseSpeechProviders', () => {
  it('returns null when there is no list to render from', () => {
    expect(parseSpeechProviders(undefined)).toBeNull()
    expect(parseSpeechProviders([])).toBeNull()
    expect(parseSpeechProviders('local')).toBeNull()
  })

  it('reads bare names as available built-ins', () => {
    expect(parseSpeechProviders(['local', 'transcribe'])?.map(p => [p.name, p.builtin, p.available]))
      .toEqual([['local', true, true], ['transcribe', true, true]])
  })

  it('drops malformed and repeated entries, and an unknown action kind', () => {
    const out = parseSpeechProviders([
      builtin('local'), { label: 'no name' }, 7, null, builtin('local'),
      { ...ACME_SIGNED_OUT, action: { kind: 'install', label: 'x' } },
    ])
    expect(out?.map(p => p.name)).toEqual(['local', 'acme'])
    expect(out?.[1].action).toBeNull()
  })

  it('reads `error` only when it is exactly true', () => {
    const out = parseSpeechProviders([ACME_CHECK_FAILED, { ...ACME_SIGNED_OUT, error: 'yes' }, ACME_SIGNED_OUT])
    expect(out?.map(p => p.error)).toEqual([true])
    expect(parseSpeechProviders([{ ...ACME_SIGNED_OUT, error: 'yes' }])?.[0].error).toBe(false)
    expect(parseSpeechProviders(['local'])?.[0].error).toBe(false)
  })

  it('gives a notice only for an unavailable registered provider', () => {
    const served = parseSpeechProviders([{ ...builtin('local'), available: false, reason: 'x' }, ACME_SIGNED_OUT])
    expect(speechProviderNotice(served, 'local')).toBeNull()
    expect(speechProviderNotice(served, 'acme')?.reason).toBe('Not signed in to Acme.')
    expect(speechProviderNotice(null, 'acme')).toBeNull()
  })
})

/* ── TTS: Settings ▸ Voice ▸ Text-to-Speech ───────────────────────────────── */

function seedVoice(over: Record<string, unknown>) {
  type VoiceConfig = Awaited<ReturnType<typeof api.voiceConfig>>
  const cfg = {
    enabled: false, provider: 'system', voice: 'Ruth', engine: 'generative', rate: '100%',
    autoSpeak: false, aws_profile: '', region: '', system_voice: '', ...over,
  } as VoiceConfig
  vi.spyOn(api, 'voiceConfig').mockResolvedValue(cfg)
  vi.spyOn(api, 'voiceVoices').mockResolvedValue({ voices: [] })
  vi.spyOn(api, 'voiceSystemVoices').mockResolvedValue({ available: true, voices: [] })
  vi.spyOn(api, 'updateVoiceConfig').mockImplementation(async (patch: object) => ({ ...cfg, ...patch }) as VoiceConfig)
  stubStt = true
  renderWithProviders(<VoicePanel />)
}

const TTS_BUILTINS = ['System (built-in)', 'Amazon Polly (cloud)']

describe('TTS provider picker', () => {
  it('falls back to today\'s options, in order, when the gateway serves no list', async () => {
    seedVoice({})
    await screen.findByRole('combobox', { name: /Provider/ }, { timeout: 5_000 })
    expect(await optionNames(/Provider/)).toEqual(TTS_BUILTINS)
    expect(screen.queryByTestId('tts-provider-notice')).toBeNull()
  })

  it('lists a registered provider after the built-ins', async () => {
    seedVoice({ provider: 'acme', providers: [builtin('system'), builtin('polly'), ACME_READY] })
    await screen.findByRole('combobox', { name: /Provider/ }, { timeout: 5_000 })
    expect(await optionNames(/Provider/)).toEqual([...TTS_BUILTINS, 'Acme Speech'])
    expect(screen.queryByTestId('tts-provider-notice')).toBeNull()
  })

  it('keeps an unavailable provider selected, with its reason and a sign-in hint', async () => {
    seedVoice({ provider: 'acme', providers: [builtin('system'), builtin('polly'), ACME_SIGNED_OUT] })
    const notice = await screen.findByTestId('tts-provider-notice', undefined, { timeout: 5_000 })
    expect(notice.textContent).toContain('Not signed in to Acme.')
    expect(screen.getByTestId('tts-provider-notice-login').textContent).toBe('Sign in with your Acme account to use it.')
    expect(screen.getByRole('combobox', { name: /Provider/ }).textContent).toContain('Acme Speech')
    // A prerequisite is status text, not an error.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a failed check as an error with the agent hand-off', async () => {
    seedVoice({ provider: 'acme', providers: [builtin('system'), builtin('polly'), ACME_CHECK_FAILED] })
    const notice = await screen.findByTestId('tts-provider-notice', undefined, { timeout: 5_000 })
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('the provider did not answer in time')
    expect(within(notice).getByRole('button', { name: /agent/i })).toBeTruthy()
    expect(screen.queryByTestId('tts-provider-notice-login')).toBeNull()
  })
})

/* ── STT: Settings ▸ Voice ▸ Speech-to-Text ───────────────────────────────── */

function seedStt(over: Record<string, unknown>, status: Record<string, unknown> = {}) {
  stubStt = false
  vi.spyOn(api, 'sttConfig').mockResolvedValue({
    enabled: true, provider: 'local', model: 'base', available: true, streaming: false,
    streaming_providers: ['local', 'apple', 'transcribe'], language_codes: ['en-US'], prereqs: [],
    transcribe_languages_min: 2, transcribe_languages_max: 5, ...over,
  } as never)
  vi.spyOn(api, 'sttStatus').mockResolvedValue({
    available: true, code: '', detail: '', models: [], ...status,
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
  } as never)
  vi.spyOn(api, 'awsConsent').mockResolvedValue({ consented: true } as never)
  vi.spyOn(api, 'sttVocabularies').mockResolvedValue({ vocabularies: [] } as never)
  renderWithProviders(<SttSettings />)
}

const STT_PICKER = /^Provider/

describe('STT provider picker', () => {
  it('falls back to today\'s options, in order, when the gateway serves no list', async () => {
    seedStt({})
    await screen.findByRole('combobox', { name: STT_PICKER }, { timeout: 5_000 })
    expect(await optionNames(STT_PICKER)).toEqual(['Local (runs on this machine)', 'Transcribe (AWS)'])
    expect(screen.queryByTestId('stt-provider-notice')).toBeNull()
  })

  it('lists a registered provider after the built-ins', async () => {
    seedStt({ providers: [builtin('local'), builtin('transcribe'), builtin('off'), ACME_READY] })
    await screen.findByRole('combobox', { name: STT_PICKER }, { timeout: 5_000 })
    const names = await optionNames(STT_PICKER)
    expect(names.at(-1)).toBe('Acme Speech')
    expect(names).toHaveLength(4)
  })

  it('keeps an unavailable provider selected, with its reason and a sign-in hint', async () => {
    seedStt({ provider: 'acme', providers: [builtin('local'), builtin('transcribe'), ACME_SIGNED_OUT] })
    const notice = await screen.findByTestId('stt-provider-notice', undefined, { timeout: 5_000 })
    expect(notice.textContent).toContain('Not signed in to Acme.')
    expect(screen.getByTestId('stt-provider-notice-login').textContent).toBe('Sign in with your Acme account to use it.')
  })

  it('says the reason once, under an "unavailable" badge, for the real backend code', async () => {
    // What the STT status endpoint serves for a registered provider whose check() said no.
    seedStt(
      { provider: 'acme', available: false, providers: [builtin('local'), builtin('transcribe'), ACME_SIGNED_OUT] },
      { available: false, code: 'stt_provider_unavailable', detail: 'Not signed in to Acme.' },
    )
    const notice = await screen.findByTestId('stt-provider-notice', undefined, { timeout: 5_000 })
    expect(notice.getAttribute('role')).toBe('status')
    expect(await screen.findByText('unavailable')).toBeTruthy()
    expect(screen.queryByText('not installed')).toBeNull()
    expect(screen.getAllByText('Not signed in to Acme.')).toHaveLength(1)
  })

  it('shows a failed check as an error, with the hand-off while no draft is open', async () => {
    seedStt({ provider: 'acme', providers: [builtin('local'), builtin('transcribe'), ACME_CHECK_FAILED] })
    const notice = await screen.findByTestId('stt-provider-notice', undefined, { timeout: 5_000 })
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('the provider did not answer in time')
    expect(within(notice).getByRole('button', { name: /agent/i })).toBeTruthy()
  })
})
