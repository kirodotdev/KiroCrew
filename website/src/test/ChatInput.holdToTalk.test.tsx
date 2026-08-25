import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { createAudioSample } from '../hooks/mic'
import { HOLD_MS_DEFAULT } from '../lib/pushToTalk'

/**
 * Hold-to-talk mode: the WeChat-style swap where the mic becomes a mode switch
 * and the textarea is replaced by a press-and-hold target.
 *
 * The gesture's own state machine is covered in
 * `src/hooks/useTouchPushToTalk.test.ts`. What is covered HERE is the wiring
 * ChatInput owns: which surface is mounted, and — the part a reviewer caught —
 * that a draft arriving mid-capture does not tear the hold target out from under
 * the finger.
 *
 * Cases that claim a gesture DRIVE one, via the pointer helpers below. Hold-mode
 * survival over a draft is gated on the hook's exported ownership now, and
 * ownership is recorded only by the hook's own pointerdown — simulating capture
 * through props leaves the hook owning nothing, which is a DIFFERENT scenario
 * (the keyboard binding, or the mic-as-record-button). Two earlier tests here
 * asserted the wrong outcome precisely because their prop-only setup could not
 * tell the two apart (#5753).
 */

vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const sampleRef = { current: createAudioSample() }

/** The full voice wiring ChatPage supplies; hold mode needs start/stop/cancel. */
const voiceProps = {
  onVoiceToggle: vi.fn(),
  onVoiceCancel: vi.fn(),
  onVoicePrewarm: vi.fn(),
  onVoiceStart: vi.fn(),
  onVoiceStop: vi.fn(),
  voiceSampleRef: sampleRef,
}

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
}

/** Coarse pointer is what gates the feature — `isTouchDevice()` reads matchMedia. */
function stubTouch(coarse: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: coarse && /coarse|hover: none/.test(q),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

/** Where the test finger starts, in client coords. Well clear of the cancel zone. */
const ORIGIN_Y = 400

/** One real pointer event on the hold target — the hook listens natively, so
 *  these are dispatched, not fired through React's synthetic layer. */
function pointer(el: Element, type: string, init: PointerEventInit = {}) {
  act(() => {
    el.dispatchEvent(new PointerEvent(type, {
      pointerId: 1, pointerType: 'touch', isPrimary: true,
      bubbles: true, cancelable: true, clientX: 100, clientY: ORIGIN_Y, ...init,
    }))
  })
}
const pressHoldTarget = (el: Element) => pointer(el, 'pointerdown')
const releaseHoldTarget = (el: Element) => pointer(el, 'pointerup')

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  stubTouch(true)
})

