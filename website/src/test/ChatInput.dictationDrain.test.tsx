import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'

/**
 * The window AFTER the dictation key is released and BEFORE the speech model
 * has a transcript: capture has ended, the utterance is queued behind the
 * recogniser, and on a first run with a cold model that queue is a weight fetch
 * plus a load into memory — minutes, not the ~1-2s an append normally takes.
 *
 * `voiceRecording` answers "is the mic capturing", so it is false for the whole
 * window; `voiceDraining` is what names it. The composer stays protected here on
 * purpose (a send would orphan the pending transcript, and a second mic press
 * would race it), which is exactly why the window needs its own way out: Escape
 * discards the held utterance and releases the one-mic mutex.
 */

vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

/** Released, and the recogniser has not produced anything yet. */
const draining = { voiceRecording: false, voiceDraining: true, voiceTranscribing: true, voiceTranscribeActive: true }

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false,
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
})

describe('ChatInput — Escape backs out of a released dictation', () => {
  it('discards while the mic is still capturing', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ voiceRecording: true, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('discards after release while a cold model is still loading', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('discards from the textarea too, so a typed-into composer is not trapped', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('routes to the discard and never to the mic-button toggle', () => {
    // The toggle reads "is the mic capturing" to pick its action, and capture has
    // already ended here, so it would START a fresh dictation on top of the one
    // the user is trying to abandon. A drain has no commit gesture: without a
    // real discard handler Escape keeps its other meanings.
    const onVoiceToggle = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceToggle }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('leaves a batch transcription alone — that audio is already at the transcriber', () => {
    // A batch transcript is the only copy of what was said and no discard can
    // recall it, so Escape must not pretend to.
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ voiceRecording: false, voiceDraining: false, voiceTranscribing: true, voiceTranscribeActive: true, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })

  it('detaches once the drain ends', () => {
    const onVoiceCancel = vi.fn()
    const { rerender } = renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    rerender(
      <ComposerVoiceSliceOverride inputProps={{ voiceRecording: false, voiceDraining: false, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })

  it('yields to an open dialog during the drain, like it does while capturing', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <>
        <div role="dialog">a modal</div>
        <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceCancel }}><ChatInput {...base} /></ComposerVoiceSliceOverride>
      </>,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })
})

describe('ChatInput — the drain keeps its other guards', () => {
  it('still refuses Enter, so the pending transcript is not orphaned', () => {
    const onSend = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceCancel: vi.fn() }}><ChatInput {...base} value="foo" connected onSend={onSend} /></ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('still blocks the mic button, so a second press cannot race the pending one', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...draining, onVoiceCancel: vi.fn(), onVoiceToggle: vi.fn() }}><ChatInput {...base} /></ComposerVoiceSliceOverride>,
    )
    expect(screen.getByRole('button', { name: 'Transcribing…' })).toBeDisabled()
  })
})
