// Escape-to-stop precedence gate (issue #22315). Both composer editors (the
// textarea keydown and the rich-text editor's Escape command) call this on a
// bare Escape; it stops the running turn only when nothing else in
// the composer owns Escape first. These pin the full precedence matrix.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { RefObject } from 'react'
import { ESCAPE_STOP_ARM_MS, useComposerStopKey, type SteerEscape, type SteerEscapeVerdict, type StopKeyEvent } from '../components/chat-input/stopKey'
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
  const claimed = result.current.stopTurnOnEscape(e, { native, composing })
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

// "Require a second Escape to stop the turn" (issue #23462). Off by default, so
// every test above runs the single-press stop with no stored setting.
describe('useComposerStopKey: a second Escape required', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
  })
  afterEach(() => { vi.useRealTimers() })

  type Hook = { current: ReturnType<typeof useComposerStopKey> }
  function press(result: Hook, e = fakeEvent()): boolean {
    let claimed = false
    act(() => { claimed = result.current.stopTurnOnEscape(e, { native: new KeyboardEvent('keydown', { key: 'Escape' }), composing: false }) })
    return claimed
  }

  it('one Escape arms and sends no cancel; a second inside the window stops the turn', () => {
    const onStop = vi.fn()
    const { result } = renderHook(() => useComposerStopKey(base({ onStop })))
    const first = fakeEvent()
    expect(press(result, first)).toBe(true)
    expect(first.preventDefault).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
    expect(result.current.escapeArmed).toBe(true)
    act(() => { vi.advanceTimersByTime(ESCAPE_STOP_ARM_MS - 1) })
    expect(press(result)).toBe(true)
    expect(onStop).toHaveBeenCalledTimes(1)
    expect(result.current.escapeArmed).toBe(false)
  })

  it('a lapsed window disarms with nothing sent, and the next Escape arms again', () => {
    const onStop = vi.fn()
    const { result } = renderHook(() => useComposerStopKey(base({ onStop })))
    press(result)
    act(() => { vi.advanceTimersByTime(ESCAPE_STOP_ARM_MS) })
    expect(result.current.escapeArmed).toBe(false)
    expect(onStop).not.toHaveBeenCalled()
    press(result)
    expect(onStop).not.toHaveBeenCalled()
    expect(result.current.escapeArmed).toBe(true)
  })

  it('an Escape armed in one session does not stop a turn after switching to another', () => {
    // One composer serves every session, and both turns are running.
    const onStop = vi.fn()
    const { result, rerender } = renderHook(props => useComposerStopKey(props), { initialProps: base({ onStop, slotKey: 'chat-a' }) })
    press(result)
    expect(result.current.escapeArmed).toBe(true)
    rerender(base({ onStop, slotKey: 'chat-b' }))
    expect(result.current.escapeArmed).toBe(false)
    press(result)
    expect(onStop).not.toHaveBeenCalled()
    expect(result.current.escapeArmed).toBe(true)
  })

  it('from the soft cancel on, one Escape runs the press protocol and the arm is dropped', () => {
    // The Stop button sent the soft cancel while an Escape was armed: the hint
    // goes, and the next Escape is the press protocol's (force kill), as it is
    // for the button.
    const onStop = vi.fn()
    const { result, rerender } = renderHook(props => useComposerStopKey(props), { initialProps: base({ onStop }) })
    press(result)
    expect(result.current.escapeArmed).toBe(true)
    rerender(base({ onStop, stopState: 'soft_pending' }))
    expect(result.current.escapeArmed).toBe(false)
    press(result)
    expect(onStop).toHaveBeenCalledTimes(1)
  })
})

