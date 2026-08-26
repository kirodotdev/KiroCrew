import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CANCEL_THRESHOLD_PX, TAP_CUE_MS, useTouchPushToTalk } from './useTouchPushToTalk'
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

const HYBRID: PttConfig = { mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }
const ORIGIN_Y = 400

let target: HTMLButtonElement

function pointer(type: string, init: PointerEventInit = {}) {
  act(() => {
    target.dispatchEvent(new PointerEvent(type, {
      pointerId: 1, pointerType: 'touch', isPrimary: true,
      bubbles: true, cancelable: true, clientX: 100, clientY: ORIGIN_Y, ...init,
    }))
  })
}
const down = (init: PointerEventInit = {}) => pointer('pointerdown', init)
const moveTo = (y: number) => pointer('pointermove', { clientY: y })
const up = (init: PointerEventInit = {}) => pointer('pointerup', init)
const pointerCancel = () => pointer('pointercancel')
/** Past the threshold, so the release must discard. */
const upInCancelZone = () => { moveTo(ORIGIN_Y - CANCEL_THRESHOLD_PX); up({ clientY: ORIGIN_Y - CANCEL_THRESHOLD_PX }) }

function mount(voice: VoiceControls, disabled = false) {
  return renderHook(() => useTouchPushToTalk(voice, { target, disabled }))
}

const passThreshold = () => act(() => { vi.advanceTimersByTime(HYBRID.holdMs) })

/**
 * Force `document.visibilityState` to 'hidden'.
 *
 * Defined as an OWN property on the document and deleted again in teardown, so a
 * backgrounding test cannot leak a hidden document into the next one — every
 * later gesture would tear itself down on the first visibilitychange.
 */
function hide() {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'hidden' as DocumentVisibilityState,
  })
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
  delete (document as unknown as Record<string, unknown>).visibilityState
})

