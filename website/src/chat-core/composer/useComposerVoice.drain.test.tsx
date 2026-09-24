import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
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
  useVoiceInput: (onText: Captured['onText'], opts: { onPartial?: Captured['onPartial'] }) => {
    fx.captured.onText = onText
    fx.captured.onPartial = opts?.onPartial
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

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sttConfig'], STT_STREAMING)
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

const SESSION = 'slot-a'

function mount() {
  const inputRef = { current: '' }
  const hook = renderHook(
    () => useComposerVoice({ sessionId: SESSION, inputRef, setInput: (v: string) => { inputRef.current = v } }),
    { wrapper: makeWrapper() },
  )
  return { hook, inputRef }
}

beforeEach(() => {
  _resetMicOwner()
  const e = fx.engine
  e.recording = false; e.transcribing = false; e.draining = false; e.sessionOwner = null; e.partial = ''
  e.cancel = vi.fn()
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
