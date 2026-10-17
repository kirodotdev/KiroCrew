/**
 * A discard must roll the dictated words out of the composer whenever the live
 * utterance put them there.
 *
 * Streaming partials are spliced into the draft as they arrive, so a discard owes
 * the user two things: end the session, and take those words back out. Gating the
 * second on the saved streaming MODE breaks the pair, because the mode and the
 * live session come apart: turning streaming off mid-capture is converted by the
 * engine's own effect into a drain, so the session is still streaming while the
 * mode already reads batch. The discard then closes the socket and leaves the
 * discarded sentence sitting in the draft - the user is told it is gone and can
 * see that it is not.
 *
 * So the forward case here flips the mode AGAINST the live transport. The
 * mode-agreeing case is kept as the passing control, and a batch session is the
 * reverse case: nothing was spliced, so nothing may be removed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

type Transport = 'batch' | 'stream' | null
type Engine = {
  recording: boolean; transcribing: boolean; sessionOwner: string | null; streamEnabled: boolean
  transport: Transport; drainCancellable: boolean
  toggle: () => void; start: () => Promise<void>; stop: () => void; cancel: () => void; prewarm: () => void
  error: string | null; level: number; deviceLabel: string; deviceId: string; clearError: () => void; partial: string
  download: null; sampleRef: { current: object }; switchDevice: () => void; deviceSwitchIsLive: boolean
}

const fx = vi.hoisted(() => {
  const state: {
    engine: null | Record<string, unknown>
    opts: null | { onPartial?: (text: string, sessionId: string | null) => void }
  } = { engine: null, opts: null }
  return { state }
})

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: (_onText: unknown, opts: unknown) => {
    fx.state.opts = opts as { onPartial?: (t: string, s: string | null) => void }
    return fx.state.engine
  },
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../api/client', () => ({
  api: { sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: true, dictation_panel: true, provider: 'local' }) },
}))

import { useComposerVoice, _resetMicOwner } from './useComposerVoice'

const STT_ON = { enabled: true, available: true, streaming: true, dictation_panel: true, provider: 'local' }

function makeEngine(over: Partial<Engine> = {}): Engine {
  return {
    recording: false, transcribing: false, sessionOwner: 'slot-a', streamEnabled: true,
    transport: null, drainCancellable: false,
    toggle: vi.fn(), start: vi.fn(async () => {}), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
    ...over,
  }
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sttConfig'], STT_ON)
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

/** Mount one composer over a caller-owned draft, so the draft is assertable. */
function mount(engine: Engine, draft = '') {
  fx.state.engine = engine as unknown as Record<string, unknown>
  const inputRef = { current: draft }
  const view = renderHook(
    () => useComposerVoice({ sessionId: 'slot-a', inputRef, setInput: (v: string) => { inputRef.current = v } }),
    { wrapper: makeWrapper() },
  )
  return { view, inputRef }
}

/**
 * Open a streaming session the way the product does and land one partial in the
 * draft. Going through `startVoice` matters: it is what clears the disarm flags a
 * fresh mount leaves set, so a partial driven around it would be dropped and the
 * rollback would have nothing to roll back.
 */
async function openStreamingWithPartial(
  engine: Engine, draft: string, partial: string, caret?: { start: number; end: number },
) {
  const m = mount(engine, draft)
  await act(async () => { await m.view.result.current.startVoice() })
  // The composer reports where its caret is; with one inside the draft the
  // dictation is spliced THERE, so the draft's own tail follows the dictated
  // words. Without one it appends, which is the shape the cases below default to.
  if (caret) m.view.result.current.voiceCaretRef.current = caret
  engine.recording = true
  engine.transport = 'stream'
  engine.partial = partial
  m.view.rerender()
  act(() => { fx.state.opts?.onPartial?.(partial, 'slot-a') })
  return m
}

beforeEach(() => { fx.state.engine = null; fx.state.opts = null; _resetMicOwner() })

