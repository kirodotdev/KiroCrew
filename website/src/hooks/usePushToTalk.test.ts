import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePushToTalk, type VoiceControls } from './usePushToTalk'
import { MAX_HOLD_MS, PTT_STORAGE_KEY, savePttConfig, type PttConfig } from '../lib/pushToTalk'

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

const MAC_ALT_RIGHT: PttConfig = { mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }

function down(code: string, init: KeyboardEventInit = {}) {
  act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { code, bubbles: true, cancelable: true, ...init }))
  })
}
function up(code: string, init: KeyboardEventInit = {}) {
  act(() => {
    document.dispatchEvent(new KeyboardEvent('keyup', { code, bubbles: true, ...init }))
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
})
afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
})

describe('hybrid mode', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  // The anti-clipping invariant, and the reason capture does not wait for the
  // threshold: `getUserMedia` costs 50-200ms and the streaming path adds a ~2-3s
  // Transcribe handshake on top, so starting at the threshold put ALL of that in
  // front of the opening syllable. One session covers the whole gesture.
  it('opens capture on the keydown, before the tap/hold question is settled', () => {
    const voice = makeVoice()
    const { result } = renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start'])
    expect(result.current.phase).toBe('arming')

    // Crossing the threshold promotes the SAME session — a second start() would
    // be swallowed by useVoiceInput's re-entrancy guard and would not re-capture
    // the audio already in this one.
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.calls).toEqual(['start'])
    expect(voice.start).toHaveBeenCalledTimes(1)
    expect(result.current.holding).toBe(true)
  })

  it('adopts the same session when a tap latches, without a second start', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(120) })   // under the 500ms threshold
    up('AltRight')

    // The latch inherits the capture that has been running since keydown, so the
    // word the user opened with is inside it. Still exactly one session, still
    // running — a tap in hybrid mode means "record until I press again".
    expect(voice.start).toHaveBeenCalledTimes(1)
    expect(voice.recording).toBe(true)
    expect(voice.calls).toEqual(['start'])
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  it('stops on release after a hold', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  it('latches on a tap released before the threshold', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(200) })
    up('AltRight')
    // Tap = start and STAY recording; no stop.
    expect(voice.calls).toEqual(['start'])
    expect(voice.recording).toBe(true)
  })

  it('a second tap ends the latched recording', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true }); act(() => { vi.advanceTimersByTime(200) }); up('AltRight')
    expect(voice.recording).toBe(true)
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start', 'stop'])
    expect(voice.recording).toBe(false)
  })

  // Auto-repeat fires keydown ~30x/sec while held; only the first may arm.
  it('ignores auto-repeat keydowns', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    for (let i = 0; i < 10; i++) down('AltRight', { altKey: true, repeat: true })
    expect(voice.start).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.start).toHaveBeenCalledTimes(1)
  })
})

describe('push-to-talk mode', () => {
  beforeEach(() => savePttConfig({ ...MAC_ALT_RIGHT, mode: 'ptt' }))

  // Capture opens on the keydown (so a real hold keeps its opening word), which
  // means a sub-threshold tap in this mode has a live session to throw away. The
  // invariant is that nothing is KEPT: discard, never commit.
  it('a tap discards the capture it opened', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    up('AltRight')
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.recording).toBe(false)
  })

  it('a hold records and release stops', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.calls).toEqual(['start', 'stop'])
  })
})

describe('toggle mode', () => {
  beforeEach(() => savePttConfig({ ...MAC_ALT_RIGHT, mode: 'toggle' }))

  it('starts immediately with no arming delay and no pre-warm', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start'])
    up('AltRight')
    expect(voice.calls).toEqual(['start'])
  })

  it('the next press stops', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true }); up('AltRight')
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start', 'stop'])
  })
})

