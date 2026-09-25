/**
 * The drain's exit and the drain's cancellability must agree.
 *
 * One rule, asserted in both directions: a discard control on screen means
 * pressing it really ends the session, and a wait that cannot be ended offers no
 * control. The failure this forbids is the middle case - a control that restyles
 * the strip while the work carries on, which reads as a stop that happened.
 *
 * The capture-phase Escape case is the passing control: it holds whatever the
 * drain window does, so a green suite here cannot come from a broken harness.
 * The "really cancels" half of the rule is asserted at the hook, in
 * useStreamingStt.drainCancel.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
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

/**
 * The state `useStreamingStt.stop()` leaves behind when the release beats the
 * recogniser: capture is over, so `voiceRecording` is false, while the socket is
 * still held and `voiceTranscribing` is up.
 */
const DRAIN = {
  voiceRecording: false,
  voiceTranscribing: true,
  voiceStreaming: true,
  voiceDictationPanel: true,
  voiceSampleRef: sampleRef,
  voiceDeviceLabel: 'Mic',
}

/** The stage-announced half of the same window, where a figure is reportable. */
const DOWNLOAD = { done: 400_000_000, total: 1_600_000_000, stage: 'downloading' as const }

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }))
})

describe('drain cancel - shown means it cancels', () => {
  it('offers a pressable discard while the backend has announced nothing', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.getByTestId('voice-drain-cancel')).toBeInTheDocument()
  })

  it('pressing it calls the discard, which is the path that ends the session', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, onVoiceCancel }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    fireEvent.click(screen.getByTestId('voice-drain-cancel'))
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('offers the same discard while a model download is reporting progress', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceDownload: DOWNLOAD, onVoiceCancel }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.getByTestId('voice-status-download')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('voice-drain-cancel'))
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('names what the silent wait is waiting on', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    const strip = screen.getByTestId('voice-status-draining')
    expect(strip).toHaveTextContent('Waiting for the speech model.')
    expect(strip).toHaveTextContent('Your dictation is kept and will be transcribed.')
  })

  it('reaches the discard by touch, with a label and not an icon alone', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    const button = screen.getByTestId('voice-drain-cancel')
    expect(button.tagName).toBe('BUTTON')
    expect(button).toHaveAccessibleName('Discard')
    expect(button.textContent).toContain('Discard')
  })
})

describe('drain cancel - nothing to cancel means nothing shown', () => {
  it('shows no discard when no dictation is in flight', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceTranscribing: false, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.queryByTestId('voice-drain-cancel')).toBeNull()
    expect(screen.queryByTestId('voice-status-draining')).toBeNull()
  })

  it('shows no discard during a batch transcription, which it could not stop', () => {
    // Batch audio is already with the transcriber over HTTP, so the transcript
    // lands whatever the strip does. An exit here would be a claim the code
    // cannot honour, so the window gets none.
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceStreaming: false, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.queryByTestId('voice-drain-cancel')).toBeNull()
  })

  it('shows no discard while capture is still live, where the panel owns the window', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceRecording: true, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.queryByTestId('voice-drain-cancel')).toBeNull()
  })

  it('shows no discard when the host wires no discard at all', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, onVoiceCancel: undefined }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.queryByTestId('voice-drain-cancel')).toBeNull()
  })
})

describe('control - the capture-phase Escape exit is unaffected', () => {
  it('Escape during capture still reaches the discard', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, voiceRecording: true, onVoiceCancel }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })
})
