/**
 * Uploaded sound files in the sound settings: their bounds, how they share
 * the custom-sound limit and name space with tone sounds, and how a category
 * set to one plays it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import {
  CUSTOM_FILE_LIMITS, CUSTOM_TONE_LIMITS, validateCustomFile, validateCustomTone, allCustomSounds,
  loadSoundSettings, playPreset, fileSoundId, fileSoundName, userSoundName, customSoundId, __resetForTests,
  missingFileFallback, useNotificationSound, type SoundSettings,
} from '../hooks/useNotificationSound'
import { MC_NOTIFICATION_EVENT } from '../hooks/notificationEvent'
import { getSoundFile } from '../hooks/customSoundFiles'

vi.mock('../hooks/customSoundFiles', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/customSoundFiles')>()
  return { ...actual, getSoundFile: vi.fn() }
})

const STORAGE_KEY = 'mc-notification-sound'
const ok = { bytes: 1000, seconds: 1 }
const KEY = 'mfz1abcd-1234abcd-5678'
const rec = (key = KEY) => ({ ...ok, key })
const tone = [{ freq: 440, start: 0, dur: 0.2, gain: 1 }]
const store = (v: unknown) => localStorage.setItem(STORAGE_KEY, JSON.stringify(v))

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  vi.mocked(getSoundFile).mockReset()
})

describe('validateCustomFile', () => {
  it('accepts a file within the limits', () => {
    expect(validateCustomFile('Bell', ok)).toEqual([])
    expect(validateCustomFile('Bell', { bytes: CUSTOM_FILE_LIMITS.maxBytes, seconds: CUSTOM_FILE_LIMITS.maxSeconds })).toEqual([])
  })

  it('refuses a record past either limit, or not a record at all', () => {
    expect(validateCustomFile('Bell', { bytes: CUSTOM_FILE_LIMITS.maxBytes + 1, seconds: 1 })).toEqual(['file_size'])
    expect(validateCustomFile('Bell', { bytes: 1000, seconds: CUSTOM_FILE_LIMITS.maxSeconds + 0.1 })).toEqual(['file_length'])
    expect(validateCustomFile('Bell', { bytes: 0, seconds: 0 })).toEqual(['file_size', 'file_length'])
    expect(validateCustomFile('Bell', 'x')).toEqual(['file_size', 'file_length'])
    expect(validateCustomFile('Bell', { bytes: Number.NaN, seconds: '1' })).toEqual(['file_size', 'file_length'])
  })

  it('shares the name rules and the name space with tone sounds', () => {
    expect(validateCustomFile('', ok)).toEqual(['name'])
    expect(validateCustomFile('chime', ok)).toEqual(['name_taken'])
    expect(validateCustomFile('bell', ok, allCustomSounds({ customTones: { Bell: tone } }))).toEqual(['name_taken'])
    expect(validateCustomTone('bell', tone, allCustomSounds({ customFiles: { Bell: ok } }))).toEqual(['name_taken'])
  })

  it('counts both kinds toward one limit', () => {
    const half = CUSTOM_TONE_LIMITS.maxSounds / 2
    const customTones = Object.fromEntries(Array.from({ length: half }, (_, i) => [`t${i}`, tone]))
    const customFiles = Object.fromEntries(Array.from({ length: half }, (_, i) => [`f${i}`, ok]))
    const all = allCustomSounds({ customTones, customFiles })
    expect(validateCustomFile('one more', ok, all)).toEqual(['too_many'])
    expect(validateCustomTone('one more', tone, all)).toEqual(['too_many'])
  })
})

describe('loading sound files from settings', () => {
  it('keeps a valid record and a category set to it', () => {
    store({ perCategory: { all: 'chime', turn: fileSoundId('Bell') }, customFiles: { Bell: { ...rec(), extra: 'x' } } })
    const s = loadSoundSettings()
    expect(s.customFiles).toEqual({ Bell: rec() })
    expect(s.perCategory.turn).toBe('file:Bell')
  })

  it('drops a hand-edited record past the limits, and the category falls back', () => {
    store({ perCategory: { turn: fileSoundId('Big') }, customFiles: { Big: { bytes: CUSTOM_FILE_LIMITS.maxBytes + 1, seconds: 1, key: KEY } } })
    const s = loadSoundSettings()
    expect(s.customFiles).toBeUndefined()
    expect(s.perCategory.turn).toBeUndefined()
  })

  it('drops a category set to a file that has no record', () => {
    store({ perCategory: { turn: fileSoundId('Gone') } })
    expect(loadSoundSettings().perCategory.turn).toBeUndefined()
  })

  it('drops a record whose storage key is missing or not of our shape', () => {
    store({
      perCategory: { turn: fileSoundId('NoKey') },
      customFiles: { NoKey: ok, BadKey: { ...ok, key: 'Door' }, Good: rec() },
    })
    const s = loadSoundSettings()
    expect(Object.keys(s.customFiles ?? {})).toEqual(['Good'])
    expect(s.perCategory.turn).toBeUndefined()
  })

  it('drops files past the shared limit, and a file reusing a tone sound name', () => {
    const customTones = Object.fromEntries(Array.from({ length: CUSTOM_TONE_LIMITS.maxSounds }, (_, i) => [`t${i}`, tone]))
    store({ customTones, customFiles: { extra: rec() } })
    expect(loadSoundSettings().customFiles).toBeUndefined()
    store({ customTones: { Bell: tone }, customFiles: { bell: rec() } })
    const s = loadSoundSettings()
    expect(Object.keys(s.customTones ?? {})).toEqual(['Bell'])
    expect(s.customFiles).toBeUndefined()
  })
})

describe('ids', () => {
  it('a file id and a tone id never read as each other', () => {
    expect(fileSoundName(fileSoundId('Bell'))).toBe('Bell')
    expect(fileSoundName(customSoundId('Bell'))).toBeNull()
    expect(userSoundName(fileSoundId('Bell'))).toBe('Bell')
    expect(userSoundName(customSoundId('Bell'))).toBe('Bell')
    expect(userSoundName('chime')).toBeNull()
  })
})

describe('playing a sound file', () => {
  let built: Array<{ src: string; volume: number; onended: (() => void) | null; onerror: (() => void) | null }>
  const flush = () => new Promise(r => setTimeout(r, 0))

  beforeEach(() => {
    built = []
    vi.stubGlobal('Audio', class {
      src: string
      volume = 1
      onended: (() => void) | null = null
      onerror: (() => void) | null = null
      play = vi.fn(() => Promise.resolve())
      pause = vi.fn()
      constructor(src: string) { this.src = src; built.push(this) }
    })
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:sound-1'),
      revokeObjectURL: vi.fn(),
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('plays the stored bytes, looked up by the record\'s key, from a blob: URL and revokes it when done', async () => {
    store({ customFiles: { Bell: rec() } })
    const blob = new Blob(['x'], { type: 'audio/wav' })
    vi.mocked(getSoundFile).mockResolvedValue(blob)
    playPreset(fileSoundId('Bell'), 0.5)
    await flush()
    expect(getSoundFile).toHaveBeenCalledWith(KEY)
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    expect(built).toHaveLength(1)
    expect(built[0].src).toBe('blob:sound-1')
    expect(built[0].volume).toBe(0.5)
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
    built[0].onended?.()
    built[0].onerror?.()
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:sound-1')
  })

  it('revokes the URL anyway when playback stalls without ending or failing', async () => {
    vi.useFakeTimers()
    try {
      store({ customFiles: { Bell: rec() } })
      vi.mocked(getSoundFile).mockResolvedValue(new Blob(['x']))
      playPreset(fileSoundId('Bell'), 0.5)
      await vi.advanceTimersByTimeAsync(0)
      expect(built).toHaveLength(1)
      await vi.advanceTimersByTimeAsync((CUSTOM_FILE_LIMITS.maxSeconds + 4) * 1000)
      expect(URL.revokeObjectURL).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(2000)
      expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
      // A late `ended` does not revoke twice.
      built[0].onended?.()
      expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('plays nothing when there is no record, the bytes are gone, or the volume is 0', async () => {
    playPreset(fileSoundId('NoRecord'), 0.5)
    await flush()
    expect(getSoundFile).not.toHaveBeenCalled()
    store({ customFiles: { Gone: rec() } })
    vi.mocked(getSoundFile).mockResolvedValue(null)
    playPreset(fileSoundId('Gone'), 0.5)
    await flush()
    expect(built).toHaveLength(0)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    playPreset(fileSoundId('Gone'), 0)
    await flush()
    expect(getSoundFile).toHaveBeenCalledTimes(1)
  })
})

describe('a file sound whose bytes are not in this browser', () => {
  // A tone is heard through WebAudio: an oscillator per tone step. One shared
  // stand-in (the module caches its context), reset per test.
  const ctx = {
    state: 'running', currentTime: 0, destination: {},
    resume: vi.fn(() => Promise.resolve()),
    createOscillator: vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), type: '', frequency: { value: 0 }, onended: null })),
    createGain: vi.fn(() => ({ gain: { setValueCurveAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() })),
  }
  const tonesPlayed = () => ctx.createOscillator.mock.calls.length
  const flush = () => new Promise(r => setTimeout(r, 0))
  const base = (perCategory: SoundSettings['perCategory']): SoundSettings =>
    ({ ...loadSoundSettings(), perCategory })

  beforeEach(() => {
    vi.stubGlobal('AudioContext', vi.fn(function () { return ctx }))
    ctx.createOscillator.mockClear()
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('resolves what the category plays with its file choices treated as unset', () => {
    const door = fileSoundId('Door')
    expect(missingFileFallback('turn', base({ all: 'pulse', turn: door }))).toBe('pulse')
    expect(missingFileFallback('turn', base({ all: door, turn: door }))).toBe('chime')
    expect(missingFileFallback('turn', base({ all: 'none', turn: door }))).toBe('none')
    // Nothing to replace: the same answer the normal resolution gives.
    expect(missingFileFallback('turn', base({ all: 'pop' }))).toBe('pop')
  })

  it('plays the fallback instead, when the bytes are gone or the record is', async () => {
    store({ customFiles: { Gone: rec() } })
    vi.mocked(getSoundFile).mockResolvedValue(null)
    playPreset(fileSoundId('Gone'), 0.5, {}, 'chime')
    await flush()
    expect(tonesPlayed()).toBeGreaterThan(0)
    ctx.createOscillator.mockClear()
    playPreset(fileSoundId('NoRecord'), 0.5, {}, 'chime')
    await flush()
    expect(tonesPlayed()).toBeGreaterThan(0)
  })

  it('plays only the file when its bytes are there, and never follows a file fallback', async () => {
    store({ customFiles: { Here: rec(), Gone: rec() } })
    vi.mocked(getSoundFile).mockResolvedValue(new Blob(['x']))
    vi.stubGlobal('Audio', class { volume = 1; onended = null; onerror = null; play = vi.fn(() => Promise.resolve()); pause = vi.fn() })
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() }))
    playPreset(fileSoundId('Here'), 0.5, {}, 'chime')
    await flush()
    expect(tonesPlayed()).toBe(0)
    vi.mocked(getSoundFile).mockResolvedValue(null)
    playPreset(fileSoundId('Gone'), 0.5, {}, fileSoundId('Here'))
    await flush()
    expect(getSoundFile).toHaveBeenCalledTimes(2)
    expect(tonesPlayed()).toBe(0)
  })

  it('a notification for a category set to it is not silent', async () => {
    store({ perCategory: { all: fileSoundId('Gone') }, customFiles: { Gone: rec() } })
    vi.mocked(getSoundFile).mockResolvedValue(null)
    const { unmount } = renderHook(() => useNotificationSound())
    try {
      window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'cron' } }))
      await flush()
      expect(tonesPlayed()).toBeGreaterThan(0)
    } finally {
      unmount()
    }
  })
})
