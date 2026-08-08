import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  loadSoundSettings,
  saveSoundSettings,
  playPreset,
  presetForKind,
  useNotificationSound,
  SOUND_PRESETS,
  __resetForTests,
  type SoundSettings,
} from '../hooks/useNotificationSound'
import { MC_SOUND_SETTINGS_CHANGED_EVENT, MC_NOTIFICATION_EVENT } from '../hooks/notificationEvent'

const STORAGE_KEY = 'mc-notification-sound'

// -- AudioContext mock ---------------------------------------------------------
// Note: the hook module caches a singleton AudioContext. Rather than fight that,
// we install a single shared mock and reset its internals between tests.
interface MockOsc { connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn>; start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; type: string; frequency: { value: number }; onended: (() => void) | null }

const mockCtx = {
  state: 'running' as 'running' | 'suspended' | 'closed',
  currentTime: 0,
  destination: {},
  resume: vi.fn(() => Promise.resolve()),
  createOscillator: vi.fn((): MockOsc => {
    const o: MockOsc = { connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), type: '', frequency: { value: 0 }, onended: null }
    return o
  }),
  createGain: vi.fn(() => ({
    gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
    connect: vi.fn(),
    disconnect: vi.fn(),
  })),
}

;(window as unknown as { AudioContext: unknown }).AudioContext = vi.fn(function () { return mockCtx })

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  mockCtx.state = 'running'
  mockCtx.currentTime = 0
  mockCtx.resume.mockClear()
  mockCtx.resume.mockImplementation(() => Promise.resolve())
  mockCtx.createOscillator.mockClear()
  mockCtx.createGain.mockClear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

// -- loadSoundSettings ---------------------------------------------------------
describe('loadSoundSettings', () => {
  it('returns defaults when localStorage is empty', () => {
    const s = loadSoundSettings()
    expect(s.enabled).toBe(true)
    expect(s.volume).toBe(0.35)
    expect(s.perCategory.all).toBe('chime')
  })

  it('returns defaults when localStorage contains corrupted JSON', () => {
    localStorage.setItem(STORAGE_KEY, '{not valid json')
    const s = loadSoundSettings()
    expect(s.enabled).toBe(true)
    expect(s.volume).toBe(0.35)
  })

  it('merges partial settings with defaults', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: false }))
    const s = loadSoundSettings()
    expect(s.enabled).toBe(false)
    expect(s.volume).toBe(0.35)
    expect(s.perCategory.all).toBe('chime')
  })

  it('clamps volume to [0, 1]', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ volume: 5 }))
    expect(loadSoundSettings().volume).toBe(1)
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ volume: -3 }))
    expect(loadSoundSettings().volume).toBe(0)
  })

  it('rejects invalid preset values in perCategory (prevents runtime TypeError in playPreset)', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      perCategory: { all: 'BOGUS', cron: 'ding' },
    }))
    const s = loadSoundSettings()
    expect(s.perCategory.all).toBe('chime') // fell back to default
    expect(s.perCategory.cron).toBe('ding') // valid preset preserved
  })

  it('rejects unknown category keys in perCategory', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      perCategory: { all: 'ding', bogusCategory: 'chime' },
    }))
    const s = loadSoundSettings()
    expect(s.perCategory.all).toBe('ding')
    expect((s.perCategory as Record<string, unknown>).bogusCategory).toBeUndefined()
  })

  it('ignores non-string preset values', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      perCategory: { all: 123, cron: null },
    }))
    const s = loadSoundSettings()
    expect(s.perCategory.all).toBe('chime') // default
    expect(s.perCategory.cron).toBeUndefined()
  })
})

// -- saveSoundSettings ---------------------------------------------------------
describe('saveSoundSettings', () => {
  it('writes to localStorage and dispatches change event', () => {
    const listener = vi.fn()
    window.addEventListener(MC_SOUND_SETTINGS_CHANGED_EVENT, listener)
    const s: SoundSettings = { enabled: true, volume: 0.5, perCategory: { all: 'ding' } }
    saveSoundSettings(s)
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(s)
    expect(listener).toHaveBeenCalledTimes(1)
    window.removeEventListener(MC_SOUND_SETTINGS_CHANGED_EVENT, listener)
  })
})

