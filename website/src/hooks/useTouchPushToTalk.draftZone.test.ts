import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CANCEL_THRESHOLD_PX, DRAFT_THRESHOLD_PX, holdZone, useTouchPushToTalk } from './useTouchPushToTalk'
import { type VoiceControls } from './usePushToTalk'
import { savePttConfig, type PttConfig } from '../lib/pushToTalk'

/*
 * The hold bar's completion gestures under `stt.submit_on_release`: a plain
 * release finishes (sends), sideways arms keep-as-draft, up arms cancel. Both
 * zones are armed rather than acted on, so moving back undoes them, and the
 * release position decides.
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

const HYBRID: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }
const X0 = 200
const Y0 = 400
let target: HTMLButtonElement

function pointer(type: string, x = X0, y = Y0) {
  act(() => {
    target.dispatchEvent(new PointerEvent(type, {
      pointerId: 1, pointerType: 'touch', isPrimary: true,
      bubbles: true, cancelable: true, clientX: x, clientY: y,
    }))
  })
}
const hold = () => { pointer('pointerdown'); act(() => { vi.advanceTimersByTime(HYBRID.holdMs) }) }

function mount(voice: VoiceControls, draftZone: boolean) {
  return renderHook(() => useTouchPushToTalk(voice, { target, draftZone }))
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  savePttConfig(HYBRID)
  target = document.createElement('button')
  document.body.appendChild(target)
})
afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  target.remove()
})

describe('holdZone', () => {
  it('needs the full distance, either way sideways, and lets cancel win', () => {
    expect(holdZone(DRAFT_THRESHOLD_PX - 1, 0, true)).toBeNull()
    expect(holdZone(DRAFT_THRESHOLD_PX, 0, true)).toBe('draft')
    expect(holdZone(-DRAFT_THRESHOLD_PX, 0, true)).toBe('draft')
    expect(holdZone(DRAFT_THRESHOLD_PX, -CANCEL_THRESHOLD_PX, true)).toBe('cancel')
    expect(holdZone(DRAFT_THRESHOLD_PX, 0, false)).toBeNull()
    // Downward travel arms nothing.
    expect(holdZone(0, CANCEL_THRESHOLD_PX * 2, true)).toBeNull()
  })
})

describe('touch hold completion', () => {
  it('a plain release is a deliberate finish', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    hold()
    expect(result.current.bar).toBe('holding')
    pointer('pointerup')
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('sliding sideways arms keep-as-draft, and the release keeps a draft', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    hold()
    pointer('pointermove', X0 - DRAFT_THRESHOLD_PX)
    expect(result.current.armedDraft).toBe(true)
    expect(result.current.bar).toBe('armed-draft')
    pointer('pointerup', X0 - DRAFT_THRESHOLD_PX)
    expect(voice.stop).toHaveBeenCalledWith(undefined)
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  it('moving back out of the draft zone disarms it before the release', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    hold()
    pointer('pointermove', X0 + DRAFT_THRESHOLD_PX)
    expect(result.current.armedDraft).toBe(true)
    pointer('pointermove', X0 + 10)
    expect(result.current.armedDraft).toBe(false)
    expect(result.current.bar).toBe('holding')
    pointer('pointerup', X0 + 10)
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('sliding sideways before the press becomes a hold does not promise a draft', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    pointer('pointerdown')
    pointer('pointermove', X0 + DRAFT_THRESHOLD_PX)
    expect(result.current.armedDraft).toBe(false)
    expect(result.current.bar).not.toBe('armed-draft')
    pointer('pointerup', X0 + DRAFT_THRESHOLD_PX)
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('a finger already in the draft zone when the press becomes a hold arms the cue', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    pointer('pointerdown')
    pointer('pointermove', X0 + DRAFT_THRESHOLD_PX)
    expect(result.current.bar).not.toBe('armed-draft')
    act(() => { vi.advanceTimersByTime(HYBRID.holdMs) })
    expect(result.current.bar).toBe('armed-draft')
    pointer('pointerup', X0 + DRAFT_THRESHOLD_PX)
    expect(voice.stop).toHaveBeenCalledWith(undefined)
  })

  it('ordinary finger drift arms nothing', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    hold()
    pointer('pointermove', X0 + 20, Y0 - 20)
    expect(result.current.bar).toBe('holding')
  })

  it('a fast flick with no move event is read from the release position', () => {
    const voice = makeVoice()
    mount(voice, true)
    hold()
    pointer('pointerup', X0 + DRAFT_THRESHOLD_PX + 5)
    expect(voice.stop).toHaveBeenCalledWith(undefined)
  })

  it('cancel still discards, and wins over a diagonal that crosses both', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    hold()
    pointer('pointermove', X0 + DRAFT_THRESHOLD_PX, Y0 - CANCEL_THRESHOLD_PX)
    expect(result.current.bar).toBe('armed-cancel')
    pointer('pointerup', X0 + DRAFT_THRESHOLD_PX, Y0 - CANCEL_THRESHOLD_PX)
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('pointercancel and a hidden page never finish', () => {
    const voice = makeVoice()
    mount(voice, true)
    hold()
    pointer('pointercancel')
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()

    hold()
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    delete (document as unknown as Record<string, unknown>).visibilityState
    expect(voice.stop).toHaveBeenCalledWith(undefined)
    expect(voice.stop).not.toHaveBeenCalledWith('finish')
  })

  it('without the draft zone, sideways is an ordinary release', () => {
    const voice = makeVoice()
    const { result } = mount(voice, false)
    hold()
    pointer('pointermove', X0 + DRAFT_THRESHOLD_PX * 2)
    expect(result.current.armedDraft).toBe(false)
    pointer('pointerup', X0 + DRAFT_THRESHOLD_PX * 2)
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })
})
