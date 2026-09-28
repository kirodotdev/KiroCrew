/**
 * The drain's exit and the drain's cancellability must agree.
 *
 * One rule, asserted in both directions: a discard control on screen means
 * pressing it really ends the session, and a wait that cannot be ended offers no
 * control. The failure this forbids is the middle case - a control that restyles
 * the strip while the work carries on, which reads as a stop that happened.
 *
 * Both directions run over BOTH transports, and each case flips the streaming
 * SETTING against the transport actually in flight. That pairing is what gives
 * the suite teeth: while setting and transport agree, a gate reading either one
 * looks correct, so a suite that exercises only the streaming path with the
 * setting agreeing cannot see a batch window that shows an exit and discards
 * nothing.
 *
 * The capture-phase Escape case is the passing control: it holds whatever the
 * drain window does, so a green suite here cannot come from a broken harness.
 * The "really cancels" half of the rule is asserted at the hooks, in
 * useVoiceInput.transportAnchoredCancel.test.tsx and
 * useStreamingStt.drainCancel.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, cleanup } from '@testing-library/react'
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
 * The released-utterance window, with no transport chosen yet: capture is over,
 * so `voiceRecording` is false, while the composer still holds a dictation.
 */
const DRAIN = {
  voiceRecording: false,
  voiceTranscribing: true,
  voiceDictationPanel: true,
  voiceSampleRef: sampleRef,
  voiceDeviceLabel: 'Mic',
}

/**
 * The two transports an in-flight utterance can be on, each with the streaming
 * setting flipped to the OTHER one - the mid-request Settings change that makes
 * setting and transport disagree.
 */
const TRANSPORTS = [
  {
    /** Socket held open by this composer's own session, so a discard closes it. */
    transport: 'streaming',
    cancellable: true,
    props: { voiceDrainCancellable: true, voiceStreaming: false },
  },
  {
    /** Blob already POSTed to the transcriber, so no press can call it back. */
    transport: 'batch',
    cancellable: false,
    props: { voiceDrainCancellable: false, voiceStreaming: true },
  },
]

/** The stage-announced half of the same window, where a figure is reportable. */
const DOWNLOAD = { done: 400_000_000, total: 1_600_000_000, stage: 'downloading' as const }

/**
 * What a press costs, verbatim from the catalogue. Both halves matter: the loss
 * is final, and the user's own typing survives it. Without the second half the
 * reader will not press, which is the whole finding - a control nobody dares use
 * is not an exit.
 */
const CONSEQUENCE = 'Discarding cannot be undone, but anything you typed yourself stays, and you can dictate again.'

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }))
})

describe.each(TRANSPORTS)('drain on the $transport transport', ({ cancellable, props }) => {
  const render = (extra: Record<string, unknown> = {}) => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, ...props, onVoiceCancel, ...extra }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    return onVoiceCancel
  }

  it('offers an exit exactly when this request can be called off', () => {
    render()
    expect(!!screen.queryByTestId('voice-drain-cancel')).toBe(cancellable)
  })

  it('offers the same exit, and only then, while a model download reports progress', () => {
    render({ voiceDownload: DOWNLOAD })
    expect(screen.getByTestId('voice-status-download')).toBeInTheDocument()
    expect(!!screen.queryByTestId('voice-drain-cancel')).toBe(cancellable)
  })

  it('pressing the exit reaches the discard, which is the path that ends the session', () => {
    const onVoiceCancel = render()
    const button = screen.queryByTestId('voice-drain-cancel')
    if (!cancellable) {
      // Nothing to press is the correct outcome here; the wait is explained by
      // the composer's own transcribing placeholder instead.
      expect(button).toBeNull()
      return
    }
    fireEvent.click(button!)
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('tells the reader what the press costs, at the control, wherever the control is', () => {
    // Both strips, because the control appears in both and the sentence has to
    // travel with it: the silent window, and the one reporting a download figure.
    for (const extra of [{}, { voiceDownload: DOWNLOAD }]) {
      renderWithProviders(
        <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, ...props, onVoiceCancel: vi.fn(), ...extra }}>
          <ChatInput {...base} />
        </ComposerVoiceSliceOverride>,
      )
      const button = screen.queryByTestId('voice-drain-cancel')
      if (!cancellable) {
        // No control, so nothing to describe. The sentence must not be on screen
        // either: a consequence with no act to consequence for is noise.
        expect(button).toBeNull()
        expect(screen.queryByText(CONSEQUENCE)).toBeNull()
        cleanup()
        continue
      }
      // The name says what it does. The description says what it costs - and it
      // has to be ON the control, because a reader who tabs to the button is not
      // reading the strip around it.
      expect(button).toHaveAccessibleName('Discard dictation')
      expect(button).toHaveAccessibleDescription(CONSEQUENCE)
      // The very node the sighted reader sees, addressed by id - one sentence
      // told to both, never a hidden copy that can drift from the visible one.
      const described = button!.getAttribute('aria-describedby')
      expect(described).toBeTruthy()
      expect(document.getElementById(described!)?.textContent).toBe(CONSEQUENCE)
      cleanup()
    }
  })
})

