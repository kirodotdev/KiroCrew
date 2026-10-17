import { describe, it, expect, beforeEach } from 'vitest'
import * as sound from '../hooks/useNotificationSound'
import {
  loadSoundSettings,
  presetForKind,
  playPreset,
  __resetForTests,
  type SoundSettings,
} from '../hooks/useNotificationSound'

const STORAGE_KEY = 'mc-notification-sound'

// The shapes below are the ones the module exports for custom sounds. They are
// looked up through the namespace so this file still loads on a build that has
// none of them, and fails on the assertion instead of at import.
type Validate = (name: string, tones: unknown, existing?: Record<string, unknown>) => string[]
const api = sound as unknown as {
  validateCustomTone?: Validate
  customSoundId?: (name: string) => string
  CUSTOM_TONE_LIMITS?: Record<string, number>
}

const myAlert = [
  { freq: 523, start: 0, dur: 0.2, gain: 1 },
  { freq: 784, start: 0.2, dur: 0.3, gain: 0.9 },
]

const oscillators: Array<{ frequency: { value: number } }> = []
const mockCtx = {
  state: 'running',
  currentTime: 0,
  destination: {},
  resume: () => Promise.resolve(),
  createOscillator: () => {
    const o = { connect() {}, disconnect() {}, start() {}, stop() {}, type: '', frequency: { value: 0 }, onended: null }
    oscillators.push(o)
    return o
  },
  createGain: () => ({ gain: { setValueCurveAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {}, disconnect() {} }),
}
;(window as unknown as { AudioContext: unknown }).AudioContext = function () { return mockCtx }

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  oscillators.length = 0
})

describe('custom notification sounds', () => {
  it('a named custom tone array round-trips through the stored settings', () => {
    expect(typeof api.customSoundId).toBe('function')
    const id = api.customSoundId!('myAlert')
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      customTones: { myAlert },
      perCategory: { all: id },
    }))
    const s = loadSoundSettings() as SoundSettings & { customTones?: Record<string, unknown> }
    expect(s.customTones).toEqual({ myAlert })
    expect(s.perCategory.all).toBe(id)
    expect(presetForKind('cron', s)).toBe(id)
  })

  it('plays the custom tones, one oscillator per step at its own frequency', () => {
    const id = api.customSoundId!('myAlert')
    playPreset(id as never, 0.5, { myAlert } as never)
    expect(oscillators.map(o => o.frequency.value)).toEqual([523, 784])
  })

  it('a reference to a custom sound that no longer exists plays nothing and falls back', () => {
    const id = api.customSoundId!('gone')
    playPreset(id as never, 0.5, {} as never)
    expect(oscillators).toHaveLength(0)
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ perCategory: { all: id, cron: id } }))
    const s = loadSoundSettings()
    expect(s.perCategory.all).toBe('chime')
    expect(s.perCategory.cron).toBeUndefined()
  })

  it('validates name and bounds, and accepts the example from the request', () => {
    expect(typeof api.validateCustomTone).toBe('function')
    const v = api.validateCustomTone!
    const L = api.CUSTOM_TONE_LIMITS!
    expect(v('myAlert', myAlert)).toEqual([])
    expect(v('', myAlert)).toContain('name')
    expect(v('a'.repeat(L.maxNameLength + 1), myAlert)).toContain('name')
    expect(v('bad<name>', myAlert)).toContain('name')
    expect(v('chime', myAlert)).toContain('name_taken')
    expect(v('mine', myAlert, { mine: myAlert })).toContain('name_taken')
    expect(v('ok', [])).toContain('count')
    expect(v('ok', Array.from({ length: L.maxTones + 1 }, () => myAlert[0]))).toContain('count')
    expect(v('ok', 'not an array')).toContain('count')
    expect(v('ok', [{ ...myAlert[0], freq: L.minFreq - 1 }])).toContain('freq')
    expect(v('ok', [{ ...myAlert[0], freq: L.maxFreq + 1 }])).toContain('freq')
    expect(v('ok', [{ ...myAlert[0], dur: L.minDur / 2 }])).toContain('dur')
    expect(v('ok', [{ ...myAlert[0], dur: L.maxDur + 1 }])).toContain('dur')
    expect(v('ok', [{ ...myAlert[0], gain: 0 }])).toContain('gain')
    expect(v('ok', [{ ...myAlert[0], gain: 1.5 }])).toContain('gain')
    expect(v('ok', [{ ...myAlert[0], start: -1 }])).toContain('start')
    expect(v('ok', [{ ...myAlert[0], start: L.maxLength }])).toContain('start')
    expect(v('ok', [{ freq: '523', start: 0, dur: 0.2, gain: 1 }])).toContain('freq')
  })

  it('drops invalid stored custom tones on load instead of playing them', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      customTones: { good: myAlert, loud: [{ freq: 523, start: 0, dur: 0.2, gain: 9 }], 'bad<n>': myAlert },
    }))
    const s = loadSoundSettings() as SoundSettings & { customTones?: Record<string, unknown> }
    expect(Object.keys(s.customTones ?? {})).toEqual(['good'])
  })

  it('leaves the built-in presets unchanged', () => {
    expect([...sound.SOUND_PRESETS]).toEqual(['chime', 'ding', 'blip', 'pop', 'pulse'])
    playPreset('chime', 0.5)
    expect(oscillators.map(o => o.frequency.value)).toEqual([1047, 1319, 1568])
  })

  it('keeps a sound named __proto__ as its own entry', () => {
    // JSON.stringify drops a literal __proto__ key, so write the blob by hand.
    localStorage.setItem(STORAGE_KEY, `{"customTones":{"__proto__":${JSON.stringify(myAlert)}},"perCategory":{"all":"custom:__proto__"}}`)
    const s = loadSoundSettings()
    expect(Object.hasOwn(s.customTones ?? {}, '__proto__')).toBe(true)
    expect(s.perCategory.all).toBe('custom:__proto__')
  })
})
