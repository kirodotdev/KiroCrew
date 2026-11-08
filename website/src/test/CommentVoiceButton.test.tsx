import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode, useState } from 'react'

// A hand-driven engine: the test plays the transcriber by calling `onText` and
// by flipping the engine flags, then re-rendering.
const engine = vi.hoisted(() => ({
  recording: false,
  transcribing: false,
  drainCancellable: false,
  sessionOwner: null as string | null,
  error: null as string | null,
  start: vi.fn(),
  stop: vi.fn(),
  cancel: vi.fn(),
  clearError: vi.fn(),
  onText: null as null | ((text: string, sid: string | null) => void),
  sessionId: null as string | null,
}))
vi.mock('../hooks/useVoiceInput', () => ({
  voiceInputSupported: true,
  useVoiceInput: (onText: (t: string, sid: string | null) => void, opts: { sessionId?: string | null }) => {
    engine.onText = onText
    engine.sessionId = opts.sessionId ?? null
    return { ...engine }
  },
}))
const apiMock = vi.hoisted(() => ({ sttConfig: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))

import { CommentVoiceButton } from '../components/CommentVoiceButton'
import { _resetMicOwner, readMicOwner, setMicOwner } from '../chat-core/composer/useComposerVoice'
import { spliceDictationText } from '../lib/dictationText'

const ON = { enabled: true, available: true, streaming: false }
let qc: QueryClient

function wrap(ui: React.ReactElement) {
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function Box({ target = 'a', onError }: { target?: string; onError?: (m: string | null) => void }) {
  const [text, setText] = useState('Hello')
  return (<>
    <textarea aria-label="box" value={text} onChange={e => setText(e.target.value)} />
    <CommentVoiceButton key={target} onError={onError} onText={t => setText(prev => spliceDictationText(prev, t.trim(), null).value)} />
  </>)
}

const box = () => (screen.getByLabelText('box') as HTMLTextAreaElement).value

beforeEach(() => {
  Object.assign(engine, { recording: false, transcribing: false, drainCancellable: false, sessionOwner: null, error: null })
  engine.start.mockReset(); engine.stop.mockReset(); engine.cancel.mockReset()
  engine.clearError.mockReset().mockImplementation(() => { engine.error = null })
  _resetMicOwner()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['sttConfig'], ON)
})

describe('CommentVoiceButton', () => {
  it('fills the box with the transcript and never sends it', () => {
    render(wrap(<Box />))
    fireEvent.click(screen.getByTestId('comment-voice'))
    expect(engine.start).toHaveBeenCalledTimes(1)
    act(() => { engine.onText!('please fix this', engine.sessionId) })
    expect(box()).toBe('Hello please fix this')
  })

  it('ignores a transcript for another session', () => {
    render(wrap(<Box />))
    act(() => { engine.onText!('not mine', 'chat-slot-1') })
    expect(box()).toBe('Hello')
  })

  it('is hidden while voice is off, and without a query client', () => {
    qc.setQueryData(['sttConfig'], { enabled: false, available: true })
    const { unmount } = render(wrap(<Box />))
    expect(screen.queryByTestId('comment-voice')).toBeNull()
    unmount()
    render(<CommentVoiceButton onText={() => {}} />)
    expect(screen.queryByTestId('comment-voice')).toBeNull()
  })

  it('does not start while another composer holds the mic', () => {
    setMicOwner('someone-else')
    render(wrap(<Box />))
    const btn = screen.getByTestId('comment-voice') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(engine.start).not.toHaveBeenCalled()
  })

  it('a second press stops, and closing the box mid-recording discards', () => {
    engine.recording = true
    const { unmount } = render(wrap(<Box />))
    fireEvent.click(screen.getByTestId('comment-voice'))
    expect(engine.stop).toHaveBeenCalledTimes(1)
    unmount()
    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(readMicOwner()).toBeNull()
  })

  it('closing the box while the mic is still starting cancels the startup', () => {
    engine.start.mockReturnValue(new Promise(() => {}))
    const { unmount } = render(wrap(<Box />))
    fireEvent.click(screen.getByTestId('comment-voice'))
    unmount()
    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(readMicOwner()).toBeNull()
  })

  it('a new target drops the old dictation and its late transcript', () => {
    engine.recording = true
    const { rerender } = render(wrap(<Box target="thread-a" />))
    const oldSink = engine.onText!
    const oldSlot = engine.sessionId
    rerender(wrap(<Box target="thread-b" />))
    expect(engine.cancel).toHaveBeenCalledTimes(1)
    expect(engine.sessionId).not.toBe(oldSlot)
    act(() => { oldSink('for thread a', oldSlot) })
    expect(box()).toBe('Hello')
  })

  it('reports its own failure once and lets the audio go', async () => {
    const onError = vi.fn()
    engine.start.mockResolvedValue(undefined)
    const { rerender } = render(wrap(<Box onError={onError} />))
    fireEvent.click(screen.getByTestId('comment-voice'))
    expect(onError).toHaveBeenLastCalledWith(null)
    // The capture starts, then the transcription fails.
    engine.recording = true; engine.sessionOwner = engine.sessionId
    await act(async () => {})
    rerender(wrap(<Box onError={onError} />))
    engine.recording = false; engine.sessionOwner = null; engine.error = 'Transcription failed'
    rerender(wrap(<Box onError={onError} />))
    expect(onError).toHaveBeenLastCalledWith('Transcription failed')
    expect(engine.clearError).toHaveBeenCalledTimes(1)
    // A later chat failure is not this button's.
    engine.error = 'Chat failed'
    rerender(wrap(<Box onError={onError} />))
    expect(onError).toHaveBeenCalledTimes(2)
  })

  it('still delivers under StrictMode, which runs the close cleanup once on mount', () => {
    render(<StrictMode>{wrap(<Box />)}</StrictMode>)
    fireEvent.click(screen.getByTestId('comment-voice'))
    act(() => { engine.onText!('dev mode words', engine.sessionId) })
    expect(box()).toBe('Hello dev mode words')
  })

  it('says so when the voice settings cannot be read', async () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    apiMock.sttConfig.mockRejectedValueOnce(new Error('down'))
    const onError = vi.fn()
    render(wrap(<Box onError={onError} />))
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith("Couldn't load voice settings."))
    expect(screen.queryByTestId('comment-voice')).toBeNull()
  })
})