// -- presetForKind -------------------------------------------------------------
describe('presetForKind', () => {
  const base: SoundSettings = { enabled: true, volume: 0.5, perCategory: { all: 'chime', cron: 'ding' } }

  it('returns "none" when disabled', () => {
    expect(presetForKind('cron', { ...base, enabled: false })).toBe('none')
  })

  it('returns category-specific preset when set', () => {
    expect(presetForKind('cron', base)).toBe('ding')
  })

  it('falls back to built-in category default when no category-specific preset', () => {
    expect(presetForKind('approval', base)).toBe('pulse')
  })

  it('falls back to "all" for undefined kind', () => {
    expect(presetForKind(undefined, base)).toBe('chime')
  })

  it('falls back to "all" for empty kind', () => {
    expect(presetForKind('', base)).toBe('chime')
  })

  it('ignores unknown kinds (does not cast silently)', () => {
    expect(presetForKind('totally-new-kind', base)).toBe('chime')
  })

  it('falls back to "chime" when perCategory.all is missing', () => {
    expect(presetForKind('anything', { ...base, perCategory: {} })).toBe('chime')
  })

  it('resolves the turn category with override and fallback', () => {
    expect(presetForKind('turn', base)).toBe('chime') // no override -> all
    expect(presetForKind('turn', { ...base, perCategory: { ...base.perCategory, turn: 'blip' } })).toBe('blip')
    expect(presetForKind('turn', { ...base, perCategory: { ...base.perCategory, turn: 'none' } })).toBe('none')
  })
})