describe('binding discrimination', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('ignores the other side of the same modifier', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltLeft', { altKey: true })
    expect(voice.calls).toEqual([])
  })

  it('ignores the bound key when another modifier family is also held', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true, ctrlKey: true })
    expect(voice.calls).toEqual([])
  })

  it('leaves a bare modifier keydown un-prevented', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    const ev = new KeyboardEvent('keydown', { code: 'AltRight', altKey: true, bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(false)
  })

  // A chord's primary key WOULD type a character or scroll the page.
  it('claims a chord binding keydown', () => {
    savePttConfig({ mode: 'hybrid', binding: { code: 'Space', alt: true, shift: true }, holdMs: 500 })
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    const ev = new KeyboardEvent('keydown', { code: 'Space', altKey: true, shiftKey: true, bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(true)
    expect(voice.calls).toEqual(['start'])
  })

  it('does not fire from inside an embedded terminal', () => {
    const term = document.createElement('div')
    term.className = 'xterm'
    document.body.appendChild(term)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    act(() => {
      term.dispatchEvent(new KeyboardEvent('keydown', { code: 'AltRight', altKey: true, bubbles: true, cancelable: true }))
    })
    expect(voice.calls).toEqual([])
    term.remove()
  })

  it('does nothing at all when disabled', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice, { disabled: true }))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.calls).toEqual([])
  })
})

describe('stuck-mic watchdogs', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  function holdDown(voice: ReturnType<typeof makeVoice>) {
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.calls).toEqual(['start'])
  }

  it('commits on window blur mid-hold', () => {
    const voice = makeVoice()
    holdDown(voice)
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  it('commits when the document is hidden mid-hold', () => {
    const voice = makeVoice()
    holdDown(voice)
    const spy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(voice.calls).toEqual(['start', 'stop'])
    spy.mockRestore()
  })

  it('commits at the hard cap when no keyup ever arrives', () => {
    const voice = makeVoice()
    holdDown(voice)
    act(() => { vi.advanceTimersByTime(MAX_HOLD_MS) })
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  // A later event's modifier flags report live hardware, so any subsequent
  // keystroke can prove a release we never received.
  it('reconciles from a later keystroke that shows the modifier is up', () => {
    const voice = makeVoice()
    holdDown(voice)
    // altKey false on this event => Option is physically up, so the keyup we
    // never saw did happen.
    down('KeyA')
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  it('does NOT treat a still-down modifier as a lost release', () => {
    const voice = makeVoice()
    holdDown(voice)
    // The modifier IS still down, so this is not a missed keyup. It is the chord
    // case, which commits (see the chord suite); what must not happen is the
    // watchdog reading it as a lost release and leaving the machine armed.
    down('KeyA', { altKey: true })
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  it('discards a still-arming capture on blur', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { window.dispatchEvent(new Event('blur')) })
    // Losing focus before the threshold: the press never became a recording, so
    // the session opened on keydown is dropped rather than committed.
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.recording).toBe(false)
  })
})

describe('a startup we do not own', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('aborts a mic-button startup instead of a no-op stop on release', () => {
    // The mic button opened a session and its acquisition is still in flight, so
    // `recording` is false and OUR startPending is false (we did not launch it).
    // The press therefore falls past the second-press guard and calls `start()`
    // again, which the producer's re-entrancy latch swallows — returning nothing,
    // which `launch` reads as a synchronous control and clears startPending for.
    // Releasing must not call `stop()`: capture has not begun, so `stop()` reaches
    // nothing and the original startup would go live after the gesture ended.
    const voice = makeVoice()
    // Swallowed by the producer's re-entrancy latch: pushes nothing forward, sets
    // no state, returns nothing — `recording` stays false throughout.
    voice.start = vi.fn(() => { voice.calls.push('start') })
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(900) })                 // past the threshold
    up('AltRight')

    expect(voice.cancel).toHaveBeenCalled()
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.calls).toEqual(['start', 'cancel'])
  })
})

