import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* `stt.submit_on_release` from the composer's side: when a deliberate finish
 * sends, and every case where it must leave a draft instead.
 *
 * The engine (`useVoiceInput`) is a mutable fake. The test plays the
 * recogniser by flipping `recording` / `transcribing` and calling the delivery
 * callbacks the hook handed in, then re-rendering, which is how the real engine
 * reports the same transitions. */

type Delivery = (text: string, sessionId: string | null, origin: 'stream' | 'batch') => void
type Opts = { onPartial?: (t: string, s: string | null) => void; onEndpoint?: () => void }

const fx = vi.hoisted(() => {
  const engine = {
    recording: false, transcribing: false, transport: null as 'stream' | 'batch' | null,
    streamEnabled: false, error: null as string | null,
    sessionOwner: null as string | null,
    toggle: () => {}, start: async () => {}, stop: () => {}, cancel: () => {}, prewarm: () => {},
    level: 0, deviceLabel: '', deviceId: '', clearError: () => {}, partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: () => {}, deviceSwitchIsLive: false,
    keptRecording: null, drainCancellable: false,
  }
  const captured = { onText: null as unknown, opts: null as unknown }
  const polish = { fn: null as unknown }
  return { engine, captured, polish }
})

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: unknown, opts: unknown) => {
    fx.captured.onText = onText
    fx.captured.opts = opts
    return fx.engine
  },
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../hooks/voiceTranscriptInbox', () => ({ redeliverPending: () => {} }))
vi.mock('../../lib/haptic', () => ({ haptic: () => {} }))
vi.mock('../../api/client', () => ({
  api: {
    sttConfig: vi.fn().mockResolvedValue({}),
    sttPolish: (...args: unknown[]) => (fx.polish.fn as (...a: unknown[]) => unknown)(...args),
  },
}))

import { useComposerVoice, _resetMicOwner } from './useComposerVoice'

function mount({ submit = true, streaming = false, polish = false } = {}) {
  const e = fx.engine
  Object.assign(e, { recording: false, transcribing: false, transport: null, error: null, streamEnabled: streaming, sessionOwner: null })
  e.start = vi.fn(async () => { e.recording = true; e.transport = streaming ? 'stream' : 'batch'; e.sessionOwner = slot.current })
  // Streaming keeps draining after the stop; batch goes quiet until the
  // recorder's async onstop hands the blob to the transcriber.
  e.stop = vi.fn(() => { e.recording = false; if (streaming) e.transcribing = true; else e.transport = null })
  e.cancel = vi.fn(() => { e.recording = false; e.transcribing = false; e.transport = null })
  e.toggle = vi.fn(() => { if (e.recording) e.stop() })

  const inputRef = { current: '' }
  const slot = { current: 'slot-a' }
  const onAutoSubmit = vi.fn(() => { sent.push(inputRef.current) })
  const sent: string[] = []
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, provider: 'local', streaming, polish, submit_on_release: submit })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  const view = renderHook(
    () => useComposerVoice({ sessionId: slot.current, inputRef, setInput: v => { inputRef.current = v }, onAutoSubmit }),
    { wrapper },
  )
  const render = () => act(() => { view.rerender() })
  const deliver = (text: string, origin: 'stream' | 'batch' = 'batch') =>
    act(() => { (fx.captured.onText as Delivery)(text, 'slot-a', origin) })
  const partial = (text: string) => act(() => { (fx.captured.opts as Opts).onPartial?.(text, 'slot-a') })
  const startRecording = async () => {
    await act(async () => { await view.result.current.startVoice() })
    render()
  }
  /** The transcriber picks the blob up, then answers with `text`. */
  const batchAnswers = (text: string | null) => {
    e.transcribing = true; render()
    act(() => { e.transcribing = false; if (text !== null) (fx.captured.onText as Delivery)(text, 'slot-a', 'batch') })
    render()
  }
  const drainEnds = () => { e.transcribing = false; e.transport = null; render() }
  const switchSlot = (id: string) => { slot.current = id; render() }
  return { view, inputRef, onAutoSubmit, sent, render, deliver, partial, startRecording, batchAnswers, drainEnds, switchSlot, e }
}

beforeEach(() => {
  _resetMicOwner()
  fx.polish.fn = vi.fn()
})