describe('useTouchPushToTalk', () => {
  // The anti-clipping invariant, inherited from the keyboard hook: capture opens
  // on the DOWN, before the tap/hold question is settled, so the opening word is
  // in the recording rather than clipped off the front of it.
  it('opens capture on pointerdown and promotes the SAME session at the threshold', () => {
    const voice = makeVoice()
    const { result } = mount(voice)

    down()
    expect(voice.calls).toEqual(['start'])
    expect(result.current.phase).toBe('arming')

    passThreshold()
    expect(voice.start).toHaveBeenCalledTimes(1)
    expect(result.current.holding).toBe(true)

    up()
    expect(voice.calls).toEqual(['start', 'stop'])
    expect(result.current.phase).toBe('idle')
  })

  it('arms cancel once the drag passes the threshold and discards on release', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    passThreshold()

    moveTo(ORIGIN_Y - (CANCEL_THRESHOLD_PX - 1))
    expect(result.current.armedCancel).toBe(false)

    moveTo(ORIGIN_Y - CANCEL_THRESHOLD_PX)
    expect(result.current.armedCancel).toBe(true)

    up({ clientY: ORIGIN_Y - CANCEL_THRESHOLD_PX })
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
  })

  // Reversibility is the whole reason a hold gesture is safe without an Esc key.
  it('disarms cancel when the finger comes back down, and then commits', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    passThreshold()
    moveTo(ORIGIN_Y - CANCEL_THRESHOLD_PX)
    expect(result.current.armedCancel).toBe(true)

    moveTo(ORIGIN_Y - 4)
    expect(result.current.armedCancel).toBe(false)

    up({ clientY: ORIGIN_Y - 4 })
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  /*
   * A press that never became a hold is not a recording the user asked for, and
   * `PttConfig.mode` no longer changes that. Capture opens on the pointerdown so
   * the opening word is never clipped, so a tap does leave a fragment — and it
   * goes unsent.
   *
   * The three modes are enumerated to pin exactly that IRRELEVANCE: this hook
   * reads only `holdMs` from the config now, so a future `mode` branch reaching
   * back into the touch gesture reddens this. `hybrid` and `toggle` used to LATCH
   * here — a session that stayed live with no finger on the target. That is gone,
   * because a latch outlives its own gesture, so the hook would have to hold an
   * invariant over a session whose end it does not control, and `VoiceControls`
   * reports neither a refused start nor a session end. Four separate defects came
   * out of inferring those, every one a bar claiming a live mic that was not open.
   */
  it.each(['ptt', 'hybrid', 'toggle'] as const)('discards a sub-threshold tap regardless of mode (%s)', mode => {
    savePttConfig({ ...HYBRID, mode })
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    up()
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
    expect(result.current.phase).toBe('idle')
  })

  // pointercancel is touch's version of a keyup that never arrives: the system
  // took the gesture, so the user never chose to finish.
  it('discards on pointercancel rather than committing', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    passThreshold()
    pointerCancel()
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('commits at the hard cap when no release ever arrives', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    passThreshold()
    act(() => { vi.advanceTimersByTime(MAX_HOLD_MS) })
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  // `recording` — not the gesture's intent — is what decides whether a teardown
  // may commit. A release that beats the permission grant has nothing to stop,
  // and stop() would leave the startup to go live with nobody watching it.
  it('cancels rather than stops when the release beats capture', () => {
    const voice = makeVoice({ start: vi.fn(() => new Promise<void>(() => {})) })
    mount(voice)
    down()
    passThreshold()
    up()
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    expect(voice.stop).not.toHaveBeenCalled()
  })

  it('does nothing at all while disabled', () => {
    const voice = makeVoice()
    const { result } = mount(voice, true)
    down()
    passThreshold()
    up()
    expect(voice.calls).toEqual([])
    expect(result.current.phase).toBe('idle')
  })

  it('ignores a second finger instead of letting it steer the gesture', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    passThreshold()
    // A non-primary pointer must not re-origin the drag or arm the cancel zone.
    pointer('pointerdown', { pointerId: 2, isPrimary: false })
    pointer('pointermove', { pointerId: 2, isPrimary: false, clientY: ORIGIN_Y - 200 })
    up()
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  // Unmount is the third way a target can go away, and it ends the same as the
  // other two: the user never released, so there is nothing to honour. This
  // asserted a COMMIT before `abandon()` unified the teardown paths — committing
  // there would transcribe unreleased audio into a composer that no longer
  // exists, which is the same defect as the detach case.
  it('discards a press in flight when the composer unmounts', () => {
    const voice = makeVoice()
    const { unmount } = mount(voice)
    down()
    passThreshold()
    unmount()
    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.recording).toBe(false)
  })

  it('commits a hold that gets backgrounded', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    passThreshold()
    hide()
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  it('discards a press that gets backgrounded before it became a hold', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    hide()
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(voice.calls).toEqual(['start', 'cancel'])
  })

  it('leaves an idle hook alone when the page is backgrounded', () => {
    const voice = makeVoice()
    mount(voice)
    hide()
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(voice.calls).toEqual([])
  })

  // Regression: the hold target mounts LATER than the hook — it appears only once
  // the user switches into hold mode. An effect keyed on a ref object runs once
  // against null and never rebinds, so the listeners never reach the button and
  // the whole gesture is silently dead. Only a real-layout capture caught this,
  // because a test that hands over a target up front cannot express it.
  it('binds to a target that mounts after the hook', () => {
    const voice = makeVoice()
    const { result, rerender } = renderHook(
      ({ el }: { el: HTMLElement | null }) => useTouchPushToTalk(voice, { target: el }),
      { initialProps: { el: null as HTMLElement | null } },
    )

    down()
    expect(voice.calls).toEqual([])

    rerender({ el: target })
    down()
    passThreshold()
    expect(result.current.holding).toBe(true)
    up()
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  // A gesture may not outlive its target. Removing the listeners without tearing
  // the gesture down left the press in flight with no release ever coming, and
  // MAX_HOLD_MS later the hard cap COMMITTED it — transcribing audio the user
  // never released on, with the mic open until then.
  it('discards a press in flight when the target is detached, and the cap cannot fire after', () => {
    const voice = makeVoice()
    const { rerender } = renderHook(
      ({ el }: { el: HTMLElement | null }) => useTouchPushToTalk(voice, { target: el }),
      { initialProps: { el: target as HTMLElement | null } },
    )
    down()
    passThreshold()
    expect(voice.recording).toBe(true)

    rerender({ el: null })
    expect(voice.calls).toEqual(['start', 'cancel'])

    act(() => { vi.advanceTimersByTime(MAX_HOLD_MS * 2) })
    expect(voice.stop).not.toHaveBeenCalled()
  })

  // Every exit path must clear ALL per-gesture state, `pointerIdRef` included.
  // `failStart` cleared the phase but not that ref, and `onPointerDown` refuses a
  // press while it is set — so one rejected streaming start() killed the button
  // outright until the composer remounted. The suite previously exercised only a
  // synchronous start() and one that never resolves, never a rejection.
  it('stays usable after a rejected start()', async () => {
    let reject: (e: Error) => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => new Promise<void>((_, rej) => { reject = rej })),
    })
    mount(voice)

    down()
    passThreshold()
    await act(async () => { reject(new Error('handshake failed')); await Promise.resolve() })
    expect(voice.cancel).toHaveBeenCalledTimes(1)
    up()

    // A rejection must not deaden the target: the next press has to open a session.
    voice.start = vi.fn(() => { voice.calls.push('start'); voice.recording = true })
    down()
    passThreshold()
    up()
    expect(voice.calls.filter(c => c === 'start').length).toBeGreaterThanOrEqual(1)
    expect(voice.stop).toHaveBeenCalledTimes(1)
  })

  // The pre-ready streaming window: `useVoiceInput` assigns session ownership only
  // after the handshake, but capture is already buffering PCM. Answering "did
  // capture begin?" with the ownership-gated flag made a release inside that
  // window discard what the user had just said. ChatInput now passes the ungated
  // `voiceCaptureActive` for exactly this question — the hook must commit whenever
  // the controls report recording, with no second opinion of its own.
  it('commits whenever the controls report recording, however that was derived', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    passThreshold()
    expect(voice.recording).toBe(true)
    up()
    expect(voice.calls).toEqual(['start', 'stop'])
  })

  // A consumer cannot infer `settling`: the gesture is already idle while capture
  // is still draining, so the hook is the only thing that knows, and it has to say.
  it('names every visible bar state itself, so no consumer has to reassemble one', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    expect(result.current.bar).toBe('idle')

    down()
    // An arming press has not promised anything yet, and lasts under holdMs.
    expect(result.current.bar).toBe('idle')

    passThreshold()
    expect(result.current.bar).toBe('holding')

    moveTo(ORIGIN_Y - CANCEL_THRESHOLD_PX)
    // Both armed and holding are true here; the discard is the one to announce.
    expect(result.current.armedCancel).toBe(true)
    expect(result.current.bar).toBe('armed-cancel')

    moveTo(ORIGIN_Y)
    expect(result.current.bar).toBe('holding')

    up()
    expect(result.current.phase).toBe('idle')
    expect(result.current.bar).toBe('idle')
  })

  it('reports settling while capture is still draining after a release', () => {
    const voice = makeVoice()
    // A stop that DRAINS: the real `useStreamingStt.stop()` keeps `recording`
    // true until socket cleanup, and that window is what the bar used to
    // mislabel as its resting state over a microphone still holding audio.
    voice.stop = vi.fn(() => { voice.calls.push('stop') })
    const { result } = mount(voice)

    down()
    passThreshold()
    up()

    expect(result.current.phase).toBe('idle')
    expect(result.current.bar).toBe('settling')
  })

  it('does not re-light a finished drain when a later foreign recording starts', () => {
    const voice = makeVoice()
    voice.stop = vi.fn(() => { voice.calls.push('stop') })   // a stop that drains
    const { result, rerender } = mount(voice)

    // This gesture commits and its capture drains.
    down()
    passThreshold()
    up()
    expect(result.current.bar).toBe('settling')

    // The drain finishes.
    voice.recording = false
    rerender()
    expect(result.current.bar).toBe('idle')

    // Later, the keyboard binding starts a recording this hook never saw. Masking
    // the flag instead of clearing it let this re-light the drain and put
    // "Finishing…" over live speech.
    voice.recording = true
    rerender()
    expect(result.current.bar).toBe('idle')
  })

  it('does not call a foreign recording a drain, on a device that has both inputs', () => {
    const voice = makeVoice()
    const { result, rerender } = mount(voice)
    // A coarse-pointer device with a hardware keyboard: the keyboard binding
    // starts a recording this hook never sees. `idle && recording` would read
    // that as this gesture's own drain and announce "Finishing…" over live speech.
    voice.recording = true
    rerender()                              // the flag is only read at render time

    expect(result.current.phase).toBe('idle')
    expect(result.current.bar).toBe('idle')

    // Counter-check: THIS gesture's own drain must still be named, or the assertion
    // would pass simply by never reaching the settling branch at all. The foreign
    // capture has to end first — the pointerdown guard deliberately refuses to
    // start a gesture on top of a live one.
    voice.recording = false
    voice.stop = vi.fn(() => { voice.calls.push('stop') })   // a stop that drains
    down()
    passThreshold()
    up()
    expect(result.current.bar).toBe('settling')
  })

  it('acknowledges a discarded tap, then clears the cue on its own', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    up()
    // Capture DID open on the press, so a fragment was really dropped. Saying
    // nothing on the most likely first gesture teaches the user nothing.
    expect(result.current.bar).toBe('tap-too-short')

    act(() => { vi.advanceTimersByTime(TAP_CUE_MS + 50) })
    expect(result.current.bar).toBe('idle')
  })

  it('drops the cue the moment a real press starts, so it cannot sit over a gesture', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    up()
    expect(result.current.bar).toBe('tap-too-short')

    down()
    passThreshold()
    expect(result.current.bar).toBe('holding')
  })

  it('discards a flick-up release even when the final move was coalesced away', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    passThreshold()

    // No pointermove at the cancel distance at all — the browser folded it into the
    // pointerup. The old code read `armedRef`, which no move had ever set, and
    // transcribed audio the user had just thrown away.
    up({ clientY: ORIGIN_Y - CANCEL_THRESHOLD_PX })

    expect(voice.calls).toEqual(['start', 'cancel'])
    expect(voice.stop).not.toHaveBeenCalled()
    expect(result.current.bar).toBe('idle')
  })

  it('commits when the finger came back down, even if the returning move was coalesced', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    passThreshold()
    moveTo(ORIGIN_Y - CANCEL_THRESHOLD_PX)   // armed, and the bar shows it
    expect(result.current.armedCancel).toBe(true)

    // Released back at the origin: the release position is what the user chose.
    up({ clientY: ORIGIN_Y })

    expect(voice.calls).toEqual(['start', 'stop'])
    expect(voice.cancel).not.toHaveBeenCalled()
  })

  // A teardown relinquishes ownership, and that has to disown the startup still in
  // flight too. It did not: the abandoned startup's `settleStart` found its sequence
  // still valid and ownership cleared, and stopped whatever was live — which by then
  // was a REPLACEMENT session the user had started from the mic button, so their new
  // speech was truncated by a resolution belonging to a gesture they had abandoned.
  it('does not let an abandoned startup stop a replacement session', async () => {
    let settle: () => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => {
        voice.calls.push('start')
        return new Promise<void>(res => { settle = res })
      }),
    })
    const { rerender } = renderHook(
      ({ el }: { el: HTMLElement | null }) => useTouchPushToTalk(voice, { target: el }),
      { initialProps: { el: target as HTMLElement | null } },
    )

    down()
    up()                                   // sub-threshold tap, still acquiring the mic
    rerender({ el: null })                 // switched to the keyboard: target detached
    expect(voice.cancel).toHaveBeenCalledTimes(1)

    // A replacement session, opened by something this hook does not drive.
    voice.recording = true
    await act(async () => { settle(); await Promise.resolve() })

    expect(voice.stop).not.toHaveBeenCalled()
    expect(voice.recording).toBe(true)
  })

  it('treats a cancel-zone release as a discard even for a sub-threshold tap', () => {
    const voice = makeVoice()
    mount(voice)
    down()
    upInCancelZone()
    expect(voice.calls).toEqual(['start', 'cancel'])
  })

  // Ownership is EXPORTED (#5753): the consumer's hold-mode predicate asks "does
  // the capture in flight belong to this gesture?", and `owns` is the hook's own
  // answer, from `ownerRef` — never inferred out there from a mounted DOM node or
  // a recording flag, both of which also match captures opened elsewhere.
  it('exports ownership for exactly the pointerdown → resolution window', () => {
    const voice = makeVoice()
    // A stop that DRAINS (like the real streaming stop), so the post-release
    // window is observable: ownership must already be gone while `settling`
    // still names the leftover drain.
    voice.stop = vi.fn(() => { voice.calls.push('stop') })
    const { result } = mount(voice)
    expect(result.current.owns).toBe(false)

    down()
    expect(result.current.owns).toBe(true)

    passThreshold()
    expect(result.current.owns).toBe(true)

    // Commit relinquishes AT the release: the drain that remains is `settling`,
    // not ownership — a consumer gating on `owns` hands the surface back here.
    up()
    expect(result.current.owns).toBe(false)
    expect(result.current.bar).toBe('settling')
  })

  it('relinquishes exported ownership on a cancel-zone discard', () => {
    const voice = makeVoice()
    const { result } = mount(voice)
    down()
    passThreshold()
    expect(result.current.owns).toBe(true)

    upInCancelZone()
    expect(result.current.owns).toBe(false)
  })

  // A rejected start() must disown too: ownership without a session would hold
  // the consumer's hold-mode gate open over a draft with no capture behind it.
  it('relinquishes exported ownership when the start() it launched rejects', async () => {
    let reject: (e: Error) => void = () => {}
    const voice = makeVoice({
      start: vi.fn(() => new Promise<void>((_, rej) => { reject = rej })),
    })
    const { result } = mount(voice)
    down()
    expect(result.current.owns).toBe(true)

    await act(async () => { reject(new Error('handshake failed')); await Promise.resolve() })
    expect(result.current.owns).toBe(false)
  })
})