describe('async start race', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  // start() is async — getUserMedia, and a first-ever permission prompt can take
  // seconds. A hold released inside that window resolves into a live session
  // with nobody holding a key, no keyup coming, and the cap timer already
  // cleared: the microphone stays open until the user notices.
  //
  // The fake is deliberately PESSIMISTIC: it lets startup complete and go live
  // even after `cancel()`, which the real streaming/batch paths do not. That
  // keeps the assertion on the hook's defence-in-depth rather than on the fake's
  // cooperation — release-time `cancel()` is the primary abort, and the settle
  // handler is the backstop for a startup that ignores it.
  //
  // `recording` only becomes true when startup RESOLVES, so a stop() issued at
  // release time is a no-op exactly as it is in useVoiceInput (mediaRef is still
  // null). An assertion that merely counts stop() calls therefore cannot detect
  // the bug — release-time disarm satisfies it either way. Assert the END STATE.
  it('stops a session whose start resolved after the key was already released', async () => {
    let resolveStart: () => void = () => {}
    const calls: string[] = []
    const voice: VoiceControls & { calls: string[] } = {
      calls,
      recording: false,
      start: vi.fn(() => new Promise<void>(r => {
        calls.push('start')
        resolveStart = () => { voice.recording = true; calls.push('capture-live'); r() }
      })),
      stop: vi.fn(() => {
        // Mirrors useVoiceInput.stop: nothing to tear down before capture began.
        calls.push(voice.recording ? 'stop-effective' : 'stop-noop')
        voice.recording = false
      }),
        cancel: vi.fn(() => { calls.push('cancel'); voice.recording = false }),
    }
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    // cancel(), not stop(): mid-startup there is no recorder for stop() to end.
    expect(calls).toEqual(['start', 'cancel'])

    await act(async () => { resolveStart(); await Promise.resolve() })

    // The microphone must not be left open by the late resolution.
    expect(voice.recording).toBe(false)
    expect(calls).toEqual(['start', 'cancel', 'capture-live', 'stop-effective'])
  })

  it('leaves a still-held session running when startup resolves late', async () => {
    let resolveStart: () => void = () => {}
    const voice = makeVoice({
      // `setRecording(true)` lands BEFORE start() resolves in the producer, so a
      // fake that resolves with `recording` still false models a state the
      // producer never occupies — and the release path keys off `recording`.
      start: vi.fn(() => new Promise<void>(r => {
        resolveStart = () => { voice.recording = true; r() }
      })),
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    // Key still down when startup finishes — the session belongs to a live hold.
    await act(async () => { resolveStart(); await Promise.resolve() })
    expect(voice.stop).not.toHaveBeenCalled()
    up('AltRight')
    expect(voice.stop).toHaveBeenCalledTimes(1)
  })

  // The nastier half, and the one a `.then()` handler structurally CANNOT cover:
  // a startup that never settles at all. The streaming path awaits a `ready`
  // frame, so a socket that opens and then goes silent leaves `start()` pending
  // forever — and the hard cap has already been cleared by the release. Cleanup
  // must therefore be synchronous at disarm time, not chained on the promise.
  it('aborts a startup that never settles, without waiting for it', async () => {
    const calls: string[] = []
    const voice: VoiceControls & { calls: string[] } = {
      calls,
      recording: false,
      // Never resolves and never rejects.
      start: vi.fn(() => { calls.push('start'); return new Promise<void>(() => {}) }),
      stop: vi.fn(() => { calls.push('stop') }),
      cancel: vi.fn(() => { calls.push('cancel') }),
    }
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')

    // cancel(), not stop(): stop() is a no-op mid-startup (no recorder, no live
    // socket), while cancel() aborts the startup itself.
    expect(calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()

    // And nothing is left armed that could resurrect it.
    await act(async () => { vi.advanceTimersByTime(MAX_HOLD_MS * 2); await Promise.resolve() })
    expect(calls).toEqual(['start', 'cancel'])
  })

  it('aborts a never-settling startup on blur too, not just on release', () => {
    const voice = makeVoice({ start: vi.fn(() => new Promise<void>(() => {})) })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    act(() => { window.dispatchEvent(new Event('blur')) })
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  // A REJECTED startup can arrive with resources half-acquired: useStreamingStt
  // builds its AudioContext after getUserMedia and the socket handshake, outside
  // any try, and useVoiceInput's streaming branch re-raises. So the mic stream is
  // open with no session to stop — only cancel() tears it down.
  it('cancels when startup rejects while the key is still held', async () => {
    let rejectStart: (e: Error) => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => new Promise<void>((_, rej) => { rejectStart = rej })),
    })
    const { result } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(result.current.holding).toBe(true)

    await act(async () => { rejectStart(new Error('AudioContext failed')); await Promise.resolve() })

    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
    // And the machine is back to idle rather than stuck in a hold with no session.
    expect(result.current.holding).toBe(false)
  })

  it('does not double-cancel when startup rejects after the key was released', async () => {
    let rejectStart: (e: Error) => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => new Promise<void>((_, rej) => { rejectStart = rej })),
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')                                  // release cancels the pending startup
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    await act(async () => { rejectStart(new Error('nope')); await Promise.resolve() })
    expect(voice.cancel).toHaveBeenCalledTimes(1)   // not again
  })

  // The settle handler must stop only the session IT opened. Releasing and then
  // immediately latching inside the getUserMedia window used to have the old
  // hold's resolution kill the brand-new session.
  it('does not stop a session a later tap opened', async () => {
    let resolveFirst: () => void = () => {}
    let call = 0
    const voice = makeVoice({
      start: vi.fn(() => {
        call++
        // Only the FIRST start is slow; useVoiceInput's re-entrancy guard makes
        // the second a no-op in reality, so the first is what goes live.
        if (call === 1) return new Promise<void>(r => { resolveFirst = r })
        return undefined
      }),
    })
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')                                   // hold over, startup still in flight
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(200) })
    up('AltRight')                                   // a tap → latch on (hybrid)
    const cancelsBefore = voice.cancel.mock.calls.length

    await act(async () => { resolveFirst(); await Promise.resolve() })

    // The tap's session survives the old hold's late resolution.
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.cancel.mock.calls.length).toBe(cancelsBefore)
  })
})

