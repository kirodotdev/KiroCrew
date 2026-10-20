import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePushToTalk, type VoiceControls } from './usePushToTalk'
import { MAX_HOLD_MS, matchesBindingWithShift, savePttConfig, shiftKeepsDraft, type PttConfig } from '../lib/pushToTalk'

/*
 * Which stops count as the person finishing. `stt.submit_on_release` sends only
 * on a stop whose intent is 'finish', so every path that is not a deliberate
 * finish, a Shift finish included, must reach `stop` with no intent at all.
 */

function makeVoice() {
  const v: VoiceControls = {
    recording: false,
    start: vi.fn(() => { v.recording = true }),
    stop: vi.fn(() => { v.recording = false }),
    cancel: vi.fn(() => { v.recording = false }),
  }
  return v
}

const BARE: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }
const CHORD: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'Space', ctrl: true }, holdMs: 500 }
const SHIFT_CHORD: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'Space', alt: true, shift: true }, holdMs: 500 }
const TOGGLE: PttConfig = { enabled: true, mode: 'toggle', binding: { code: 'AltRight' }, holdMs: 500 }

function key(type: 'keydown' | 'keyup', code: string, init: KeyboardEventInit = {}) {
  act(() => {
    document.dispatchEvent(new KeyboardEvent(type, { code, bubbles: true, cancelable: true, ...init }))
  })
}
const pastThreshold = () => act(() => { vi.advanceTimersByTime(500) })

beforeEach(() => { vi.useFakeTimers(); localStorage.clear() })
afterEach(() => { vi.useRealTimers(); localStorage.clear() })

describe('push-to-talk stop intent', () => {
  it('a hold released by the key is a deliberate finish', () => {
    savePttConfig(BARE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltRight', { altKey: true })
    pastThreshold()
    key('keyup', 'AltRight')
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('a chord hold released with Shift still down keeps a draft', () => {
    savePttConfig(CHORD)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'Space', { ctrlKey: true })
    pastThreshold()
    key('keyup', 'Space', { ctrlKey: true, shiftKey: true })
    expect(voice.stop).toHaveBeenCalledWith(undefined)
  })

  it('pressing Shift during a bare-modifier hold finishes it as a draft', () => {
    savePttConfig(BARE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltRight', { altKey: true })
    pastThreshold()
    key('keydown', 'ShiftLeft', { altKey: true, shiftKey: true })
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.stop).toHaveBeenCalledWith(undefined)
    // The modifier's own release afterwards is not a second stop.
    key('keyup', 'AltRight')
    expect(voice.stop).toHaveBeenCalledTimes(1)
  })

  it('an accidental non-Shift key during a hold commits without sending', () => {
    savePttConfig(BARE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltRight', { altKey: true })
    pastThreshold()
    key('keydown', 'KeyV', { altKey: true })
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.stop).toHaveBeenCalledWith(undefined)
  })

  it('Shift is ignored when the binding itself uses Shift', () => {
    savePttConfig(SHIFT_CHORD)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'Space', { altKey: true, shiftKey: true })
    pastThreshold()
    key('keyup', 'Space', { altKey: true, shiftKey: true })
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('with the shifted stop off, Shift plus the binding is ignored during a latched capture, as before', () => {
    savePttConfig(TOGGLE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltRight', { altKey: true })
    key('keyup', 'AltRight')
    expect(voice.recording).toBe(true)
    key('keydown', 'AltRight', { altKey: true, shiftKey: true })
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.recording).toBe(true)
  })

  it('pressing the key again to stop a latched capture is a finish; with Shift it keeps a draft', () => {
    savePttConfig(TOGGLE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice, { shiftDraftStop: true }))
    key('keydown', 'AltRight', { altKey: true })
    key('keyup', 'AltRight')
    expect(voice.recording).toBe(true)
    key('keydown', 'AltRight', { altKey: true })
    expect(voice.stop).toHaveBeenLastCalledWith('finish')

    key('keyup', 'AltRight')
    key('keydown', 'AltRight', { altKey: true })
    key('keyup', 'AltRight')
    expect(voice.recording).toBe(true)
    // Shift held before the modifier: refused by matchesBinding, accepted as
    // the keep-draft stop because a capture is running.
    key('keydown', 'AltRight', { altKey: true, shiftKey: true })
    expect(voice.stop).toHaveBeenLastCalledWith(undefined)
    expect(voice.recording).toBe(false)
  })

  it('Shift plus the binding does not START a capture', () => {
    savePttConfig(BARE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown', 'AltRight', { altKey: true, shiftKey: true })
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('involuntary stops carry no intent: the hard cap, blur and a hidden page', () => {
    savePttConfig(BARE)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))

    key('keydown', 'AltRight', { altKey: true })
    pastThreshold()
    act(() => { vi.advanceTimersByTime(MAX_HOLD_MS) })
    expect(voice.stop).toHaveBeenLastCalledWith(undefined)

    key('keyup', 'AltRight')
    key('keydown', 'AltRight', { altKey: true })
    pastThreshold()
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.stop).toHaveBeenLastCalledWith(undefined)
    expect(voice.stop).not.toHaveBeenCalledWith('finish')
  })
})

describe('Shift helpers', () => {
  it('shiftKeepsDraft is false for any binding that uses Shift', () => {
    expect(shiftKeepsDraft({ shiftKey: true }, { code: 'AltRight' })).toBe(true)
    expect(shiftKeepsDraft({ shiftKey: true }, { code: 'ShiftRight' })).toBe(false)
    expect(shiftKeepsDraft({ shiftKey: true }, { code: 'Space', alt: true, shift: true })).toBe(false)
    expect(shiftKeepsDraft({ shiftKey: false }, { code: 'AltRight' })).toBe(false)
  })

  it('matchesBindingWithShift matches only the binding plus Shift', () => {
    const e = (init: Partial<KeyboardEvent>) => ({ code: 'Space', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...init }) as KeyboardEvent
    expect(matchesBindingWithShift(e({ ctrlKey: true, shiftKey: true }), { code: 'Space', ctrl: true })).toBe(true)
    expect(matchesBindingWithShift(e({ ctrlKey: true }), { code: 'Space', ctrl: true })).toBe(false)
    expect(matchesBindingWithShift(e({ ctrlKey: true, altKey: true, shiftKey: true }), { code: 'Space', ctrl: true })).toBe(false)
    expect(matchesBindingWithShift(e({ altKey: true, shiftKey: true }), { code: 'Space', alt: true, shift: true })).toBe(false)
  })
})
