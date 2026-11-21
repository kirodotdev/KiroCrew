import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ARM_MOVE_PX, CANCEL_MOVE_PX, usePointerHoldToTalk } from './usePointerHoldToTalk'
import { type VoiceControls } from './usePushToTalk'
import { MAX_HOLD_MS, savePttConfig, type PttConfig } from '../lib/pushToTalk'

/** A recording-state-tracking stand-in for useVoiceInput. */
function makeVoice(overrides: Partial<VoiceControls> = {}) {
  const calls: string[] = []
  const v: VoiceControls & { calls: string[] } = {
    calls,
    recording: false,
    start: vi.fn(() => { calls.push('start'); v.recording = true }),
    stop: vi.fn(() => { calls.push('stop'); v.recording = false }),
    cancel: vi.fn(() => { calls.push('cancel'); v.recording = false }),
    ...overrides,
  }
  return v
}

const ON: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500, pointerHold: true, pointerHoldMs: 500 }
const ORIGIN = { x: 100, y: 400 }

/** The text column the hook binds to, holding the textarea a press lands in. */
let target: HTMLDivElement
let textarea: HTMLTextAreaElement
/** A sibling control in the column that is NOT the message box. */
let button: HTMLButtonElement

function pointer(on: EventTarget, type: string, init: PointerEventInit = {}) {
  act(() => {
    on.dispatchEvent(new PointerEvent(type, {
      pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0,
      bubbles: true, cancelable: true, clientX: ORIGIN.x, clientY: ORIGIN.y, ...init,
    }))
  })
}
const down = (init: PointerEventInit = {}) => pointer(textarea, 'pointerdown', init)
const moveBy = (dx: number, dy = 0) => pointer(document, 'pointermove', { clientX: ORIGIN.x + dx, clientY: ORIGIN.y + dy })
const up = (init: PointerEventInit = {}) => pointer(document, 'pointerup', init)
const pointerCancel = () => pointer(document, 'pointercancel')

function mount(voice: VoiceControls, disabled = false) {
  return renderHook(() => usePointerHoldToTalk(voice, { target, disabled }))
}

const passThreshold = () => act(() => { vi.advanceTimersByTime(ON.pointerHoldMs) })

function hide() {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'hidden' as DocumentVisibilityState,
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  savePttConfig(ON)
  target = document.createElement('div')
  textarea = document.createElement('textarea')
  button = document.createElement('button')
  target.append(textarea, button)
  document.body.appendChild(target)
})

afterEach(() => {
  vi.useRealTimers()
  target.remove()
  delete (document as unknown as { visibilityState?: unknown }).visibilityState
})

