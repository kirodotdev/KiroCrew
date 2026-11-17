import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePushToTalk, type VoiceControls } from './usePushToTalk'
import { pttKeyCouldStartMuteChord } from '../lib/dictationMute'
import { SELECTABLE_BARE_CODES, savePttConfig, type PttConfig } from '../lib/pushToTalk'
import { SHORTCUT_OVERRIDES_KEY } from '../lib/shortcutRegistry'

/*
 * Push-to-talk on a lone modifier the mute chord also uses.
 *
 * During a recording a press of the push-to-talk key stops it. The mute chord
 * is only pressed during a recording, and when push-to-talk is one of the
 * chord's own modifiers the chord begins with that same keydown. The stop
 * therefore waits for the release: the key let go alone is the stop, the key
 * joined by another is a chord and the recording carries on.
 *
 * This suite runs as a non-macOS host, where the mute chord is Alt+Shift+Q.
 */

function makeVoice() {
  const v: VoiceControls = {
    recording: true,
    start: vi.fn(() => { v.recording = true }),
    stop: vi.fn(() => { v.recording = false }),
    cancel: vi.fn(() => { v.recording = false }),
  }
  return v
}

const ptt = (code: string): PttConfig => ({ enabled: true, mode: 'hybrid', binding: { code }, holdMs: 500 })

function key(type: 'keydown' | 'keyup', code: string, init: KeyboardEventInit = {}) {
  act(() => {
    document.dispatchEvent(new KeyboardEvent(type, { code, bubbles: true, cancelable: true, ...init }))
  })
}

beforeEach(() => { vi.useFakeTimers(); localStorage.clear() })
afterEach(() => { vi.useRealTimers(); localStorage.clear() })

describe('mute chord pressed with the push-to-talk modifier', () => {
  it('Alt, Shift, Q with Left Alt as push-to-talk leaves the recording running', () => {
    savePttConfig(ptt('AltLeft'))
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice, { shiftDraftStop: true }))
    key('keydown', 'AltLeft', { altKey: true })
    key('keydown', 'ShiftLeft', { altKey: true, shiftKey: true })
    key('keydown', 'KeyQ', { altKey: true, shiftKey: true })
    key('keyup', 'KeyQ', { altKey: true, shiftKey: true })
    key('keyup', 'ShiftLeft', { altKey: true })
    key('keyup', 'AltLeft')
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.cancel).not.toHaveBeenCalled()
    expect(voice.recording).toBe(true)
  })

  it('Shift, Alt, Q leaves it running too, where Alt arrives as the keep-draft stop', () => {
    savePttConfig(ptt('AltLeft'))
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice, { shiftDraftStop: true }))
    key('keydown', 'ShiftLeft', { shiftKey: true })
    key('keydown', 'AltLeft', { altKey: true, shiftKey: true })
    key('keydown', 'KeyQ', { altKey: true, shiftKey: true })
    key('keyup', 'KeyQ', { altKey: true, shiftKey: true })
    key('keyup', 'AltLeft', { shiftKey: true })
    key('keyup', 'ShiftLeft')
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.recording).toBe(true)
  })

  it('the key pressed and released alone still stops, as a finish', () => {
    savePttConfig(ptt('AltLeft'))
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltLeft', { altKey: true })
    expect(voice.stop).not.toHaveBeenCalled()
    key('keyup', 'AltLeft')
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('a window blur before the release carries the stop out', () => {
    savePttConfig(ptt('AltLeft'))
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltLeft', { altKey: true })
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.stop).toHaveBeenCalledTimes(1)
    // The late key-up finds nothing left to do.
    key('keyup', 'AltLeft')
    expect(voice.stop).toHaveBeenCalledTimes(1)
  })

  it('a modifier the chord does not use still stops on the press', () => {
    savePttConfig(ptt('ControlRight'))
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'ControlRight', { ctrlKey: true })
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('follows the chord the user has bound, not the default', () => {
    // Rebound to Ctrl+Shift+M: Left Alt no longer starts it, Right Ctrl does.
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, JSON.stringify({ 'toggle-dictation-mute': { key: 'm', ctrl: true, shift: true } }))
    expect(pttKeyCouldStartMuteChord({ code: 'AltLeft' }, false)).toBe(false)
    expect(pttKeyCouldStartMuteChord({ code: 'ControlRight' }, false)).toBe(true)
    savePttConfig(ptt('AltLeft'))
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltLeft', { altKey: true })
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })
})

describe('which push-to-talk keys can start the default mute chord', () => {
  const starts = (mac: boolean) => SELECTABLE_BARE_CODES.filter(code => pttKeyCouldStartMuteChord({ code }, mac))

  it('Windows and Linux, Alt+Shift+Q: either Alt and Right Shift', () => {
    expect(starts(false)).toEqual(['AltRight', 'AltLeft', 'ShiftRight'])
  })

  it('macOS, Control+Shift+M: Right Control and Right Shift, not the default Right Option', () => {
    expect(starts(true)).toEqual(['ControlRight', 'ShiftRight'])
  })

  it('never a chord binding', () => {
    expect(pttKeyCouldStartMuteChord({ code: 'Space', alt: true, shift: true }, false)).toBe(false)
  })
})