// On macOS, Option-chords are how you type ⌥V, ⌥3, ⌥5 and most special
// characters. A bound bare modifier must not turn those into dictation.
describe('modifier chords must not trigger recording', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('a quick Option-chord does not latch recording', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })          // bound modifier down
    act(() => { vi.advanceTimersByTime(120) })
    down('KeyV', { altKey: true })              // typing ⌥V
    act(() => { vi.advanceTimersByTime(80) })
    up('KeyV', { altKey: true })
    up('AltRight')                              // released well under 500ms

    // The release must NOT read as a tap. Capture opened on the keydown, so the
    // invariant is that it is DISCARDED — and on the streaming path `cancel()`
    // drops the local buffer before the server's `ready`, so the keystroke is
    // never transmitted either.
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.recording).toBe(false)
  })

  it('a chord does not latch recording in TOGGLE mode either', () => {
    // Toggle mode used to latch at keydown and leave the phase `idle`, which is
    // the state the chord reconciliation, the keyup handler and the blur guards
    // all bail out of — so ⌥ then E (typing `é`) turned the microphone on with
    // nothing left in the gesture machinery able to turn it off. The press is now
    // armed like any other, so the joining key discards it.
    savePttConfig({ ...MAC_ALT_RIGHT, mode: 'toggle' })
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(60) })
    down('KeyE', { altKey: true })              // typing ⌥e → é
    up('KeyE', { altKey: true })
    up('AltRight')

    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.recording).toBe(false)
    // And the release must not have promoted the discarded press to a latch.
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('a chord held past the threshold does not start a hold', () => {
    const voice = makeVoice()
    const { result } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    down('KeyV', { altKey: true })
    act(() => { vi.advanceTimersByTime(900) })   // well past 500ms
    // The chord discarded the capture and disarmed, so the threshold never
    // arrives to promote it into a hold.
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(result.current.holding).toBe(false)
    up('AltRight')
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('a stray keypress mid-dictation commits rather than discarding', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })   // a real hold is running
    expect(voice.start).toHaveBeenCalledTimes(1)
    down('KeyA', { altKey: true })
    // What was already said survives; discarding would lose the utterance.
    expect(voice.calls).toEqual(['start', 'stop'])
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  it('another modifier joining the press also counts as a chord', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(120) })
    down('ShiftLeft', { altKey: true, shiftKey: true })
    up('AltRight')
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
  })
})