describe('ChatInput — hold-to-talk mode', () => {
  it('offers the mode switch on touch and swaps the textarea for the hold target', () => {
    renderWithProviders(<ChatInput {...base} {...voiceProps} />)

    const toSpeech = screen.getByRole('button', { name: 'Switch to voice' })
    expect(screen.queryByTestId('hold-to-talk')).toBeNull()

    fireEvent.click(toSpeech)
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
    // The textarea stays MOUNTED (sr-only) so value, caret and IME state survive.
    expect(screen.getByLabelText('Message input')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch to keyboard' })).toBeTruthy()
  })

  it('does not offer the mode switch on a fine pointer', () => {
    stubTouch(false)
    renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    expect(screen.queryByRole('button', { name: 'Switch to voice' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Voice input' })).toBeTruthy()
  })

  // With a draft there is no mode to switch into, so the mic must go back to being
  // a record button. Disabling it instead removed dictating ONTO existing text for
  // every touch user — including anyone who never opened hold mode, since the mic is
  // the only voice entry point there.
  it('suspends hold mode with a draft and reverts the mic to a record control', () => {
    localStorage.setItem('mc-voice-mode', '1')
    renderWithProviders(<ChatInput {...base} {...voiceProps} value="a typed draft" />)

    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
    const mic = screen.getByRole('button', { name: 'Voice input' })
    expect((mic as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(mic)
    expect(voiceProps.onVoiceToggle).toHaveBeenCalled()
  })

  // The regression a reviewer caught on the streaming path: `onPartial` writes each
  // hypothesis into the composer WHILE the finger is still down, so suspending on
  // that draft would unmount the hold target mid-gesture — taking the pointer
  // listeners with it, so the release and the slide-up land on nothing while
  // capture keeps running. The finger goes down FOR REAL here: hold-mode survival
  // is gated on the hook's ownership, and only the hook's own pointerdown records
  // it — rendering with capture props alone is the not-owned scenario below.
  it('keeps the hold target mounted when a streaming partial lands mid-hold', () => {
    vi.useFakeTimers()
    try {
      localStorage.setItem('mc-voice-mode', '1')
      voiceProps.onVoiceStart.mockClear()
      voiceProps.onVoiceStop.mockClear()
      const { rerender } = renderWithProviders(<ChatInput {...base} {...voiceProps} />)
      const bar = screen.getByTestId('hold-to-talk')

      pressHoldTarget(bar)
      expect(voiceProps.onVoiceStart).toHaveBeenCalledTimes(1)
      // Past the hold threshold: the press is a recognised HOLD, not a tap.
      act(() => { vi.advanceTimersByTime(HOLD_MS_DEFAULT) })

      // Capture goes live and a partial arrives: the composer now holds text, but
      // the finger is still down — the bar must not be unmounted from under it.
      rerender(<ChatInput {...base} {...voiceProps} voiceRecording value="arm auto merge on" />)
      expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Switch to keyboard' })).toBeTruthy()

      // The finger lifts, committing the capture. Ownership ends AT the release,
      // so the draft reclaims the textarea immediately; the drain that remains
      // belongs to the mic, which is a record toggle again and can stop it —
      // instead of a disabled `settling` bar owning the surface for it.
      releaseHoldTarget(bar)
      expect(voiceProps.onVoiceStop).toHaveBeenCalledTimes(1)
      expect(screen.queryByTestId('hold-to-talk')).toBeNull()
      const mic = screen.getByRole('button', { name: 'Stop recording' })
      expect((mic as HTMLButtonElement).disabled).toBe(false)

      // Capture ends — the draft keeps the textarea.
      rerender(<ChatInput {...base} {...voiceProps} value="arm auto merge on" />)
      expect(screen.queryByTestId('hold-to-talk')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  // `voiceRecording` is ownership-gated (`owned && recording`) and lands only
  // after the streaming handshake, so during that window capture is real and the
  // gated flag still reads false. The hold target must not vanish there — that
  // is the mid-gesture unmount hazard, and it would also make the hook's commit
  // veto answer "no capture" for audio that exists. The gesture's own ownership
  // is already held (it is recorded at the pointerdown, before any handshake),
  // so what this case exercises is the UNGATED capture flag being the one the
  // hold-mode predicate pairs it with.
  it('keeps the hold target mounted on ungated capture, while the handshake is pending', () => {
    vi.useFakeTimers()
    try {
      localStorage.setItem('mc-voice-mode', '1')
      const { rerender } = renderWithProviders(
        <ChatInput {...base} {...voiceProps} voiceRecording={false} />,
      )
      pressHoldTarget(screen.getByTestId('hold-to-talk'))
      // Past the hold threshold: the press is a recognised HOLD, as the name says.
      act(() => { vi.advanceTimersByTime(HOLD_MS_DEFAULT) })

      // Ownership held, handshake pending (`voiceRecording` still false) while a
      // partial already fills the composer: the bar must survive on the gesture's
      // ownership plus the ungated flag alone.
      rerender(
        <ChatInput {...base} {...voiceProps} voiceRecording={false} voiceCaptureActive value="a streaming partial" />,
      )
      expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  // The scenario #5753 exists for: a coarse-pointer device that ALSO has a
  // hardware keyboard (an iPad with a keyboard case). Hold mode is on and the
  // composer empty, so the bar is mounted — and dictation starts from the
  // keyboard push-to-talk binding, which this composer's touch hook never sees.
  // The capture is real, but the gesture owns nothing, and a mounted bar must no
  // longer stand in for ownership: the old proxy (`holdTarget !== null`) kept
  // hold mode alive when the streaming partial landed, rendering a disabled
  // `settling` bar beside a disabled mode switch — two dead touch controls
  // describing a capture neither of them owned.
  it('does not keep a keyboard-binding capture in hold mode once its partial lands', () => {
    localStorage.setItem('mc-voice-mode', '1')
    const { rerender } = renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()

    // Keyboard PTT starts capture: no pointer ever touches the bar. While the
    // composer is still empty there is no draft to read, so hold mode
    // legitimately stays — the bar doubles as a "one mic at a time" stop target.
    rerender(<ChatInput {...base} {...voiceProps} voiceRecording voiceCaptureActive />)
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()

    // A streaming partial lands. The draft suspends hold mode exactly as if no
    // capture were running, because the touch gesture owns none of it: the
    // keyboard dictation keeps the ordinary composer surface.
    rerender(<ChatInput {...base} {...voiceProps} voiceRecording voiceCaptureActive value="a keyboard partial" />)
    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
    // ...and the capture keeps its own live stop control: the mic is a record
    // toggle again, enabled, labelled for the session it can actually end.
    const stop = screen.getByRole('button', { name: 'Stop recording' })
    expect((stop as HTMLButtonElement).disabled).toBe(false)
  })

  // The dictation panel OUTLIVES the gesture: its gate reads `voiceRecording`,
  // which stays true through the streaming drain after the release, while hold
  // mode has already dropped with the draft. The keyboard hint must stay
  // suppressed for that window — the drain is the gesture's own, and a thumb
  // has no Esc key — which is what the `settling` term in `gestureDriven`
  // carries.
  it("keeps the dictation panel keyboard hint suppressed through the gesture's own drain", () => {
    vi.useFakeTimers()
    try {
      localStorage.setItem('mc-voice-mode', '1')
      const { rerender } = renderWithProviders(
        <ChatInput {...base} {...voiceProps} voiceDictationPanel />,
      )
      pressHoldTarget(screen.getByTestId('hold-to-talk'))
      act(() => { vi.advanceTimersByTime(HOLD_MS_DEFAULT) })
      rerender(
        <ChatInput {...base} {...voiceProps} voiceDictationPanel voiceRecording value="a streaming partial" />,
      )

      // Release with the draft present: hold mode drops, capture keeps draining.
      releaseHoldTarget(screen.getByTestId('hold-to-talk'))
      rerender(
        <ChatInput {...base} {...voiceProps} voiceDictationPanel voiceRecording value="a streaming partial" />,
      )

      expect(screen.getByTestId('voice-dictation-panel')).toBeTruthy()
      expect(screen.queryByText(/Esc to cancel/)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  // ...and the same panel over a capture the gesture does NOT own keeps the
  // hint: a keyboard-binding dictation has a real Esc key and a mic that
  // finishes the recording, so suppressing the row there would hide a working
  // affordance.
  it('keeps the dictation panel keyboard hint for a keyboard-binding capture', () => {
    localStorage.setItem('mc-voice-mode', '1')
    renderWithProviders(
      <ChatInput {...base} {...voiceProps} voiceDictationPanel voiceRecording value="a keyboard partial" />,
    )
    expect(screen.getByTestId('voice-dictation-panel')).toBeTruthy()
    expect(screen.getByText(/Esc to cancel/)).toBeTruthy()
  })

  it('does not promote a draft composer into hold mode when dictation starts from the mic', () => {
    localStorage.setItem('mc-voice-mode', '1')
    // The saved preference is on, but a draft is already present, so the mic is a
    // record button and the bar was never mounted. Capture starting on that route
    // must NOT swap in a bar that renders `settling` (disabled) beside a disabled
    // mode switch — that leaves a live microphone with nothing able to stop it.
    renderWithProviders(
      <ChatInput {...base} {...voiceProps} voiceRecording voiceCaptureActive value="a draft the user typed" />,
    )

    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
    const stop = screen.getByRole('button', { name: 'Stop recording' })
    expect((stop as HTMLButtonElement).disabled).toBe(false)
  })

  // Label, icon, action and disabled state all come from one predicate, because
  // deriving them separately is how a control comes to say one thing and do
  // another. The path that caught it: a streaming partial lands mid-capture, so the
  // composer has a draft while capture is still live — the label read "Switch to
  // keyboard" (hold mode is still on, capture overrides the draft) while the click
  // stopped the recording instead.
  it('keeps the mic label and its action in agreement during streaming capture', () => {
    vi.useFakeTimers()
    try {
      localStorage.setItem('mc-voice-mode', '1')
      // The gesture is REAL, so this is a transcript landing mid-gesture rather
      // than dictation started over a draft — only ownership tells them apart.
      const { rerender } = renderWithProviders(<ChatInput {...base} {...voiceProps} />)
      pressHoldTarget(screen.getByTestId('hold-to-talk'))
      // Past the hold threshold: the press is a recognised HOLD.
      act(() => { vi.advanceTimersByTime(HOLD_MS_DEFAULT) })
      rerender(
        <ChatInput {...base} {...voiceProps} voiceRecording voiceCaptureActive value="a streaming partial" />,
      )

      // Hold mode survives the draft while the gesture owns the capture, so this is
      // still a switch.
      const mic = screen.getByRole('button', { name: 'Switch to keyboard' })
      // ...and a mode cannot be changed mid-capture, so the switch is disabled rather
      // than silently doing the other job.
      expect((mic as HTMLButtonElement).disabled).toBe(true)
      expect(screen.queryByRole('button', { name: 'Stop recording' })).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('disables the voice controls while ANOTHER session is still transcribing', () => {
    localStorage.setItem('mc-voice-mode', '1')
    // The gated flag is false — this slot owns nothing — but `startVoice` refuses
    // on the global one, so an enabled bar would invite a press and capture
    // nothing. Same shape as the capture split: an ownership-gated flag being
    // asked a question that is global.
    renderWithProviders(
      <ChatInput {...base} {...voiceProps} voiceTranscribing={false} voiceTranscribeActive />,
    )

    const bar = screen.getByTestId('hold-to-talk') as HTMLButtonElement
    expect(bar.disabled).toBe(true)
    expect(bar.textContent).toContain('Transcribing')

    // ...but the way OUT of voice mode must stay open. As a mode switch the mic
    // starts no capture, so a transcription in another session is no reason to
    // disable it — doing that strands this slot in voice mode with no way to type.
    const mic = screen.getByRole('button', { name: 'Switch to keyboard' }) as HTMLButtonElement
    expect(mic.disabled).toBe(false)
  })

  it('remembers the mode across mounts', () => {
    const first = renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    fireEvent.click(screen.getByRole('button', { name: 'Switch to voice' }))
    expect(localStorage.getItem('mc-voice-mode')).toBe('1')
    first.unmount()

    renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
  })
})
