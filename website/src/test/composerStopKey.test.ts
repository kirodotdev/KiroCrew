// Escape-to-stop precedence gate (issue #22315). Both composer editors (the
// textarea keydown and the rich-text editor's Escape command) call this on a
// bare Escape; it stops the running turn only when nothing else in
// the composer owns Escape first. These pin the full precedence matrix.
import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { RefObject } from 'react'
import { useComposerStopKey, type StopKeyEvent } from '../components/chat-input/stopKey'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'

type Opts = Parameters<typeof useComposerStopKey>[0]

const pickerRef = (open: boolean): RefObject<boolean> => ({ current: open })

function base(over: Partial<Opts> = {}): Opts {
  return {
    isRunning: true,
    onStop: vi.fn(),
    anyPickerOpenRef: pickerRef(false),
    voiceRecording: false,
    voiceDrainCancellable: false,
    hasApproval: false,
    approvalNudgeActive: false,
    ...over,
  }
}

function fakeEvent(repeat = false) {
  return { repeat, preventDefault: vi.fn() } satisfies StopKeyEvent
}

function run(opts: Opts, e = fakeEvent(), composing = false, native: Event = new KeyboardEvent('keydown', { key: 'Escape' })) {
  const { result } = renderHook(() => useComposerStopKey(opts))
  const claimed = result.current(e, { native, composing })
  return { claimed, e }
}