describe('submit on release: batch', () => {
  it('sends once, after the transcript lands', async () => {
    const t = mount()
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    // The recorder has stopped but nothing has been transcribed yet.
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    t.batchAnswers('hello there')
    expect(t.sent).toEqual(['hello there'])
    t.render(); t.render()
    expect(t.onAutoSubmit).toHaveBeenCalledTimes(1)
  })

  it('keeps a draft when the setting is off', async () => {
    const t = mount({ submit: false })
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.batchAnswers('hello there')
    expect(t.inputRef.current).toBe('hello there')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it.each([[undefined]])('keeps a draft for a stop with no intent (%s)', async intent => {
    const t = mount()
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice(intent) })
    t.batchAnswers('hello there')
    expect(t.inputRef.current).toBe('hello there')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('a mic click finishes, a Shift-click keeps a draft, and a bare call never sends', async () => {
    for (const [arg, expectSend] of [[{ shiftKey: false }, true], [{ shiftKey: true }, false], [undefined, false]] as const) {
      const t = mount()
      await t.startRecording()
      act(() => { t.view.result.current.toggleVoice(arg) })
      t.batchAnswers('hello')
      expect(t.onAutoSubmit).toHaveBeenCalledTimes(expectSend ? 1 : 0)
      t.view.unmount()
      _resetMicOwner()
    }
  })

  it('keeps a draft when the person typed while it was transcribing', async () => {
    const t = mount()
    t.inputRef.current = 'Re:'
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.inputRef.current = 'Re: also'
    t.batchAnswers('hello')
    expect(t.inputRef.current).toContain('hello')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('sends nothing for an empty or failed transcription', async () => {
    const t = mount()
    t.inputRef.current = 'typed before'
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.batchAnswers(null)
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    expect(t.inputRef.current).toBe('typed before')

    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.e.error = 'Transcription failed'
    t.batchAnswers(null)
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('never sends into another chat after a switch', async () => {
    const t = mount()
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.switchSlot('slot-b')
    t.batchAnswers('hello')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('a new capture drops a release still waiting', async () => {
    const t = mount()
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    await t.startRecording()
    t.batchAnswers('hello')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('waits for AI cleanup and sends the cleaned text', async () => {
    let resolve: (v: unknown) => void = () => {}
    fx.polish.fn = vi.fn(() => new Promise(r => { resolve = r }))
    const t = mount({ polish: true })
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.batchAnswers('hello there')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    await act(async () => { resolve({ ok: true, changed: true, text: 'Hello there.' }) })
    t.render()
    expect(t.sent).toEqual(['Hello there.'])
  })

  it('a late cleanup reply for a kept draft never sends through a later release', async () => {
    let resolve: (v: unknown) => void = () => {}
    fx.polish.fn = vi.fn(() => new Promise(r => { resolve = r }))
    const t = mount({ polish: true })
    // Capture 1 is kept as a draft; its cleanup is still in flight.
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice(undefined) })
    t.batchAnswers('kept draft')
    // Capture 2 is stopped deliberately but transcribes nothing.
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    await act(async () => { resolve({ ok: true, changed: true, text: 'Kept draft.' }) })
    t.render()
    expect(t.inputRef.current).toBe('Kept draft.')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('a recording too short to transcribe ends the release, and a later write does not send', async () => {
    const t = mount()
    t.inputRef.current = 'typed earlier'
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    // The engine drops a blob under 100 bytes and clears its owner.
    act(() => { t.e.sessionOwner = null })
    t.render()
    t.deliver('late text')
    t.render()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('still sends when the cleanup fails, with the raw transcript', async () => {
    fx.polish.fn = vi.fn(() => Promise.reject(new Error('boom')))
    const t = mount({ polish: true })
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.batchAnswers('hello there')
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    t.render()
    expect(t.sent).toEqual(['hello there'])
  })
})

describe('submit on release: streaming', () => {
  it('sends once, after the drain, with the corrected tail', async () => {
    const t = mount({ streaming: true })
    await t.startRecording()
    t.partial('hello')
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    t.partial('hello world')
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    // An endpoint verdict in the drain does not send: the release owns it.
    act(() => { (fx.captured.opts as Opts).onEndpoint?.() })
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    t.drainEnds()
    expect(t.sent).toEqual(['hello world'])
    t.render()
    expect(t.onAutoSubmit).toHaveBeenCalledTimes(1)
  })

  it('a send before the release leaves nothing to send twice', async () => {
    const t = mount({ streaming: true })
    await t.startRecording()
    t.partial('hello')
    // The endpointer (or Enter) sent mid-hold: the host's send disarms first.
    act(() => { t.view.result.current.disarmForSend() })
    t.inputRef.current = ''
    act(() => { t.view.result.current.stopVoice('finish') })
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('discarding the drain never sends', async () => {
    const t = mount({ streaming: true })
    await t.startRecording()
    t.partial('hello')
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    act(() => { t.view.result.current.cancelVoice() })
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('typing during the drain keeps a draft', async () => {
    const t = mount({ streaming: true })
    await t.startRecording()
    t.partial('hello')
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    t.inputRef.current = 'hello and more'
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
  })

  it('typing after the last partial, before the stop, keeps a draft', async () => {
    const t = mount({ streaming: true })
    await t.startRecording()
    t.partial('hello')
    t.inputRef.current = 'hello, edited'
    act(() => { t.view.result.current.stopVoice('finish') })
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    expect(t.inputRef.current).toBe('hello, edited')
  })

  it('an empty partial that retracts the hypothesis after the stop sends nothing', async () => {
    const t = mount({ streaming: true })
    t.inputRef.current = 'typed earlier'
    await t.startRecording()
    t.partial('hello')
    act(() => { t.view.result.current.stopVoice('finish') })
    t.render()
    t.partial('')
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    expect(t.inputRef.current).toBe('typed earlier')
  })

  it('a hypothesis already retracted before the stop sends nothing', async () => {
    const t = mount({ streaming: true })
    t.inputRef.current = 'typed earlier'
    await t.startRecording()
    t.partial('hello')
    t.partial('')
    act(() => { t.view.result.current.stopVoice('finish') })
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    expect(t.inputRef.current).toBe('typed earlier')
  })

  it('a silent recording over typed text sends nothing', async () => {
    const t = mount({ streaming: true })
    t.inputRef.current = 'typed earlier'
    await t.startRecording()
    act(() => { t.view.result.current.stopVoice('finish') })
    t.drainEnds()
    expect(t.onAutoSubmit).not.toHaveBeenCalled()
    expect(t.inputRef.current).toBe('typed earlier')
  })
})
