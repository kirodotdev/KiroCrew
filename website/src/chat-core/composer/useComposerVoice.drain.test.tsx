import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useLayoutEffect, useRef } from 'react'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* The released-dictation drain, from the engine up to the props ChatInput reads.
 *
 * Two separate claims live here. One: the drain reaches the composer under its
 * own name, so a surface can offer a back-out for exactly the window where the
 * utterance is queued behind the recogniser and nothing else is. Two: a discard
 * taken in that window is final — the engine's own result arrives late by
 * definition (that is what the wait IS), and it must not land in a composer the
 * user has already backed out of.
 *
 * The engine is faked, and the fake CAPTURES the callbacks the hook hands it, so
 * a late transcript can be delivered after the cancel exactly as the socket would
 * deliver one. */

type Engine = {
  recording: boolean; transcribing: boolean; draining: boolean; sessionOwner: string | null; streamEnabled: boolean
  toggle: () => void; start: () => Promise<void>; stop: () => void; cancel: () => void; prewarm: () => void
  error: string | null; level: number; deviceLabel: string; deviceId: string; clearError: () => void; partial: string
  download: null; sampleRef: { current: object }; switchDevice: () => void; deviceSwitchIsLive: boolean
}

type Captured = {
  onText?: (text: string, sessionId: string | null, origin: string) => void
  onPartial?: (text: string, sessionId?: string | null) => void
  onCaptureStop?: () => void
}

const fx = vi.hoisted(() => {
  const engine: Engine = {
    recording: false, transcribing: false, draining: false, sessionOwner: null, streamEnabled: true,
    toggle: vi.fn(), start: vi.fn(async () => {}), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
  }
  const captured: Captured = {}
  return { engine, captured }
})

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: Captured['onText'], opts: {
    onPartial?: Captured['onPartial']; onCaptureStop?: Captured['onCaptureStop']
  }) => {
    fx.captured.onText = onText
    fx.captured.onPartial = opts?.onPartial
    fx.captured.onCaptureStop = opts?.onCaptureStop
    return fx.engine
  },
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../api/client', () => ({
  api: { sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: true, dictation_panel: true, provider: 'local' }) },
}))

import { useComposerVoice, composerVoiceInputProps, _resetMicOwner } from './useComposerVoice'

const STT_STREAMING = { enabled: true, available: true, streaming: true, dictation_panel: true, provider: 'local' }
/** Batch: one blob, one final, routed to the slot that dictated it. */
const STT_BATCH = { ...STT_STREAMING, streaming: false }

function makeWrapper(cfg: typeof STT_STREAMING = STT_STREAMING) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sttConfig'], cfg)
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

const SESSION = 'slot-a'

/** A host that parks the outgoing slot's draft in a LAYOUT effect, as ChatPane
 *  does. The wrapper is the hook's PARENT, so its layout effect runs after the
 *  hook's own -- which is the whole point: a passive discard would run later
 *  still, and park the abandoned dictation. */