describe('useComposerStopKey (issue #22315)', () => {
  it('stops the turn and claims the key when a turn is running and nothing else owns Escape', () => {
    const onStop = vi.fn()
    const { claimed, e } = run(base({ onStop }))
    expect(claimed).toBe(true)
    expect(onStop).toHaveBeenCalledTimes(1)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
  })

  it('yields (no stop) when no turn is running', () => {
    const onStop = vi.fn()
    const { claimed, e } = run(base({ isRunning: false, onStop }))
    expect(claimed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('yields when the surface has no onStop', () => {
    const { claimed } = run(base({ onStop: undefined }))
    expect(claimed).toBe(false)
  })

  it('yields during IME composition (Escape cancels the candidate window)', () => {
    const onStop = vi.fn()
    const { claimed, e } = run(base({ onStop }), fakeEvent(), true)
    expect(claimed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('yields when this same Escape already stopped voice read-back', () => {
    // With auto-speak on, the reply is read aloud while the turn streams, and
    // Escape is the stop-speaking shortcut. That capture-phase listener marks
    // the keydown it spent; the gate must leave the turn alone for it, or the
    // press meant for the speech soft-cancels the turn and the next one
    // force-kills it (Opus review on #23463).
    const onStop = vi.fn()
    const native = new KeyboardEvent('keydown', { key: 'Escape' })
    noteEscapeStoppedSpeech(native)
    const { claimed, e } = run(base({ onStop }), fakeEvent(), false, native)
    expect(claimed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
    // The mark is per event: the next, unmarked Escape stops the turn.
    expect(run(base({ onStop })).claimed).toBe(true)
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('yields to an open trigger picker', () => {
    const onStop = vi.fn()
    const { claimed } = run(base({ onStop, anyPickerOpenRef: pickerRef(true) }))
    expect(claimed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('yields to a recording dictation', () => {
    const onStop = vi.fn()
    expect(run(base({ onStop, voiceRecording: true })).claimed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('yields to a draining dictation', () => {
    const onStop = vi.fn()
    expect(run(base({ onStop, voiceDrainCancellable: true })).claimed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('yields to a pending approval and to its mode nudge', () => {
    const a = vi.fn(); const b = vi.fn()
    expect(run(base({ onStop: a, hasApproval: true })).claimed).toBe(false)
    expect(run(base({ onStop: b, approvalNudgeActive: true })).claimed).toBe(false)
    expect(a).not.toHaveBeenCalled(); expect(b).not.toHaveBeenCalled()
  })

  it('yields to an open role="dialog" surface', () => {
    // A dialog, the command bar, or a snip overlay all bind Escape on window
    // and own it over the composer. SessionControlHost is one of these: it
    // is a non-modal role="dialog" that does not steal focus, and its Escape
    // handling (onKeyDown isolates every key EXCEPT Escape; its document
    // listener closes without preventDefault/stopPropagation) deliberately
    // lets Escape reach the document unstopped — so it depends on THIS probe
    // (not its own handler) to keep Escape from also stopping the turn while
    // it's open. Pinned here as a generic role="dialog" with no special
    // marker, which is exactly why narrowing the probe to aria-modal would
    // regress it.
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    try {
      const onStop = vi.fn()
      expect(run(base({ onStop })).claimed).toBe(false)
      expect(onStop).not.toHaveBeenCalled()
    } finally {
      document.body.removeChild(dialog)
    }
  })

  it('still stops the turn when the open dialog opts out with data-escape-passthrough (the floating live-desktop view)', () => {
    // ComputerUseLiveView is a draggable, strictly read-only role="dialog"
    // that coexists ALONGSIDE a focused composer rather than in front of it:
    // it never steals focus and never claims Escape for itself, so the old
    // unscoped probe silently killed Escape-to-stop the whole time it was up
    // (issue #22315, HOLD item 4). It is the ONLY surface that carries this
    // attribute — every other role="dialog" keeps deferring here (see above).
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('data-escape-passthrough', '')
    document.body.appendChild(dialog)
    try {
      const onStop = vi.fn()
      const { claimed, e } = run(base({ onStop }))
      expect(claimed).toBe(true)
      expect(onStop).toHaveBeenCalledTimes(1)
      expect(e.preventDefault).toHaveBeenCalledTimes(1)
    } finally {
      document.body.removeChild(dialog)
    }
  })

  it('yields to a non-dialog surface that marks itself data-escape-owner', () => {
    // The embedded file preview (MarkdownPanel) and the message-search bar own
    // Escape from their own document-level handlers, but carry NO role="dialog"
    // and close WITHOUT preventDefault — so neither the dialog probe nor a
    // defaultPrevented check would see them. They mark themselves
    // data-escape-owner while open; the gate must yield so one Escape does not
    // both dismiss them AND stop the turn (Opus review on #22410).
    const owner = document.createElement('div')
    owner.setAttribute('data-escape-owner', '')
    document.body.appendChild(owner)
    try {
      const onStop = vi.fn()
      expect(run(base({ onStop })).claimed).toBe(false)
      expect(onStop).not.toHaveBeenCalled()
    } finally {
      document.body.removeChild(owner)
    }
  })

  it('swallows a HELD (auto-repeat) Escape without escalating to a force kill', () => {
    // After the first soft cancel a keydown past the 400ms arm window is read
    // as a force kill by the shared stop protocol; OS key-repeat must not
    // trigger that. A repeat is claimed (preventDefault + true) so it reaches
    // nothing else, but onStop is NOT called -- escalation needs a fresh press.
    const onStop = vi.fn()
    const e = fakeEvent(true)
    const { claimed } = run(base({ onStop }), e)
    expect(claimed).toBe(true)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
  })
})

// Escape cancels an in-flight prompt optimize. The optimize is a closer
// claimant than the turn but yields to every other Escape owner.
describe('useComposerStopKey: in-flight optimize', () => {
  const optimizing = (on: boolean): RefObject<boolean> => ({ current: on })

  it('cancels the optimize and does NOT stop the running turn', () => {
    const onStop = vi.fn(); const onCancelOptimize = vi.fn()
    const { claimed, e } = run(base({ onStop, optimizingRef: optimizing(true), onCancelOptimize }))
    expect(claimed).toBe(true)
    expect(onCancelOptimize).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
  })

  it('cancels the optimize when no turn is running', () => {
    const onCancelOptimize = vi.fn()
    const { claimed } = run(base({ isRunning: false, onStop: undefined, optimizingRef: optimizing(true), onCancelOptimize }))
    expect(claimed).toBe(true)
    expect(onCancelOptimize).toHaveBeenCalledTimes(1)
  })

  it('keeps the old behaviour when no optimize is running', () => {
    const onStop = vi.fn(); const onCancelOptimize = vi.fn()
    expect(run(base({ onStop, optimizingRef: optimizing(false), onCancelOptimize })).claimed).toBe(true)
    expect(onStop).toHaveBeenCalledTimes(1)
    expect(onCancelOptimize).not.toHaveBeenCalled()
    expect(run(base({ isRunning: false, onStop, optimizingRef: optimizing(false), onCancelOptimize })).claimed).toBe(false)
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('yields to an open picker, IME composition and a voice read-back stop', () => {
    const onCancelOptimize = vi.fn()
    const opts = (over: Partial<Opts> = {}) => base({ optimizingRef: optimizing(true), onCancelOptimize, ...over })
    expect(run(opts({ anyPickerOpenRef: pickerRef(true) })).claimed).toBe(false)
    expect(run(opts(), fakeEvent(), true).claimed).toBe(false)
    const native = new KeyboardEvent('keydown', { key: 'Escape' })
    noteEscapeStoppedSpeech(native)
    expect(run(opts(), fakeEvent(), false, native).claimed).toBe(false)
    expect(onCancelOptimize).not.toHaveBeenCalled()
  })
})