// -- playPreset ---------------------------------------------------------------
describe('playPreset', () => {
  it('no-ops for "none"', () => {
    playPreset('none', 0.5)
    expect(mockCtx.createOscillator).not.toHaveBeenCalled()
  })

  it('no-ops when volume <= 0', () => {
    playPreset('chime', 0)
    playPreset('chime', -0.1)
    expect(mockCtx.createOscillator).not.toHaveBeenCalled()
  })

  it('primes resume() when AudioContext is suspended and schedules oscillators after resume succeeds', async () => {
    mockCtx.state = 'suspended'
    // Simulate the browser transitioning to 'running' as part of resume()
    mockCtx.resume.mockImplementation(() => {
      mockCtx.state = 'running'
      return Promise.resolve()
    })
    playPreset('chime', 0.5)
    expect(mockCtx.resume).toHaveBeenCalledTimes(1)
    // Initially no oscillators (scheduling happens in the resume promise callback)
    expect(mockCtx.createOscillator).not.toHaveBeenCalled()
    // After the resume promise resolves, the tones get scheduled
    await Promise.resolve()
    await Promise.resolve()
    expect(mockCtx.createOscillator).toHaveBeenCalled()
  })

  it('does not schedule oscillators if resume() resolves without transitioning to running', async () => {
    mockCtx.state = 'suspended'
    mockCtx.resume.mockImplementation(() => Promise.resolve()) // stays suspended
    playPreset('chime', 0.5)
    await Promise.resolve()
    await Promise.resolve()
    expect(mockCtx.createOscillator).not.toHaveBeenCalled()
  })

  it('bails out when AudioContext is closed (does not throw InvalidStateError)', () => {
    mockCtx.state = 'closed'
    // Should not throw, and should not attempt to schedule oscillators
    expect(() => playPreset('chime', 0.5)).not.toThrow()
    expect(mockCtx.createOscillator).not.toHaveBeenCalled()
    // resume() is NOT called for closed — only for suspended
    expect(mockCtx.resume).not.toHaveBeenCalled()
  })

  it('recovers from closed state on next call (rebuilds AudioContext singleton)', () => {
    const AudioContextSpy = window.AudioContext as unknown as ReturnType<typeof vi.fn>
    const callsBefore = AudioContextSpy.mock.calls.length

    // First call sees 'closed' → should clear the singleton and bail out
    mockCtx.state = 'closed'
    playPreset('chime', 0.5)
    expect(mockCtx.createOscillator).not.toHaveBeenCalled()

    // Next call with a running context should build a fresh AudioContext
    // (singleton was cleared) and actually schedule oscillators.
    mockCtx.state = 'running'
    playPreset('chime', 0.5)
    expect(AudioContextSpy.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(mockCtx.createOscillator).toHaveBeenCalled()
  })

  it('schedules oscillators for a valid preset when running', () => {
    playPreset('ding', 0.5)
    expect(mockCtx.createOscillator).toHaveBeenCalled()
  })

  it('disconnects oscillator and gain when onended fires (memory-leak guard)', () => {
    playPreset('ding', 0.5)
    const osc = mockCtx.createOscillator.mock.results.at(-1)!.value as MockOsc
    const gain = mockCtx.createGain.mock.results.at(-1)!.value as { disconnect: ReturnType<typeof vi.fn> }
    expect(osc.onended).toBeTypeOf('function')
    osc.onended!()
    expect(osc.disconnect).toHaveBeenCalled()
    expect(gain.disconnect).toHaveBeenCalled()
  })
})

// -- useNotificationSound hook: cooldown debounce ------------------------------
describe('useNotificationSound', () => {
  it('debounces notifications within 300ms cooldown', () => {
    let nowMs = 1000
    const perf = vi.spyOn(performance, 'now').mockImplementation(() => nowMs)

    const { unmount } = renderHook(() => useNotificationSound())
    const oscsBefore = mockCtx.createOscillator.mock.calls.length

    const dispatch = () => window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'cron' } }))

    nowMs = 1000; dispatch() // first call → plays
    const after1 = mockCtx.createOscillator.mock.calls.length
    nowMs = 1100; dispatch() // 100ms later → within cooldown, skipped
    const after2 = mockCtx.createOscillator.mock.calls.length
    nowMs = 1500; dispatch() // 500ms after first → past cooldown, plays
    const after3 = mockCtx.createOscillator.mock.calls.length

    expect(after1).toBeGreaterThan(oscsBefore) // first played
    expect(after2).toBe(after1)                // second was debounced
    expect(after3).toBeGreaterThan(after2)     // third played

    perf.mockRestore()
    unmount()
  })

  it('silent notification (preset=none) does not consume the cooldown window', () => {
    saveSoundSettings({ enabled: true, volume: 0.35, perCategory: { all: 'chime', cron: 'none' } })
    let nowMs = 1000
    const perf = vi.spyOn(performance, 'now').mockImplementation(() => nowMs)
    const { unmount } = renderHook(() => useNotificationSound())
    const oscsBefore = mockCtx.createOscillator.mock.calls.length

    nowMs = 1000
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'cron' } }))
    expect(mockCtx.createOscillator.mock.calls.length).toBe(oscsBefore)

    nowMs = 1050
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'approval' } }))
    expect(mockCtx.createOscillator.mock.calls.length).toBeGreaterThan(oscsBefore)

    perf.mockRestore()
    unmount()
  })

  it('unsubscribes on unmount', () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const { unmount } = renderHook(() => useNotificationSound())
    unmount()
    expect(removeSpy).toHaveBeenCalledWith(MC_SOUND_SETTINGS_CHANGED_EVENT, expect.any(Function))
    expect(removeSpy).toHaveBeenCalledWith(MC_NOTIFICATION_EVENT, expect.any(Function))
  })
})

// -- SOUND_PRESETS invariant ---------------------------------------------------
describe('SOUND_PRESETS', () => {
  it('exports the expected preset names', () => {
    expect(SOUND_PRESETS).toEqual(['chime', 'ding', 'blip', 'pop', 'pulse'])
  })
})