describe('usePointerHoldToTalk', () => {
  // The whole point of the departure from the touch hook: a press on a text
  // box is first of all a caret placement, and the microphone must not be
  // touched for one.
  it('opens capture only once the press has held past the cutoff', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    expect(result.current.phase).toBe('arming')
    expect(voice.start).not.toHaveBeenCalled()
    passThreshold()
    expect(result.current.phase).toBe('holding')
    expect(result.current.holding).toBe(true)
    expect(result.current.owns).toBe(true)
    expect(voice.start).toHaveBeenCalledTimes(1)
  })

  it('commits a release at the spot it was held on as a deliberate finish', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down(); passThreshold(); up()
    expect(voice.stop).toHaveBeenCalledWith('finish')
    expect(voice.cancel).not.toHaveBeenCalled()
    expect(result.current.phase).toBe('idle')
    expect(result.current.owns).toBe(false)
  })

  it.each([
    { path: 'release', finishGesture: () => up() },
    { path: 'move past the cancel distance', finishGesture: () => moveBy(CANCEL_MOVE_PX) },
    { path: 'window blur', finishGesture: () => act(() => { window.dispatchEvent(new Event('blur')) }) },
  ] as const)('relinquishes without touching a capture another input ended on $path', ({ finishGesture }) => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down(); passThreshold()

    // The keyboard binding finishes the shared stream while this pointer is
    // still held. Recording ends immediately; its committed final may drain.
    voice.stop('finish')
    finishGesture()

    expect(voice.calls).toEqual(['start', 'stop'])
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.cancel).not.toHaveBeenCalled()
    expect(result.current.phase).toBe('idle')
    expect(result.current.owns).toBe(false)
  })

  it('treats a release before the cutoff as an ordinary click: no capture, nothing to discard', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    act(() => { vi.advanceTimersByTime(ON.pointerHoldMs - 1) })
    up()
    expect(voice.calls).toEqual([])
    expect(result.current.phase).toBe('idle')
    // The dropped timer must not fire late and open a recording with no press
    // behind it.
    act(() => { vi.advanceTimersByTime(ON.pointerHoldMs) })
    expect(voice.start).not.toHaveBeenCalled()
  })

  describe('movement cancels', () => {
    it('hands an arming press back to the text box once it travels like a drag-select', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down()
      moveBy(ARM_MOVE_PX)
      expect(result.current.phase).toBe('idle')
      passThreshold()
      expect(voice.start).not.toHaveBeenCalled()
    })

    it('tolerates the drift of a hand holding still while arming', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down()
      moveBy(ARM_MOVE_PX - 1)
      passThreshold()
      expect(result.current.phase).toBe('holding')
      expect(voice.start).toHaveBeenCalledTimes(1)
    })

    it('discards a hold at once when the pointer leaves the spot it was held on', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down(); passThreshold()
      moveBy(0, CANCEL_MOVE_PX)
      expect(voice.cancel).toHaveBeenCalledTimes(1)
      expect(voice.stop).not.toHaveBeenCalled()
      expect(result.current.phase).toBe('idle')
      expect(result.current.owns).toBe(false)
      // The release that follows is nobody's: the gesture is already over.
      up({ clientY: ORIGIN.y + CANCEL_MOVE_PX })
      expect(voice.calls).toEqual(['start', 'cancel'])
    })

    it('keeps a hold through the larger drift of speaking for a while', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down(); passThreshold()
      moveBy(CANCEL_MOVE_PX - 1)
      expect(result.current.phase).toBe('holding')
      up({ clientX: ORIGIN.x + CANCEL_MOVE_PX - 1 })
      expect(voice.stop).toHaveBeenCalledWith('finish')
    })

    // A final move can be coalesced into the release that follows it, so the
    // release position is what decides, as in the touch hook.
    it('discards when the release itself lands past the cancel distance with no move before it', () => {
      const voice = makeVoice()
      mount(voice)
      down(); passThreshold()
      up({ clientX: ORIGIN.x + CANCEL_MOVE_PX })
      expect(voice.cancel).toHaveBeenCalledTimes(1)
      expect(voice.stop).not.toHaveBeenCalled()
    })
  })

  describe('what does not arm', () => {
    it('nothing, while the setting is off', () => {
      savePttConfig({ ...ON, pointerHold: false })
      const voice = makeVoice()
      const { result } = mount(voice)
      down(); passThreshold()
      expect(result.current.phase).toBe('idle')
      expect(voice.start).not.toHaveBeenCalled()
    })

    it('nothing, while disabled', () => {
      const voice = makeVoice()
      const { result } = mount(voice, true)
      down(); passThreshold()
      expect(result.current.phase).toBe('idle')
      expect(voice.start).not.toHaveBeenCalled()
    })

    // Touch has its own hold mode with a cancel zone sized for a thumb.
    it('a touch press', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down({ pointerType: 'touch' }); passThreshold()
      expect(result.current.phase).toBe('idle')
    })

    it('a secondary button, or a press with a modifier held', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      for (const init of [{ button: 2 }, { shiftKey: true }, { ctrlKey: true }, { altKey: true }, { metaKey: true }] as PointerEventInit[]) {
        down(init); passThreshold()
        expect(result.current.phase).toBe('idle')
      }
      expect(voice.start).not.toHaveBeenCalled()
    })

    it('a press in the column that is not on the message box', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      pointer(button, 'pointerdown'); passThreshold()
      expect(result.current.phase).toBe('idle')
      expect(voice.start).not.toHaveBeenCalled()
    })

    it('a press on the rich editor root arms like one on the textarea', () => {
      const voice = makeVoice()
      const editor = document.createElement('div')
      editor.setAttribute('contenteditable', 'true')
      target.appendChild(editor)
      const { result } = mount(voice)
      pointer(editor, 'pointerdown'); passThreshold()
      expect(result.current.phase).toBe('holding')
    })

    // Unlike the touch bar, a press here is a caret placement, not a stop: the
    // mic button or the key owns a recording they opened.
    it('a press while a recording opened elsewhere is live leaves that recording alone', () => {
      const voice = makeVoice({ recording: true })
      const { result } = mount(voice)
      down(); passThreshold(); up()
      expect(result.current.phase).toBe('idle')
      expect(voice.calls).toEqual([])
    })
  })

  describe('the cutoff window re-checks start conditions', () => {
    it('hands the press back if the composer became disabled during the wait', () => {
      const voice = makeVoice()
      const { result, rerender } = renderHook(
        ({ disabled }) => usePointerHoldToTalk(voice, { target, disabled }),
        { initialProps: { disabled: false } },
      )
      down()
      rerender({ disabled: true })
      passThreshold()
      expect(result.current.phase).toBe('idle')
      expect(voice.start).not.toHaveBeenCalled()
    })

    it('hands the press back if the microphone started recording during the wait (the key, or another composer)', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down()
      // Something else opened the shared mic inside the cutoff window.
      voice.recording = true
      passThreshold()
      expect(result.current.phase).toBe('idle')
      expect(voice.start).not.toHaveBeenCalled()
      // The gesture owns nothing, so a release does not touch that recording.
      up()
      expect(voice.stop).not.toHaveBeenCalled()
      expect(voice.cancel).not.toHaveBeenCalled()
    })

    // The key's startup can still be in flight at the cutoff: `recording` is not
    // yet true, and the composer refuses the second start without returning a
    // promise. The gesture must notice the refusal rather than hold a session
    // it never opened.
    it('backs out when its start is refused because another startup is still in flight', () => {
      const voice = makeVoice({ start: vi.fn(() => { voice.calls.push('start-refused') }) })
      const { result } = mount(voice)
      down(); passThreshold()
      expect(voice.start).toHaveBeenCalledTimes(1)
      expect(result.current.phase).toBe('idle')
      expect(result.current.owns).toBe(false)
      // The key's capture goes live afterwards; this gesture's release must not
      // stop, discard or send it.
      voice.recording = true
      up()
      expect(voice.stop).not.toHaveBeenCalled()
      expect(voice.cancel).not.toHaveBeenCalled()
      // The hard cap was never armed for a refused start.
      act(() => { vi.advanceTimersByTime(MAX_HOLD_MS) })
      expect(voice.stop).not.toHaveBeenCalled()
    })
  })

  describe('the system takes the gesture', () => {
    it('pointercancel discards a hold', () => {
      const voice = makeVoice()
      mount(voice)
      down(); passThreshold(); pointerCancel()
      expect(voice.cancel).toHaveBeenCalledTimes(1)
      expect(voice.stop).not.toHaveBeenCalled()
    })

    it('a window blur mid-hold commits without an intent, so it never sends', () => {
      const voice = makeVoice()
      mount(voice)
      down(); passThreshold()
      act(() => { window.dispatchEvent(new Event('blur')) })
      expect(voice.stop).toHaveBeenCalledTimes(1)
      expect(vi.mocked(voice.stop).mock.calls[0][0]).toBeUndefined()
    })

    it('a window blur while arming drops the press without touching the microphone', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down()
      act(() => { window.dispatchEvent(new Event('blur')) })
      expect(result.current.phase).toBe('idle')
      passThreshold()
      expect(voice.calls).toEqual([])
    })

    it('hiding the page commits a hold without an intent, so it never sends', () => {
      const voice = makeVoice()
      mount(voice)
      down(); passThreshold()
      hide()
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      expect(voice.stop).toHaveBeenCalledTimes(1)
      expect(vi.mocked(voice.stop).mock.calls[0][0]).toBeUndefined()
    })

    it('hiding the page drops an arming press without touching the microphone', () => {
      const voice = makeVoice()
      const { result } = mount(voice)
      down()
      hide()
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      expect(result.current.phase).toBe('idle')
      passThreshold()
      expect(voice.calls).toEqual([])
    })

    it('the hard cap commits a hold whose release never arrives', () => {
      const voice = makeVoice()
      mount(voice)
      down(); passThreshold()
      act(() => { vi.advanceTimersByTime(MAX_HOLD_MS) })
      expect(voice.stop).toHaveBeenCalledTimes(1)
    })

    it('unmounting mid-hold discards it', () => {
      const voice = makeVoice()
      const { unmount } = mount(voice)
      down(); passThreshold()
      unmount()
      expect(voice.cancel).toHaveBeenCalledTimes(1)
      expect(voice.stop).not.toHaveBeenCalled()
    })
  })

  describe('a startup still in flight', () => {
    it('a release before the microphone is granted cancels rather than stopping', async () => {
      let resolve!: () => void
      const voice = makeVoice({ start: vi.fn(() => new Promise<void>(r => { resolve = r })) })
      mount(voice)
      down(); passThreshold()
      // Capture was requested but has not been granted: `recording` is false.
      up()
      expect(voice.cancel).toHaveBeenCalledTimes(1)
      expect(voice.stop).not.toHaveBeenCalled()
      // The abandoned startup resolving later claims nothing.
      await act(async () => { resolve(); await Promise.resolve() })
      expect(voice.stop).not.toHaveBeenCalled()
    })

    it('a failed startup returns the machine to idle', async () => {
      let reject!: (e: unknown) => void
      const voice = makeVoice({ start: vi.fn(() => new Promise<void>((_, r) => { reject = r })) })
      const { result } = mount(voice)
      down(); passThreshold()
      expect(result.current.phase).toBe('holding')
      await act(async () => { reject(new Error('denied')); await Promise.resolve() })
      expect(result.current.phase).toBe('idle')
      expect(result.current.owns).toBe(false)
      expect(voice.cancel).toHaveBeenCalledTimes(1)
    })
  })
})