describe('discard rolls the dictated region out of the composer', () => {
  it('when the mode was turned off mid-utterance and the session is still streaming', async () => {
    const engine = makeEngine()
    const { view, inputRef } = await openStreamingWithPartial(engine, 'my note ', 'hello world')
    expect(inputRef.current).toContain('hello world')

    // The user turns streaming OFF in Settings. The engine converts that into a
    // drain: its socket is still the live session, while the saved mode now reads
    // batch. This is the pairing the engine's own effect manufactures.
    engine.streamEnabled = false
    engine.recording = false
    engine.transport = 'stream'
    engine.drainCancellable = true
    view.rerender()

    act(() => { view.result.current.cancelVoice() })

    // Both halves of the discard: the session ended AND the words are gone.
    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(inputRef.current).toBe('my note ')
  })

  it('control: the same rollback when the mode still agrees with the session', async () => {
    const engine = makeEngine()
    const { view, inputRef } = await openStreamingWithPartial(engine, 'my note ', 'hello world')
    expect(inputRef.current).toContain('hello world')

    act(() => { view.result.current.cancelVoice() })

    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(inputRef.current).toBe('my note ')
  })

  it('keeps a suffix the user typed after the dictation', async () => {
    const engine = makeEngine()
    const { view, inputRef } = await openStreamingWithPartial(engine, 'my note ', 'hello world')
    const written = inputRef.current
    inputRef.current = written + ' and mine'
    engine.streamEnabled = false
    engine.transport = 'stream'
    view.rerender()

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('my note  and mine')
  })

  /**
   * MID-DRAFT is the ordinary shape, not an edge: the caret is restored to the END
   * of the dictated region, so anything typed during the drain lands BETWEEN that
   * region and the draft's own tail. The composer then reads `head + typed + tail`,
   * which matching the whole written value as a prefix cannot recognise - so the
   * discard falls through to leaving the draft alone, and the sentence the user was
   * told had been discarded stays on screen. The two ends are verified separately
   * here, which identifies the typed run exactly.
   */
  it('removes the region when drain-time typing landed in the middle of the draft', async () => {
    const engine = makeEngine()
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'Hi team, see you', 'one moment', { start: 8, end: 8 })
    expect(inputRef.current).toBe('Hi team, one moment see you')

    // The user lets go. The socket keeps draining and the composer is theirs to
    // type in again, at a caret sitting in the middle of their own draft.
    act(() => { view.result.current.stopVoice() })
    engine.recording = false
    engine.drainCancellable = true
    view.rerender()
    inputRef.current = 'Hi team, one moment plus mine see you'

    act(() => { view.result.current.cancelVoice() })

    // The dictated words are gone and every character the user authored survives:
    // their own typing where they put it, the draft's tail untouched.
    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(inputRef.current).toBe('Hi team, plus mine see you')
  })

  /**
   * The words the user typed during the drain arrive back in a draft whose
   * separators went out with the dictation. `spliceDictationText` puts a space at
   * each junction of the region it writes, so both of those spaces live inside the
   * two pieces the rollback drops - and a typed run spliced back in raw is glued
   * to whatever it now touches. The user asked for the dictation to go away and
   * got their own sentence damaged instead, which is the discard failing at the
   * one job it has. Each case below leaves exactly ONE junction needing a
   * separator, so a rollback that re-derives only the other one still fails.
   */
  it('keeps the words apart at the trailing junction the dictation had spaced', async () => {
    const engine = makeEngine()
    // The caret sits after the draft's own space, so the dictation needs no
    // leading separator and the one it adds is the TRAILING one, inside `tail`.
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'Hi team, see you', 'one moment', { start: 9, end: 9 })
    expect(inputRef.current).toBe('Hi team, one moment see you')

    act(() => { view.result.current.stopVoice() })
    engine.recording = false
    engine.drainCancellable = true
    view.rerender()
    // Typed at the restored caret, which is the end of the dictated region: no
    // space of its own on either side.
    inputRef.current = 'Hi team, one momentplus see you'

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('Hi team, plus see you')
  })

  it('keeps the words apart at the leading junction the dictation had spaced', async () => {
    const engine = makeEngine()
    // Mirror image: the draft's tail carries its own space, so the separator the
    // dictation added is the LEADING one, inside `head`.
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'abc def', 'one moment', { start: 3, end: 3 })
    expect(inputRef.current).toBe('abc one moment def')

    act(() => { view.result.current.stopVoice() })
    engine.recording = false
    engine.drainCancellable = true
    view.rerender()
    inputRef.current = 'abc one momentplus def'

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('abc plus def')
  })

  /**
   * Dictating over a SELECTION replaces it, so the rollback owes two things that
   * are easy to get half right: give the selected words back, and put the run
   * typed during the drain AFTER them. The caret the user typed at was restored to
   * the end of the region that stood in the selection's place, so their typing
   * belongs where that region ended -- rebuilding at the selection's start
   * restores the words but files the typing in front of them, which is their own
   * sentence reordered rather than their dictation removed.
   */
  it('restores a replaced selection and keeps drain-time typing after it', async () => {
    const engine = makeEngine()
    // The user selects "SEL" in AAASELBBB and dictates over it.
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'AAASELBBB', 'one moment', { start: 3, end: 6 })
    expect(inputRef.current).toBe('AAA one moment BBB')

    act(() => { view.result.current.stopVoice() })
    engine.recording = false
    engine.drainCancellable = true
    view.rerender()
    inputRef.current = 'AAA one momentT BBB'

    act(() => { view.result.current.cancelVoice() })

    // Selected words back, exactly once, with the typing after them.
    expect(inputRef.current).toBe('AAASEL T BBB')
  })

  it('restores a selection that ran to the end of the draft', async () => {
    const engine = makeEngine()
    // The selection reaches the draft's end, so the replaced span has no tail
    // after it and the resume point is clamped by the draft's own length.
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'keep this', 'one moment', { start: 5, end: 9 })
    expect(inputRef.current).toBe('keep one moment')

    act(() => { view.result.current.stopVoice() })
    engine.recording = false
    engine.drainCancellable = true
    view.rerender()
    inputRef.current = 'keep one momentT'

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('keep this T')
  })

  /**
   * Restoring focus makes the caret's position matter, and a controlled value
   * replacement does not leave it where it was: React swaps the textarea value and
   * the browser puts the DOM caret at the end. So the discard has to say where the
   * caret belongs -- after the run the user typed, not behind the draft tail it
   * restored -- the same compensation the delivery path in this file already makes.
   */
  it('leaves the caret after the typing, not at the end of the restored draft', async () => {
    const engine = makeEngine()
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'Hi team, see you', 'one moment', { start: 8, end: 8 })

    act(() => { view.result.current.stopVoice() })
    engine.recording = false
    engine.drainCancellable = true
    view.rerender()
    inputRef.current = 'Hi team, one moment plus mine see you'

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('Hi team, plus mine see you')
    // Right after "mine", which is where they were typing -- NOT after "you".
    expect(view.result.current.voicePendingCaretRef.current)
      .toBe('Hi team, plus mine'.length)
  })

  it('leaves the draft alone when the user rewrote the dictated region itself', async () => {
    const engine = makeEngine()
    const { view, inputRef } = await openStreamingWithPartial(
      engine, 'Hi team, see you', 'one moment', { start: 8, end: 8 })

    // They replaced the dictated words with their own, so nothing can tell their
    // text from ours. Matching on the tail alone would delete "mine" with it.
    const rewritten = 'Hi team, mine see you'
    inputRef.current = rewritten

    act(() => { view.result.current.cancelVoice() })

    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(inputRef.current).toBe(rewritten)
  })
})

describe('discard removes nothing a batch utterance did not write', () => {
  it('leaves the draft alone for a batch session even with the mode set to streaming', async () => {
    // Batch splices no partials, so there is no dictated region to take back.
    // Removing anything here would delete text the user authored.
    const engine = makeEngine()
    const { view, inputRef } = mount(engine, 'my note')
    await act(async () => { await view.result.current.startVoice() })
    engine.recording = true
    engine.transport = 'batch'
    engine.streamEnabled = true
    view.rerender()

    act(() => { view.result.current.cancelVoice() })

    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(inputRef.current).toBe('my note')
  })

  it('leaves the draft alone with no utterance in flight at all', () => {
    const engine = makeEngine({ transport: null, streamEnabled: true })
    const { view, inputRef } = mount(engine, 'my note')

    act(() => { view.result.current.cancelVoice() })

    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(inputRef.current).toBe('my note')
  })
})
