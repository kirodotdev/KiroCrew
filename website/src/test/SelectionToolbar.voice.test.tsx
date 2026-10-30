import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRef } from 'react'

// A hand-driven engine: the test plays the transcriber by calling `onText`.
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
vi.mock('../api/client', () => ({ api: { sttConfig: vi.fn() } }))

import { ComposerBox } from '../components/SelectionToolbar'
import { _resetMicOwner } from '../chat-core/composer/useComposerVoice'
import { i18nT } from '../i18n/t'

let qc: QueryClient

function box(submitting: boolean, onTextChange: (next: string) => void, onGrow = () => {}) {
  return (
    <QueryClientProvider client={qc}>
      <ComposerBox
        inputRef={createRef<HTMLTextAreaElement>()}
        autoFocus={false}
        actions={[]}
        copiedId={null}
        hintIdBase="h"
        onAction={() => {}}
        onSubmit={() => {}}
        onEscape={() => {}}
        onCopyShortcut={async () => true}
        onGrow={onGrow}
        text="looks wrong"
        onTextChange={onTextChange}
        copyFailed={false}
        onDismissCopyFailed={() => {}}
        submitting={submitting}
        submitFailed={false}
        onDismissSubmitFailed={() => {}}
        voiceTarget={1}
      />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  engine.error = null
  _resetMicOwner()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming: false })
})

describe('ComposerBox dictation', () => {
  it('re-measures after the voice notice appears and disappears', async () => {
    const onGrow = vi.fn(() => screen.queryByRole('alert')?.textContent ?? null)
    const onTextChange = vi.fn()
    const { rerender } = render(box(false, onTextChange, onGrow))
    onGrow.mockClear()

    // Fail the real voice button's engine without changing the draft or props.
    engine.error = 'Transcription failed'
    await act(async () => { fireEvent.click(screen.getByTestId('comment-voice')) })
    expect(screen.getByRole('alert')).toHaveTextContent('Transcription failed')
    expect(onGrow).toHaveReturnedWith(expect.stringContaining('Transcription failed'))

    onGrow.mockClear()
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(onGrow).toHaveReturnedWith(null)
    expect(onTextChange).not.toHaveBeenCalled()

    // An ordinary rerender must not keep asking the parent to grow.
    onGrow.mockClear()
    rerender(box(false, onTextChange, onGrow))
    expect(onGrow).not.toHaveBeenCalled()
    engine.error = null
  })

  it('adds a transcript to the draft while no post is in flight', () => {
    const onTextChange = vi.fn()
    render(box(false, onTextChange))
    act(() => { engine.onText!('please fix', engine.sessionId) })
    expect(onTextChange).toHaveBeenCalledWith('looks wrong please fix')
  })

  // The draft store keeps a slot whose text differs from the posted one, so a
  // transcript written mid-post would survive the post as a stale draft.
  it('drops a transcript that settles while the post is in flight', () => {
    const onTextChange = vi.fn()
    const { rerender } = render(box(false, onTextChange))
    rerender(box(true, onTextChange))
    act(() => { engine.onText!('please fix', engine.sessionId) })
    expect(onTextChange).not.toHaveBeenCalled()
  })
})
