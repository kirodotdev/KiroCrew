import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePointerHoldToTalk } from '../../hooks/usePointerHoldToTalk'
import { type VoiceControls } from '../../hooks/usePushToTalk'
import { spliceDictationText } from '../../lib/dictationText'
import { savePttConfig, type PttConfig } from '../../lib/pushToTalk'
import type { ComposerControl, ComposerSelection } from '../composerControl'

vi.mock('../../store', () => ({ useAppSelector: () => null }))

import { useDictationControls } from './voice'

/*
 * The composer's dictation caret and the pointer hold, together. jsdom has no
 * native drag-select: drift is simulated by widening the selection, firing the
 * `select` the textarea gets (the composer records the caret on it) and the
 * document `selectionchange`. What it cannot prove is the browser's own order
 * of those events under a held button, or the rich editor's state catching up
 * with the DOM selection.
 */

const ON: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500, pointerHold: true, pointerHoldMs: 500 }
const DRAFT = 'hello brave new world'
const PRESS = 5

let target: HTMLDivElement
let textarea: HTMLTextAreaElement

function makeVoice(): VoiceControls {
  const v: VoiceControls = {
    recording: false,
    start: vi.fn(() => { v.recording = true }),
    stop: vi.fn(() => { v.recording = false }),
    cancel: vi.fn(() => { v.recording = false }),
  }
  return v
}

/** The composer's textarea control, as `useComposerEngine` builds it. */
const control: ComposerControl = {
  focus: () => textarea.focus(),
  getRootElement: () => textarea,
  getSelection: () => ({ start: textarea.selectionStart, end: textarea.selectionEnd }),
  setSelection: (start, end = start) => textarea.setSelectionRange(start, end),
}

function mountComposer() {
  const voiceCaretRef = { current: null as ComposerSelection | null }
  const voicePendingCaretRef = { current: null as number | null }
  const voice = makeVoice()
  const hook = renderHook(({ value }) => {
    const dictation = useDictationControls({
      composerControl: () => control, value, anyPickerOpenRef: { current: false },
      voiceCaretRef, voicePendingCaretRef, voiceDictationPanel: false, voiceRecording: false,
      voiceDrainCancellable: false, voiceError: undefined, voiceSampleRef: undefined, voiceTranscribing: false,
      onVoiceCancel: undefined, onVoiceToggle: undefined,
    })
    usePointerHoldToTalk(voice, { target, onCaretHeld: dictation.holdCaret })
    return dictation
  }, { initialProps: { value: DRAFT } })
  // The textarea's onSelect / onMouseUp handler.
  textarea.addEventListener('select', () => hook.result.current.recordCaret())
  return { ...hook, voice, voiceCaretRef }
}

function pointer(on: EventTarget, type: string, dy = 0) {
  act(() => {
    on.dispatchEvent(new PointerEvent(type, {
      pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0,
      bubbles: true, cancelable: true, clientX: 100, clientY: 400 + dy,
    }))
  })
}

/** The drag-select the held button makes: the range, the `select` the
 *  composer records on, then the document event. */
function drift(start: number, end: number) {
  act(() => {
    textarea.setSelectionRange(start, end)
    textarea.dispatchEvent(new Event('select'))
    document.dispatchEvent(new Event('selectionchange'))
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  savePttConfig(ON)
  target = document.createElement('div')
  textarea = document.createElement('textarea')
  target.append(textarea)
  document.body.appendChild(target)
  textarea.value = DRAFT
  textarea.setSelectionRange(PRESS, PRESS)
})

afterEach(() => {
  vi.useRealTimers()
  target.remove()
})

describe('pointer hold and the dictation caret', () => {
  it('a hold in the middle of a typed draft, a 10 px drift and a release keep both halves of the draft', () => {
    const { voice, voiceCaretRef } = mountComposer()
    pointer(textarea, 'pointerdown')
    act(() => { vi.advanceTimersByTime(ON.pointerHoldMs) })
    pointer(document, 'pointermove', 10)
    // A 10 px drift down selects across a line.
    drift(PRESS, DRAFT.length)
    pointer(document, 'pointerup', 10)
    expect(voice.stop).toHaveBeenCalledWith('finish')
    expect(voiceCaretRef.current).toEqual({ start: PRESS, end: PRESS })
    const spliced = spliceDictationText(DRAFT, 'and', voiceCaretRef.current).value
    expect(spliced.startsWith('hello')).toBe(true)
    expect(spliced.endsWith(' brave new world')).toBe(true)
    expect(spliced).toBe('hello and brave new world')
  })

  it('during a hold refuses a range from the composer\'s own caret recording, and takes ranges again after release', () => {
    const { result, voiceCaretRef, rerender } = mountComposer()
    pointer(textarea, 'pointerdown')
    act(() => { vi.advanceTimersByTime(ON.pointerHoldMs) })
    expect(voiceCaretRef.current).toEqual({ start: PRESS, end: PRESS })
    // Every way the composer reads the live selection, against a range.
    act(() => { textarea.setSelectionRange(PRESS, PRESS + 6) })
    act(() => { result.current.recordCaret() })
    act(() => { result.current.publishLexicalSelection({ start: 0, end: 11 }) })
    rerender({ value: DRAFT })
    expect(voiceCaretRef.current).toEqual({ start: PRESS, end: PRESS })
    // A collapsed caret still gets through.
    act(() => { result.current.publishLexicalSelection({ start: 11, end: 11 }) })
    expect(voiceCaretRef.current).toEqual({ start: 11, end: 11 })
    pointer(document, 'pointerup')
    act(() => { textarea.setSelectionRange(2, 9) })
    act(() => { result.current.recordCaret() })
    expect(voiceCaretRef.current).toEqual({ start: 2, end: 9 })
  })

  it('leaves the caret recording alone for an ordinary click and drag', () => {
    const { voiceCaretRef } = mountComposer()
    pointer(textarea, 'pointerdown')
    drift(PRESS, PRESS + 3)
    pointer(document, 'pointerup')
    expect(voiceCaretRef.current).toEqual({ start: PRESS, end: PRESS + 3 })
  })
})
