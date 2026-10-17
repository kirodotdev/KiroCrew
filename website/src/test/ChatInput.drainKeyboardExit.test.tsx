/**
 * Escape backs out of the released-utterance DRAIN, not only a live recording.
 *
 * On a streaming release capture ends (`voiceRecording` false) while the
 * utterance stays queued behind the model. Keyed on `voiceRecording` alone the
 * Escape-to-discard listener unbinds at that release and the composer locks with
 * no keyboard way out (#13500); it now stays bound through the cancellable drain
 * and discards. The live-recording cases are the control that proves the
 * listener binds at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import { createAudioSample } from '../hooks/mic'

vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const sampleRef = { current: createAudioSample() }
const base = { value: '', onChange: vi.fn(), onSend: vi.fn() }

/** The released-utterance window: capture is over, so `voiceRecording` is false,
 *  while the composer still holds a dictation waiting on the recogniser. */
const DRAIN = {
  voiceRecording: false,
  voiceTranscribing: true,
  voiceDictationPanel: true,
  voiceSampleRef: sampleRef,
  voiceDeviceLabel: 'Mic',
}

const pressEscape = () =>
  fireEvent.keyDown(document, { key: 'Escape', code: 'Escape', bubbles: true })

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }))
})

describe('Escape exits the dictation drain', () => {
  it('discards a cancellable streaming drain', () => {
    const onVoiceCancel = vi.fn()
    const onVoiceToggle = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceDrainCancellable: true, onVoiceCancel, onVoiceToggle }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    pressEscape()
    // Discarded through the rollback, never toggled (which would start a new
    // recording rather than call the held one off).
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('ignores Escape when the wait cannot be called off (batch)', () => {
    const onVoiceCancel = vi.fn()
    const onVoiceToggle = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceDrainCancellable: false, onVoiceCancel, onVoiceToggle }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    pressEscape()
    expect(onVoiceCancel).not.toHaveBeenCalled()
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('does not bind a drain Escape to toggle when no discard is wired', () => {
    // Through the drain the only honest action is the discard. With none wired,
    // Escape must bind nothing rather than fall back to toggle, which would open
    // a fresh recording on top of the one still draining.
    const onVoiceToggle = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceDrainCancellable: true, onVoiceCancel: undefined, onVoiceToggle }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    pressEscape()
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('still discards a live recording via Escape (control)', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceRecording: true, voiceDrainCancellable: false, onVoiceCancel }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    pressEscape()
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('a live recording with no cancel falls back to toggle (stop + discard)', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceRecording: true, voiceDrainCancellable: false, onVoiceCancel: undefined, onVoiceToggle }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    pressEscape()
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
  })
})