// A steer the running turn has not read comes before the stop: each Escape takes
// back the newest one, and the press that takes back the last one goes on as a
// press with none pending would (stop, or arm with a second Escape required).
describe('useComposerStopKey: pending steers', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.useRealTimers(); localStorage.clear() })

  function steerEscape(verdicts: SteerEscapeVerdict[], counts = { offered: 1, reachable: 1 }) {
    const left = [...verdicts]
    return { ...counts, takeBack: vi.fn((): SteerEscapeVerdict => left.shift() ?? 'none'), stop: vi.fn() } satisfies SteerEscape
  }
  type Hook = { current: ReturnType<typeof useComposerStopKey> }
  function press(result: Hook, e = fakeEvent()): boolean {
    let claimed = false
    act(() => { claimed = result.current.stopTurnOnEscape(e, { native: new KeyboardEvent('keydown', { key: 'Escape' }), composing: false }) })
    return claimed
  }

  it('takes back the last steer and stops the turn on one press, through the steer stop', () => {
    const onStop = vi.fn()
    const steer = steerEscape(['last'])
    const { claimed, e } = run(base({ onStop, steerEscape: steer }))
    expect(claimed).toBe(true)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
    expect(steer.takeBack).toHaveBeenCalledTimes(1)
    expect(steer.stop).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('only takes back a steer while others stay pending', () => {
    const onStop = vi.fn()
    const steer = steerEscape(['more'], { offered: 2, reachable: 2 })
    const { claimed } = run(base({ onStop, steerEscape: steer }))
    expect(claimed).toBe(true)
    expect(steer.takeBack).toHaveBeenCalledTimes(1)
    expect(steer.stop).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
  })

  it('two steers: the first press takes one back, the second takes the other back and stops', () => {
    const steer = steerEscape(['more', 'last'], { offered: 2, reachable: 2 })
    const { result } = renderHook(() => useComposerStopKey(base({ steerEscape: steer })))
    press(result)
    expect(steer.stop).not.toHaveBeenCalled()
    press(result)
    expect(steer.takeBack).toHaveBeenCalledTimes(2)
    expect(steer.stop).toHaveBeenCalledTimes(1)
  })

  it('stops through the steer stop when no steer is left to take back', () => {
    const onStop = vi.fn()
    const steer = steerEscape([], { offered: 0, reachable: 0 })
    const { claimed } = run(base({ onStop, steerEscape: steer }))
    expect(claimed).toBe(true)
    expect(steer.takeBack).toHaveBeenCalledTimes(1)
    expect(steer.stop).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('takes nothing back on a held Escape', () => {
    const steer = steerEscape(['last'])
    const e = fakeEvent(true)
    const { claimed } = run(base({ steerEscape: steer }), e)
    expect(claimed).toBe(true)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
    expect(steer.takeBack).not.toHaveBeenCalled()
    expect(steer.stop).not.toHaveBeenCalled()
  })

  it('leaves Escape to every claimant ahead of the turn, and takes nothing back with no turn running', () => {
    const steer = steerEscape(['last', 'last', 'last', 'last', 'last', 'last', 'last', 'last', 'last'])
    expect(run(base({ steerEscape: steer, anyPickerOpenRef: pickerRef(true) })).claimed).toBe(false)
    expect(run(base({ steerEscape: steer }), fakeEvent(), true).claimed).toBe(false)
    const spoken = new KeyboardEvent('keydown', { key: 'Escape' })
    noteEscapeStoppedSpeech(spoken)
    expect(run(base({ steerEscape: steer }), fakeEvent(), false, spoken).claimed).toBe(false)
    expect(run(base({ steerEscape: steer, voiceRecording: true })).claimed).toBe(false)
    expect(run(base({ steerEscape: steer, hasApproval: true })).claimed).toBe(false)
    expect(run(base({ steerEscape: steer, approvalNudgeActive: true })).claimed).toBe(false)
    const onCancelOptimize = vi.fn()
    expect(run(base({ steerEscape: steer, optimizingRef: { current: true }, onCancelOptimize })).claimed).toBe(true)
    expect(onCancelOptimize).toHaveBeenCalledTimes(1)
    expect(run(base({ steerEscape: steer, isRunning: false })).claimed).toBe(false)
    const owner = document.createElement('div')
    owner.setAttribute('data-escape-owner', '')
    document.body.appendChild(owner)
    try {
      expect(run(base({ steerEscape: steer })).claimed).toBe(false)
    } finally {
      document.body.removeChild(owner)
    }
    expect(steer.takeBack).not.toHaveBeenCalled()
    expect(steer.stop).not.toHaveBeenCalled()
  })

  it('takes back the last steer without stopping again while a stop is under way', () => {
    const onStop = vi.fn()
    const steer = steerEscape(['last'])
    const { claimed } = run(base({ onStop, steerEscape: steer, stopState: 'soft_pending' }))
    expect(claimed).toBe(true)
    expect(steer.takeBack).toHaveBeenCalledTimes(1)
    expect(steer.stop).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
  })

  describe('with a second Escape required', () => {
    beforeEach(() => {
      vi.useFakeTimers()
      localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
    })

    it('one steer: the take-back press arms, and the next press stops', () => {
      const steer = steerEscape(['last'])
      const { result } = renderHook(() => useComposerStopKey(base({ steerEscape: steer })))
      expect(press(result)).toBe(true)
      expect(steer.stop).not.toHaveBeenCalled()
      expect(result.current.escapeArmed).toBe(true)
      expect(press(result)).toBe(true)
      expect(steer.stop).toHaveBeenCalledTimes(1)
      expect(result.current.escapeArmed).toBe(false)
    })

    it('two steers: three presses, and only the press after the last take-back stops', () => {
      const steer = steerEscape(['more', 'last'], { offered: 2, reachable: 2 })
      const { result } = renderHook(() => useComposerStopKey(base({ steerEscape: steer })))
      press(result)
      expect(result.current.escapeArmed).toBe(false)
      press(result)
      expect(result.current.escapeArmed).toBe(true)
      expect(steer.stop).not.toHaveBeenCalled()
      press(result)
      expect(steer.takeBack).toHaveBeenCalledTimes(3)
      expect(steer.stop).toHaveBeenCalledTimes(1)
    })

    it('a take-back press opens the window again from its own press', () => {
      const steer = steerEscape(['none', 'last'])
      const { result } = renderHook(() => useComposerStopKey(base({ steerEscape: steer })))
      press(result)
      expect(result.current.escapeArmed).toBe(true)
      act(() => { vi.advanceTimersByTime(ESCAPE_STOP_ARM_MS - 500) })
      press(result)
      act(() => { vi.advanceTimersByTime(ESCAPE_STOP_ARM_MS - 1) })
      expect(result.current.escapeArmed).toBe(true)
      press(result)
      expect(steer.stop).toHaveBeenCalledTimes(1)
    })

    it('a lapsed window after the take-back needs a new arming press', () => {
      const steer = steerEscape(['last'])
      const { result } = renderHook(() => useComposerStopKey(base({ steerEscape: steer })))
      press(result)
      act(() => { vi.advanceTimersByTime(ESCAPE_STOP_ARM_MS) })
      expect(result.current.escapeArmed).toBe(false)
      press(result)
      expect(steer.stop).not.toHaveBeenCalled()
      expect(result.current.escapeArmed).toBe(true)
    })
  })

  describe('the hint', () => {
    const hintOf = (opts: Opts) => renderHook(() => useComposerStopKey(opts)).result.current.escapeHint

    it('says a press takes back the message and stops when that press does both', () => {
      expect(hintOf(base({ steerEscape: steerEscape([]) }))).toBe('take_back_and_stop')
    })

    it('says a press only takes back the message while another steer would stay pending, a stop is under way, or a second Escape is required', () => {
      expect(hintOf(base({ steerEscape: steerEscape([], { offered: 2, reachable: 2 }) }))).toBe('take_back')
      expect(hintOf(base({ steerEscape: steerEscape([], { offered: 1, reachable: 2 }) }))).toBe('take_back')
      expect(hintOf(base({ steerEscape: steerEscape([]), stopState: 'soft_pending' }))).toBe('take_back')
      localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
      expect(hintOf(base({ steerEscape: steerEscape([]) }))).toBe('take_back')
    })

    it('shows nothing for a steer no press can take back yet, or with no turn running', () => {
      expect(hintOf(base({ steerEscape: steerEscape([], { offered: 0, reachable: 1 }) }))).toBeNull()
      expect(hintOf(base({ steerEscape: steerEscape([]), isRunning: false }))).toBeNull()
      expect(hintOf(base())).toBeNull()
    })

    it('shows nothing while a dictation or an approval owns Escape', () => {
      expect(hintOf(base({ steerEscape: steerEscape([]), voiceRecording: true }))).toBeNull()
      expect(hintOf(base({ steerEscape: steerEscape([]), voiceDrainCancellable: true }))).toBeNull()
      expect(hintOf(base({ steerEscape: steerEscape([]), hasApproval: true }))).toBeNull()
      expect(hintOf(base({ steerEscape: steerEscape([]), approvalNudgeActive: true }))).toBeNull()
    })

    it('says "again to stop" while armed, and a steer to take back outranks it', () => {
      vi.useFakeTimers()
      localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
      const steer = steerEscape([], { offered: 0, reachable: 0 })
      const { result, rerender } = renderHook(props => useComposerStopKey(props), { initialProps: base({ steerEscape: steer }) })
      press(result)
      expect(result.current.escapeHint).toBe('again_to_stop')
      rerender(base({ steerEscape: { ...steer, offered: 1, reachable: 1 } }))
      expect(result.current.escapeHint).toBe('take_back')
    })
  })
})