describe('the streaming drain, in full', () => {
  const STREAM = { ...DRAIN, ...TRANSPORTS[0].props }

  it('names what the silent wait is waiting on, and offers the exit in the same breath', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    const strip = screen.getByTestId('voice-status-draining')
    expect(strip).toHaveTextContent('Getting ready to transcribe your dictation.')
    // One sentence, not a reassurance the exit appears to contradict.
    expect(strip).toHaveTextContent('Your dictation is kept and will be transcribed, or discard it to stop waiting.')
  })

  it('answers what the discard does not stop, but only where a download is reported', () => {
    const { unmount } = renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, voiceDownload: DOWNLOAD, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.getByTestId('voice-status-download'))
      .toHaveTextContent('The download continues even if you discard, so nothing is fetched twice.')
    unmount()

    // No announced stage, so there is no download to be truthful about.
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.getByTestId('voice-status-draining'))
      .not.toHaveTextContent('The download continues even if you discard')
  })

  /**
   * The press destroys its own button: the discard clears the drain, the strip
   * unmounts, and the focused element goes with it. Nothing else puts focus back -
   * the composer's focus effect is keyed on the capture panel and on a batch
   * transcript, and a streaming drain is neither - so focus lands on the document
   * body and the composer stops hearing the keyboard. For a control added FOR the
   * keyboard and touch paths, that is the exit closing the door behind itself.
   */
  it('hands focus back to the composer, which the press would otherwise destroy', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    const button = screen.getByTestId('voice-drain-cancel')
    // Focus where a keyboard user pressing it actually is, so the assertion is
    // about focus MOVING rather than about where it happened to start.
    button.focus()
    expect(document.activeElement).toBe(button)

    fireEvent.click(button)

    expect(document.activeElement).toBe(screen.getByRole('textbox'))
  })

  it('reaches the discard by touch, with a label that names what is discarded', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    const button = screen.getByTestId('voice-drain-cancel')
    expect(button.tagName).toBe('BUTTON')
    expect(button).toHaveAccessibleName('Discard dictation')
    expect(button.textContent).toContain('Discard dictation')
  })
})

describe('drain cancel - nothing to cancel means nothing shown', () => {
  const STREAM = { ...DRAIN, ...TRANSPORTS[0].props }

  it('shows no discard when no dictation is in flight', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, voiceTranscribing: false, voiceDrainCancellable: false, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.queryByTestId('voice-drain-cancel')).toBeNull()
    expect(screen.queryByTestId('voice-status-draining')).toBeNull()
  })

  it('shows no discard while capture is still live, where the panel owns the window', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, voiceRecording: true, onVoiceCancel: vi.fn() }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    expect(screen.queryByTestId('voice-drain-cancel')).toBeNull()
  })

  it('shows no discard when the host wires no discard at all', () => {
    renderWithProviders(
      <ComposerVoiceSliceOverride inputProps={{ ...STREAM, onVoiceCancel: undefined }}>
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
      <ComposerVoiceSliceOverride inputProps={{ ...DRAIN, ...TRANSPORTS[0].props, voiceRecording: true, onVoiceCancel }}>
        <ChatInput {...base} />
      </ComposerVoiceSliceOverride>,
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })
})