describe('a release during startup commits only what was actually captured', () => {
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  // The discriminator is `recording`, not the transport. useStreamingStt flips
  // recording true at the moment its worklet is wired and PCM is buffering, and
  // only THEN awaits the server's `ready` frame — so `recording` is exactly the
  // "has capture begun" boundary, and the two cases below are opposite sides of
  // it on the SAME transport.

  // Worklet connected, PCM buffering, still waiting on `ready`: real speech is in
  // there. cancel() would throw it away; stop() sends the stop frame (the socket
  // is open) and the upstream force-cleanup keeps the mic ceiling.
  it('commits when the worklet is already buffering', () => {
    const voice = makeVoice({
      // Mirrors useStreamingStt: recording goes true BEFORE the ready await.
      start: vi.fn(() => { voice.recording = true; return new Promise<void>(() => {}) }),
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  // Still inside getUserMedia — a delayed permission grant. No socket exists, so
  // stop() is a NO-OP and the startup would run to completion and go live AFTER
  // the release, transmitting audio for a press the user already finished. Only
  // cancel() aborts it.
  it('aborts when the release beats the permission grant', async () => {
    let resolveStart: () => void = () => {}
    const voice = makeVoice({
      // recording stays false: permission has not been granted yet.
      start: vi.fn(() => new Promise<void>(r => {
        resolveStart = () => { voice.recording = true; r() }
      })),
    })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.cancel).toHaveBeenCalledTimes(1)

    // Even if the startup ignores that abort and goes live, nothing is left
    // holding it — the settle handler is the backstop.
    await act(async () => { resolveStart(); await Promise.resolve() })
    expect(voice.recording).toBe(false)
  })

  it('aborts a batch startup, where no recorder exists yet', () => {
    const voice = makeVoice({ start: vi.fn(() => new Promise<void>(() => {})) })
    renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    up('AltRight')
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })
})

describe('a second press cancels a startup that has not gone live yet', () => {
  // `recording` stays false for the whole getUserMedia + handshake window, so a
  // press arriving there used to fall through the "already capturing" test and
  // open a SECOND start(). useVoiceInput's re-entrancy guard swallows that, so
  // the FIRST startup still went live — the user pressed to switch the mic off
  // and it came on instead, with nothing left holding a key to turn it back off.
  //
  // Assert the END STATE (is the mic live after the startup resolves), not the
  // call count: a swallowed second start() leaves the counts looking plausible.
  function makeSlowVoice(streamEnabled: boolean) {
    let resolveStart: () => void = () => {}
    const calls: string[] = []
    const voice: VoiceControls & { calls: string[]; go: () => void } = {
      calls,
      recording: false,
      streamEnabled,
      // Pessimistic on purpose: startup completes and goes live even after a
      // cancel(), so the assertion rests on the hook rather than the fake.
      start: vi.fn(() => new Promise<void>(r => {
        calls.push('start')
        resolveStart = () => { voice.recording = true; calls.push('capture-live'); r() }
      })),
      stop: vi.fn(() => { calls.push('stop'); voice.recording = false }),
        cancel: vi.fn(() => { calls.push('cancel'); voice.recording = false }),
      go: () => resolveStart(),
    }
    return voice
  }

  it('ends a pending toggle-mode session instead of opening a second one', async () => {
    savePttConfig({ mode: 'toggle', binding: { code: 'AltRight' }, holdMs: 500 })
    // recording stays false until go(): the fake is still inside getUserMedia.
    const voice = makeSlowVoice(true)
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    up('AltRight')
    expect(voice.start).toHaveBeenCalledTimes(1)

    // Second press, still inside the startup window.
    down('AltRight', { altKey: true })
    up('AltRight')
    expect(voice.start).toHaveBeenCalledTimes(1)   // no second session opened
    // Capture had not begun, so the press ABORTS rather than committing silence.
    expect(voice.cancel).toHaveBeenCalled()

    // The original startup now lands. It must not leave the mic live.
    await act(async () => { voice.go() })
    expect(voice.recording).toBe(false)
  })

  it('ends a pending hybrid tap-latch on the next press', async () => {
    savePttConfig(MAC_ALT_RIGHT)
    const voice = makeSlowVoice(false)
    renderHook(() => usePushToTalk(voice))

    // Tap: released before the threshold, so this latches on.
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(100) })
    up('AltRight')
    expect(voice.start).toHaveBeenCalledTimes(1)

    down('AltRight', { altKey: true })
    up('AltRight')
    expect(voice.start).toHaveBeenCalledTimes(1)
    expect(voice.cancel).toHaveBeenCalled()        // batch: nothing captured yet

    await act(async () => { voice.go() })
    expect(voice.recording).toBe(false)
  })

  // The mirror case: a latch is SUPPOSED to outlive the keypress that opened it,
  // so the settle handler must not treat "no key held" as "orphan, stop it".
  it('leaves a latched session running when its startup resolves', async () => {
    savePttConfig(MAC_ALT_RIGHT)
    const voice = makeSlowVoice(false)
    renderHook(() => usePushToTalk(voice))

    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(100) })
    up('AltRight')

    await act(async () => { voice.go() })
    expect(voice.recording).toBe(true)
    expect(voice.stop).not.toHaveBeenCalled()
  })
})