function makeParkingWrapper(
  cfg: typeof STT_STREAMING,
  slot: { current: string },
  inputRef: { current: string },
  parked: Record<string, string>,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sttConfig'], cfg)
  return function Wrapper({ children }: { children: ReactNode }) {
    const seen = useRef(slot.current)
    useLayoutEffect(() => {
      if (seen.current !== slot.current) { parked[seen.current] = inputRef.current; seen.current = slot.current }
    })
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

function mountUnderParkingHost(cfg: typeof STT_STREAMING, caretRef?: { current: { start: number; end: number } | null }) {
  const inputRef = { current: '' }
  const slot = { current: SESSION }
  const parked: Record<string, string> = {}
  fx.engine.streamEnabled = cfg.streaming
  const hook = renderHook(
    () => useComposerVoice({
      sessionId: slot.current, inputRef, setInput: (v: string) => { inputRef.current = v }, caretRef,
    }),
    { wrapper: makeParkingWrapper(cfg, slot, inputRef, parked) },
  )
  const switchTo = (sessionId: string) => { slot.current = sessionId; act(() => { hook.rerender() }) }
  return { hook, inputRef, switchTo, parked }
}

function mount(cfg: typeof STT_STREAMING = STT_STREAMING, caretRef?: { current: { start: number; end: number } | null }) {
  const inputRef = { current: '' }
  // The slot on screen, readable by the hook and movable by the test: a switch is
  // this composer re-rendering with another session, not a second composer.
  const slot = { current: SESSION }
  fx.engine.streamEnabled = cfg.streaming
  const hook = renderHook(
    () => useComposerVoice({
      sessionId: slot.current, inputRef, setInput: (v: string) => { inputRef.current = v }, caretRef,
    }),
    { wrapper: makeWrapper(cfg) },
  )
  const switchTo = (sessionId: string) => { slot.current = sessionId; act(() => { hook.rerender() }) }
  return { hook, inputRef, switchTo }
}

beforeEach(() => {
  _resetMicOwner()
  const e = fx.engine
  e.recording = false; e.transcribing = false; e.draining = false; e.sessionOwner = null; e.partial = ''
  e.streamEnabled = true
  e.cancel = vi.fn()
  e.toggle = vi.fn()
  // Reset too: one case replaces it with a start that parks, and a parked promise
  // left in place would hand the next case a microphone that never answers.
  e.start = vi.fn(async () => {})
  fx.captured.onText = undefined
  fx.captured.onPartial = undefined
})

/** The released utterance is queued behind the recogniser: capture is over, the
 *  transport is not, and the owning composer is this one. */
function enterDrain(hook: ReturnType<typeof mount>['hook']) {
  fx.engine.recording = false
  fx.engine.transcribing = true
  fx.engine.draining = true
  fx.engine.sessionOwner = SESSION
  // The engine reports the capture ending, which is what freezes the release
  // caret and arms the post-release write path. A drain reached without it is
  // not the state the user is in.
  act(() => { fx.captured.onCaptureStop?.() })
  act(() => { hook.rerender() })
}

describe('useComposerVoice — the drain reaches ChatInput under its own name', () => {
  it('reports the drain apart from the transcription it is folded into', () => {
    const { hook } = mount()
    enterDrain(hook)
    const props = composerVoiceInputProps(hook.result.current)
    expect(props.voiceDraining).toBe(true)
    // Capture really has ended, which is why the recording-keyed way out is shut.
    expect(props.voiceRecording).toBe(false)
  })

  it('reports no drain for a batch transcription, whose audio cannot be recalled', () => {
    const { hook } = mount()
    fx.engine.transcribing = true
    fx.engine.draining = false
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    const props = composerVoiceInputProps(hook.result.current)
    expect(props.voiceDraining).toBe(false)
    expect(props.voiceTranscribing).toBe(true)
  })

  it('does not report another composer\'s drain, since only the owner may discard', () => {
    const { hook } = mount()
    fx.engine.draining = true
    fx.engine.transcribing = true
    fx.engine.sessionOwner = 'slot-b'
    act(() => { hook.rerender() })
    expect(composerVoiceInputProps(hook.result.current).voiceDraining).toBe(false)
  })

  it('offers a discard handler for the drain to route to', () => {
    const { hook } = mount()
    enterDrain(hook)
    expect(typeof composerVoiceInputProps(hook.result.current).onVoiceCancel).toBe('function')
  })
})

describe('useComposerVoice — a drain discard is final', () => {
  it('releases the engine so the microphone is free for another chat', () => {
    const { hook } = mount()
    enterDrain(hook)
    act(() => { hook.result.current.cancelVoice() })
    expect(fx.engine.cancel).toHaveBeenCalledTimes(1)
  })

  it('keeps a late transcript out of the composer', () => {
    // The cold-model case: the wait produced no partial, so the composer holds
    // only what the user typed. A transcript that lands after the discard is the
    // abandoned utterance and must not be appended to it.
    const { inputRef, hook } = mount()
    inputRef.current = 'a draft the user typed'
    enterDrain(hook)
    act(() => { hook.result.current.cancelVoice() })
    act(() => { fx.captured.onText?.('the abandoned utterance', SESSION, 'stream') })
    expect(inputRef.current).toBe('a draft the user typed')
  })

  it('keeps a late partial out of the composer', () => {
    const { inputRef, hook } = mount()
    inputRef.current = 'a draft the user typed'
    enterDrain(hook)
    act(() => { hook.result.current.cancelVoice() })
    act(() => { fx.captured.onPartial?.('the abandoned hyp', SESSION) })
    expect(inputRef.current).toBe('a draft the user typed')
  })

  it('accepts a transcript again on the next dictation', () => {
    // The discard must disarm THIS utterance, not the feature: a session that
    // starts after it delivers normally.
    const { inputRef, hook } = mount()
    enterDrain(hook)
    act(() => { hook.result.current.cancelVoice() })
    fx.engine.recording = true
    fx.engine.draining = false
    fx.engine.transcribing = false
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    act(() => { void hook.result.current.startVoice() })
    act(() => { fx.captured.onText?.('the next utterance', SESSION, 'stream') })
    expect(inputRef.current).toContain('the next utterance')
  })
})

describe('useComposerVoice — a drain discard takes the speech and leaves the typing', () => {
  /** Dictate into an existing draft, then release: the composer holds the user's
   *  own text plus the streamed run, and the wait begins. A `caret` with a
   *  non-empty range makes the write replace those words, as the real splice does.
   *  It is seeded AFTER mount, because arriving in a slot drops its caret. */
  function dictateThenRelease(draft: string, spoken: string, caret?: { start: number; end: number }) {
    const caretRef: { current: { start: number; end: number } | null } = { current: null }
    const { hook, inputRef, switchTo } = mount(STT_STREAMING, caretRef)
    inputRef.current = draft
    caretRef.current = caret ?? null
    fx.engine.recording = true
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    act(() => { void hook.result.current.startVoice() })
    fx.engine.partial = spoken
    act(() => { fx.captured.onPartial?.(spoken, SESSION) })
    enterDrain(hook)
    fx.engine.partial = spoken
    return { hook, inputRef, switchTo }
  }

  it('removes the dictated run when the user has typed ahead of it, not only after it', () => {
    // Typing anywhere but at the end breaks the prefix the rollback used to
    // require, and the wait is long enough that editing one's own draft is
    // ordinary. The run still goes; the edit stays.
    const { hook, inputRef } = dictateThenRelease('ask about the rollout', 'and the dates')
    expect(inputRef.current).toBe('ask about the rollout and the dates')
    inputRef.current = 'PLEASE ask about the rollout and the dates'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('PLEASE ask about the rollout')
  })

  it('takes its own run and not the user\'s identical phrase elsewhere in the draft', () => {
    // The draft already says what the dictation said. Only ONE of the two is the
    // machine's, and which one is a question about position, not about text.
    const { hook, inputRef } = dictateThenRelease('and the dates matter', 'and the dates')
    expect(inputRef.current).toBe('and the dates matter and the dates')
    inputRef.current = 'X and the dates matter and the dates'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('X and the dates matter')
  })

  it('leaves the user\'s own copy alone once they have edited the dictated run away', () => {
    // The user rewrote the run themselves and their draft happens to hold the
    // same words somewhere else. A text match would delete those; the span is
    // gone, so nothing is.
    const { hook, inputRef } = dictateThenRelease('ask about the rollout', 'and the dates')
    inputRef.current = 'ask and the dates about the rollout urgently'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('ask and the dates about the rollout urgently')
  })

  it('leaves the composer alone when the user already removed the run themselves', () => {
    const { hook, inputRef } = dictateThenRelease('ask about the rollout', 'and the dates')
    inputRef.current = 'ask about the rollout instead'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('ask about the rollout instead')
  })

  it('gives back the words the dictation spoke over, not just the ones it added', () => {
    // Dictating with a selection DELETES those characters, so a discard that only
    // drops what was added hands back a draft the user never wrote.
    const { hook, inputRef } = dictateThenRelease(
      'Please review the rollout plan', 'release', { start: 7, end: 13 },
    )
    expect(inputRef.current).toBe('Please release the rollout plan')
    inputRef.current = 'URGENT: Please release the rollout plan'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('URGENT: Please review the rollout plan')
  })

  it('still gives the spoken-over words back after a stabilised partial lands in the drain', () => {
    // The insertion point is deliberately rebased once the release has happened,
    // so it no longer describes the selection the write consumed. The discard
    // reads what the write recorded instead of asking the caret again.
    const { hook, inputRef } = dictateThenRelease(
      'Please review the rollout plan', 'release', { start: 7, end: 13 },
    )
    inputRef.current = 'Please release the rollout plan today'
    fx.engine.partial = 'release'
    act(() => { fx.captured.onPartial?.('release', SESSION) })
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('Please review the rollout plan today')
  })

  it('leaves both copies when the user types the same word the dictation wrote', () => {
    // Typing `ship ` in front of the run and typing ` ship` after it produce the
    // same draft, so which copy is the machine's is not decidable. Keeping a
    // visible residue is the safe answer; deleting the typed word is not.
    const { hook, inputRef } = dictateThenRelease('please review', 'ship', { start: 7, end: 13 })
    expect(inputRef.current).toBe('please ship')
    inputRef.current = 'please ship ship'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('please ship ship')
  })

  it('leaves the draft alone when the dictation repeated a word already beside it', () => {
    // Dictating `review` right after the user's own `review`. If they delete the
    // dictated copy themselves, their word occupies the run's offsets and reads
    // as the run; removing it would take the word they kept.
    const { hook, inputRef } = dictateThenRelease('please review the plan', 'review', { start: 6, end: 6 })
    expect(inputRef.current).toBe('please review review the plan')
    inputRef.current = 'please review the plan'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('please review the plan')
  })

  it('takes nothing back from the draft that follows a send', () => {
    // A send during the drain ends the life of the run those offsets describe: the
    // composer is cleared and whatever the user types next is their own. The engine
    // still reports the drain, so liveness alone would not stop the cancel.
    const { hook, inputRef } = dictateThenRelease('please ', 'ship it')
    expect(inputRef.current).toBe('please ship it')
    act(() => { hook.result.current.disarmForSend() })
    // The host clears the composer on send; the user then types their next message,
    // which happens to repeat the words they just sent. The locator PLACES the run in
    // this one -- the typing sits away from it, so nothing about the offsets is in
    // doubt -- which is what makes the cleared record the only thing standing between
    // the cancel and their new draft.
    inputRef.current = 'ok please ship it'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('ok please ship it')
  })

  it('drops the drain\'s own final after a send, not just after a stop', () => {
    // The main send refuses while an utterance is in flight, but a follow-up chip
    // does not, so a send during the DRAIN is reachable. Disarming only while
    // capture is live left the stream armed here, and the clears had already taken
    // the snapshot value the late-write guard reads -- so the recogniser's final
    // was spliced into a composer that had already sent, at the offset the release
    // froze.
    const { hook, inputRef } = dictateThenRelease('ask about the rollout ', 'and the dates')
    expect(inputRef.current).toBe('ask about the rollout and the dates')
    act(() => { hook.result.current.disarmForSend() })
    // The host clears the composer on send and the user starts their next message.
    inputRef.current = 'next question'
    act(() => { fx.captured.onPartial?.('and the dates', SESSION) })
    expect(inputRef.current).toBe('next question')
  })

  it('keeps the drain\'s own final when the send left the composer alone', () => {
    // The twin of the case above, and why that disarm has to be conditional. An
    // option answer carries its own text: the draft is untouched afterwards, and so
    // is the endpoint. This is the COLD shape -- the wait produced no partial, so
    // the close-time final is the only copy of the utterance -- which is exactly
    // what an unconditional disarm on that send threw away.
    const { inputRef, hook } = mount()
    inputRef.current = 'a draft the user typed'
    enterDrain(hook)
    act(() => { fx.captured.onText?.('the utterance nobody abandoned', SESSION, 'stream') })
    expect(inputRef.current).toBe('a draft the user typed the utterance nobody abandoned')
  })

  it('hands a held batch transcript over after the layout phase, not inside it', () => {
    // The rollback has to run in the layout phase to beat the host's own rebind;
    // this delivery must NOT. Handed over before that rebind, a transcript held for
    // the slot being switched TO is claimed while the host still believes it shows
    // the slot being left, so the speech is written into the OUTGOING slot's draft
    // and is missing from the one that dictated it. The inbox's subscribe path
    // defers its own hand-over for the same reason, in the same words.
    //
    // Structural, because the behavioural discriminator needs a held result staged
    // in the inbox's module state across a switch, and the ordering it protects is
    // between two effects of one commit -- which RTL flushes apart. Deliberately
    // brittle: if the call is reshaped, UPDATE the substring, never delete it.
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(resolve(here, './useComposerVoice.ts'), 'utf8')
    const layout = src.indexOf('useLayoutEffect(() => {')
    expect(layout, 'the session-switch layout effect is gone').toBeGreaterThan(-1)
    const deferred = src.indexOf('queueMicrotask(() => {', layout)
    const call = src.indexOf('redeliverPending()', layout)
    expect(call, 'the held-transcript hand-over is gone').toBeGreaterThan(-1)
    expect(deferred, 'the hand-over must be deferred out of the layout phase').toBeGreaterThan(-1)
    expect(deferred, 'the deferral must wrap the hand-over, not follow it').toBeLessThan(call)
  })

  it('lets go of the microphone claim when a start is still parked on the dialog', async () => {
    // A press that is waiting on the permission dialog has taken this hook's
    // re-entrancy latch and the microphone claim, and only its own settle releases
    // them -- which the dialog can postpone indefinitely. Leaving for another chat
    // used to keep both, so every later press, in this composer and in any other,
    // waited on a session nobody was in any more. Neither `recording` nor
    // `draining` is set yet at that point, so the discard below cannot cover it.
    let answer: (() => void) | null = null
    fx.engine.start = vi.fn(() => new Promise<void>(res => { answer = () => res() }))
    const { hook, switchTo } = mount(STT_STREAMING)
    act(() => { void hook.result.current.startVoice() })
    expect(fx.engine.start).toHaveBeenCalledTimes(1)
    switchTo('slot-b')
    // The press in the slot the user is now in must reach the engine.
    act(() => { void hook.result.current.startVoice() })
    expect(fx.engine.start).toHaveBeenCalledTimes(2)
    // The abandoned start answering later must not disturb the live one.
    await act(async () => { answer?.(); await Promise.resolve() })
    expect(fx.engine.start).toHaveBeenCalledTimes(2)
  })

  it('takes nothing back when no dictation is in flight', () => {
    // The write record outlives the utterance that made it: once partials own the
    // region the close-time final is suppressed before the clear. A cancel can
    // then arrive with nothing running -- a push-to-talk whose start was refused
    // releases into `cancel()` -- and by then those characters are the user's
    // accepted transcript.
    const { hook, inputRef } = dictateThenRelease('please ', 'ship it')
    expect(inputRef.current).toBe('please ship it')
    // The dictation ends: the engine reports neither capture nor drain.
    fx.engine.recording = false
    fx.engine.transcribing = false
    fx.engine.draining = false
    act(() => { hook.rerender() })
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('please ship it')
  })

  it('takes the separator the LAST rebuild added, not the first write\'s answer', () => {
    // Two partials while recording, each rebuilt from the snapshot. The first is
    // a script that needs no separator before the draft's Latin tail; the
    // correction is Latin and does need one, so the space in the composer is the
    // correction's. Carrying the first write's empty answer would strand it.
    const caretRef: { current: { start: number; end: number } | null } = { current: null }
    const { hook, inputRef } = mount(STT_STREAMING, caretRef)
    inputRef.current = 'world'
    caretRef.current = { start: 0, end: 0 }
    fx.engine.recording = true
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    act(() => { void hook.result.current.startVoice() })
    fx.engine.partial = '\u4f60\u597d'
    act(() => { fx.captured.onPartial?.('\u4f60\u597d', SESSION) })
    expect(inputRef.current).toBe('\u4f60\u597dworld')
    fx.engine.partial = 'hello'
    act(() => { fx.captured.onPartial?.('hello', SESSION) })
    expect(inputRef.current).toBe('hello world')
    enterDrain(hook)
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('world')
  })

  it('leaves the draft alone when the write put its own separator between the copies', () => {
    // Dictating at the very start of a draft that already opens with those words:
    // the write's separator lands BEHIND the run, so the copy sits one character
    // further along than the span's own end. The user removes the duplicate
    // during the wait; Escape must not take the copy they kept.
    const { hook, inputRef } = dictateThenRelease('the plan is ready', 'the plan', { start: 0, end: 0 })
    expect(inputRef.current).toBe('the plan the plan is ready')
    inputRef.current = 'the plan is ready'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('the plan is ready')
  })

  it('takes the separator it added on the far side, restoring the draft as it was', () => {
    // A mid-draft dictation separates itself from the text that follows. That
    // space is the write's, so the discard owes it back too.
    const { hook, inputRef } = dictateThenRelease('askthe rollout', 'about', { start: 3, end: 3 })
    expect(inputRef.current).toBe('ask about the rollout')
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('askthe rollout')
  })

  it('leaves that separator alone once the user has closed the gap themselves', () => {
    // They deleted the space. Removing one anyway would eat the first character
    // of their own text.
    const { hook, inputRef } = dictateThenRelease('askthe rollout', 'about', { start: 3, end: 3 })
    inputRef.current = 'ask aboutthe rollout'
    act(() => { hook.result.current.cancelVoice() })
    expect(inputRef.current).toBe('askthe rollout')
  })
})
describe('useComposerVoice — leaving the slot mid-drain does not strand the engine', () => {
  it('discards the queued utterance, whose only back-out left with the slot', () => {
    // Escape reaches the drain through the OWNING composer. Switch chats and that
    // composer is gone from the screen, so an engine left running holds the
    // microphone with nothing able to release it.
    const { hook, switchTo } = mount()
    enterDrain(hook)
    switchTo('slot-b')
    expect(fx.engine.cancel).toHaveBeenCalledTimes(1)
  })

  it('takes the run out of the draft it leaves behind, not just the engine', () => {
    // A switch abandons the utterance as squarely as Escape does, and the draft it
    // wrote into is persisted. Clearing the record before the rollback left the
    // discarded speech in that draft with the words it spoke over still deleted.
    const caretRef: { current: { start: number; end: number } | null } = { current: null }
    const { hook, inputRef, switchTo } = mount(STT_STREAMING, caretRef)
    inputRef.current = 'Please review the rollout plan'
    caretRef.current = { start: 7, end: 13 }
    fx.engine.recording = true
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    act(() => { void hook.result.current.startVoice() })
    fx.engine.partial = 'release'
    act(() => { fx.captured.onPartial?.('release', SESSION) })
    enterDrain(hook)
    expect(inputRef.current).toBe('Please release the rollout plan')
    switchTo('slot-b')
    expect(inputRef.current).toBe('Please review the rollout plan')
  })

  it('rolls the run back before a host parks the outgoing draft in a layout effect', () => {
    // One host parks the draft it is leaving in a LAYOUT effect, which runs before
    // every passive effect of the same commit. A passive discard would therefore
    // park the abandoned dictation and the park would keep the words it spoke
    // over deleted, with no copy anywhere to restore them from.
    const caretRef: { current: { start: number; end: number } | null } = { current: null }
    const { hook, inputRef, switchTo, parked } = mountUnderParkingHost(STT_STREAMING, caretRef)
    inputRef.current = 'Please review the rollout plan'
    caretRef.current = { start: 7, end: 13 }
    fx.engine.recording = true
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    act(() => { void hook.result.current.startVoice() })
    fx.engine.partial = 'release'
    act(() => { fx.captured.onPartial?.('release', SESSION) })
    enterDrain(hook)
    expect(inputRef.current).toBe('Please release the rollout plan')
    switchTo('slot-b')
    expect(parked['slot-a']).toBe('Please review the rollout plan')
  })

  it('leaves a draft it cannot place the run in alone on the way out', () => {
    // The host may swap the composer to the incoming slot's draft before this
    // runs. The locator refuses a value it cannot place the span in, so the
    // rollback is safe in either order.
    const caretRef: { current: { start: number; end: number } | null } = { current: null }
    const { hook, inputRef, switchTo } = mount(STT_STREAMING, caretRef)
    inputRef.current = 'please '
    fx.engine.recording = true
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    act(() => { void hook.result.current.startVoice() })
    fx.engine.partial = 'ship it'
    act(() => { fx.captured.onPartial?.('ship it', SESSION) })
    enterDrain(hook)
    expect(inputRef.current).toBe('please ship it')
    inputRef.current = 'a different chat, mid-sentence'
    switchTo('slot-b')
    expect(inputRef.current).toBe('a different chat, mid-sentence')
  })

  it('leaves a batch transcription to finish, since its audio is already gone', () => {
    // Batch has no drain: the blob is at the transcriber and its single final is
    // routed back to the slot that dictated it, so there is nothing to throw away.
    const { hook, switchTo } = mount(STT_BATCH)
    fx.engine.recording = false
    fx.engine.transcribing = true
    fx.engine.draining = false
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    switchTo('slot-b')
    expect(fx.engine.cancel).not.toHaveBeenCalled()
  })

  it('discards a streaming capture the user walks out on, rather than committing it', () => {
    // The commit path cannot deliver here: the switch disarms the streaming final
    // one line earlier, so stopping only starts a drain nobody can end — the new
    // slot does not own the session, so its Escape is inert, and the microphone
    // stays claimed until the engine's own timeout.
    const { hook, switchTo } = mount()
    fx.engine.recording = true
    fx.engine.transcribing = false
    fx.engine.draining = false
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    switchTo('slot-b')
    expect(fx.engine.cancel).toHaveBeenCalledTimes(1)
    expect(fx.engine.toggle).not.toHaveBeenCalled()
  })

  it('still commits a batch capture, whose transcript reaches the slot that spoke it', () => {
    // Batch keeps the pre-existing contract: stop and transcribe. Only streaming,
    // whose final this switch has already dropped, is discarded.
    const { hook, switchTo } = mount(STT_BATCH)
    fx.engine.recording = true
    fx.engine.transcribing = false
    fx.engine.draining = false
    fx.engine.sessionOwner = SESSION
    act(() => { hook.rerender() })
    switchTo('slot-b')
    expect(fx.engine.toggle).toHaveBeenCalledTimes(1)
    expect(fx.engine.cancel).not.toHaveBeenCalled()
  })
})
