import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CANCEL_THRESHOLD_PX, SEND_SIDE_PX, holdZone, useTouchPushToTalk } from './useTouchPushToTalk'
import { type VoiceControls } from './usePushToTalk'
import { savePttConfig, type PttConfig } from '../lib/pushToTalk'

/*
 * The hold bar's send zone, offered when a plain release only transcribes
 * (`stt.submit_on_release` off): the up zone splits, so up and to the right
 * sends and any other upward slide still cancels. Armed rather than acted on,
 * and read from the release position, like the other zones.
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
const UP = Y0 - CANCEL_THRESHOLD_PX
const RIGHT = X0 + SEND_SIDE_PX
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

function mount(voice: VoiceControls, sendZone = true) {
  return renderHook(() => useTouchPushToTalk(voice, { target, sendZone }))
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

describe('holdZone with a send zone', () => {
  it('splits the up zone: right of the lean sends, everything else cancels', () => {
    expect(holdZone(SEND_SIDE_PX, -CANCEL_THRESHOLD_PX, false, true)).toBe('send')
    expect(holdZone(SEND_SIDE_PX - 1, -CANCEL_THRESHOLD_PX, false, true)).toBe('cancel')
    expect(holdZone(0, -CANCEL_THRESHOLD_PX, false, true)).toBe('cancel')
    expect(holdZone(-SEND_SIDE_PX, -CANCEL_THRESHOLD_PX, false, true)).toBe('cancel')
    // Rightward travel alone is not a send: the finger has to go up as well.
    expect(holdZone(SEND_SIDE_PX * 4, -(CANCEL_THRESHOLD_PX - 1), false, true)).toBeNull()
    // Without the zone, up-right is still the cancel it always was.
    expect(holdZone(SEND_SIDE_PX, -CANCEL_THRESHOLD_PX, false, false)).toBe('cancel')
  })
})

describe('touch hold send zone', () => {
  it('sliding up and right arms send, and the release sends', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    hold()
    pointer('pointermove', RIGHT, UP)
    expect(result.current.armedSend).toBe(true)
    expect(result.current.armedCancel).toBe(false)
    expect(result.current.bar).toBe('armed-send')
    pointer('pointerup', RIGHT, UP)
    expect(voice.stop).toHaveBeenCalledWith('send')
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  it('sliding up and left still cancels', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    hold()
    pointer('pointermove', X0 - SEND_SIDE_PX, UP)
    expect(result.current.bar).toBe('armed-cancel')
    pointer('pointerup', X0 - SEND_SIDE_PX, UP)
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('leaning back left out of the send zone turns it into a cancel', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    hold()
    pointer('pointermove', RIGHT, UP)
    expect(result.current.bar).toBe('armed-send')
    pointer('pointermove', X0, UP)
    expect(result.current.armedSend).toBe(false)
    expect(result.current.bar).toBe('armed-cancel')
    pointer('pointerup', X0, UP)
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('moving back down disarms the send, and a plain release only finishes', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    hold()
    pointer('pointermove', RIGHT, UP)
    pointer('pointermove', RIGHT, Y0)
    expect(result.current.armedSend).toBe(false)
    expect(result.current.bar).toBe('holding')
    pointer('pointerup', RIGHT, Y0)
    expect(voice.stop).toHaveBeenCalledWith('finish')
  })

  it('reads the release position when the move into the zone was coalesced away', () => {
    const voice = makeVoice()
    mount(voice)
    hold()
    pointer('pointerup', RIGHT, UP)
    expect(voice.stop).toHaveBeenCalledWith('send')
  })

  it('a tap that ends in the send zone discards and promises nothing', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    pointer('pointerdown')
    pointer('pointermove', RIGHT, UP)
    // Only a hold can send, so the bar must not promise one yet.
    expect(result.current.armedSend).toBe(false)
    expect(result.current.bar).not.toBe('armed-send')
    pointer('pointerup', RIGHT, UP)
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('without the send zone, up and right cancels as before', () => {
    const voice = makeVoice()
    const { result } = mount(voice, false)
    hold()
    pointer('pointermove', RIGHT, UP)
    expect(result.current.armedSend).toBe(false)
    expect(result.current.bar).toBe('armed-cancel')
    pointer('pointerup', RIGHT, UP)
    expect(voice.cancel).toHaveBeenCalledTimes(1)
  })
})