describe('unmount must not orphan the microphone', () => {
  // Everything that would otherwise stop capture is bound to this hook: the
  // key-up listener, blur, visibilitychange, and the hard cap timer. Unmount
  // removes all four at once, so whatever is open at that moment stays open —
  // and a startup still IN FLIGHT is the worst case, because the producer's own
  // unmount cleanup runs before that promise resolves and therefore cannot tear
  // down the stream it goes on to assign.
  beforeEach(() => savePttConfig(MAC_ALT_RIGHT))

  it('commits a live hold', () => {
    const voice = makeVoice()
    const { unmount } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    expect(voice.recording).toBe(true)
    unmount()
    expect(voice.recording).toBe(false)
    expect(voice.stop).toHaveBeenCalled()
  })

  it('releases a mic pre-warmed during the arming window', () => {
    const voice = makeVoice()
    const { unmount } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual(['start'])
    unmount()
    expect(voice.cancel).toHaveBeenCalled()
  })

  it('commits an in-flight startup whose capture had already begun', () => {
    const voice = makeVoice({
      // Mirrors useStreamingStt: recording true once the worklet is buffering,
      // while the `ready` handshake is still outstanding.
      start: vi.fn(() => { voice.recording = true; return new Promise<void>(() => {}) }),
    })
    const { unmount } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    unmount()
    expect(voice.stop).toHaveBeenCalledTimes(1)
  })

  it('aborts an in-flight startup that never reached capture', () => {
    const voice = makeVoice({ start: vi.fn(() => new Promise<void>(() => {})) })
    const { unmount } = renderHook(() => usePushToTalk(voice))
    down('AltRight', { altKey: true })
    act(() => { vi.advanceTimersByTime(500) })
    unmount()
    // No recorder and no socket: only cancel() aborts the acquisition.
    expect(voice.cancel).toHaveBeenCalled()
    expect(voice.stop).not.toHaveBeenCalled()
  })
})

describe('live rebinding', () => {
  it('picks up a new binding written by Settings without a remount', () => {
    savePttConfig(MAC_ALT_RIGHT)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))

    act(() => { savePttConfig({ ...MAC_ALT_RIGHT, binding: { code: 'ShiftRight' } }) })
    down('AltRight', { altKey: true })
    expect(voice.calls).toEqual([])
    down('ShiftRight', { shiftKey: true })
    expect(voice.calls).toEqual(['start'])
  })

  it('reacts to a rebind made in another dashboard window', () => {
    savePttConfig(MAC_ALT_RIGHT)
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    act(() => {
      localStorage.setItem(PTT_STORAGE_KEY, JSON.stringify({ ...MAC_ALT_RIGHT, binding: { code: 'MetaRight' } }))
      window.dispatchEvent(new Event('storage'))
    })
    down('MetaRight', { metaKey: true })
    expect(voice.calls).toEqual(['start'])
  })
})
