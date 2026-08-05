import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { createAudioSample } from '../hooks/mic'

/**
 * Dictation-mode surface swap + the Escape affordance.
 *
 * The panel is gated on three independent conditions (setting on, WebGL2
 * present, motion not reduced) and deliberately yields to the status bar on a
 * mic error, since only the bar can surface a dismissible error.
 */

vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const sampleRef = { current: createAudioSample() }

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

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

describe('ChatInput — dictation panel', () => {
  it('shows the dictation panel instead of the status bar while recording', () => {
    renderWithProviders(
      <ChatInput {...base} voiceRecording voiceDictationPanel voiceSampleRef={sampleRef} voiceDeviceLabel="Mic" />,
    )
    expect(screen.getByTestId('voice-dictation-panel')).toBeInTheDocument()
    // The old thin bar's label must not also be on screen.
    expect(screen.queryByText('Recording')).toBeNull()
  })

  it('keeps the thin status bar when the setting is off', () => {
    renderWithProviders(
      <ChatInput {...base} voiceRecording voiceDictationPanel={false} voiceSampleRef={sampleRef} />,
    )
    expect(screen.queryByTestId('voice-dictation-panel')).toBeNull()
    expect(screen.getByText('Recording')).toBeInTheDocument()
  })

  it('falls back to the status bar on a mic error so the error stays dismissible', () => {
    const onClearVoiceError = vi.fn()
    renderWithProviders(
      <ChatInput
        {...base}
        voiceRecording
        voiceDictationPanel
        voiceSampleRef={sampleRef}
        voiceError="Microphone permission denied."
        onClearVoiceError={onClearVoiceError}
      />,
    )
    expect(screen.queryByTestId('voice-dictation-panel')).toBeNull()
    expect(screen.getByText('Microphone permission denied.')).toBeInTheDocument()
  })

  it('renders neither surface when not recording', () => {
    renderWithProviders(<ChatInput {...base} voiceDictationPanel voiceSampleRef={sampleRef} />)
    expect(screen.queryByTestId('voice-dictation-panel')).toBeNull()
    expect(screen.queryByText('Recording')).toBeNull()
  })

  it('collapses the textarea so the transcript is not rendered twice', () => {
    // The panel REPLACES the textarea (chosen design). The textarea stays
    // mounted and focusable — Enter-to-send routes through its handler — but it
    // must not also display the same text next to the panel.
    renderWithProviders(
      <ChatInput
        {...base}
        value="summarize the startup fix"
        voiceRecording
        voiceDictationPanel
        voiceSampleRef={sampleRef}
      />,
    )
    const ta = screen.getByLabelText('Message input')
    expect(ta.parentElement?.className).toContain('sr-only')
    // Still focusable: an unfocusable textarea would make "Enter to send" a lie.
    expect(document.activeElement).toBe(ta)
  })

  it('leaves the textarea visible when the panel is not showing', () => {
    renderWithProviders(<ChatInput {...base} value="hello" />)
    const ta = screen.getByLabelText('Message input')
    expect(ta.parentElement?.className).not.toContain('sr-only')
  })

  it('shows the composer text in the panel, so what is shown is what will be sent', () => {
    renderWithProviders(
      <ChatInput
        {...base}
        value="summarize the startup fix"
        voiceRecording
        voiceDictationPanel
        voiceSampleRef={sampleRef}
      />,
    )
    expect(screen.getByTestId('voice-dictation-transcript').textContent).toBe('summarize the startup fix')
  })
})

describe('ChatInput — Escape while recording', () => {
  it('stops recording when focus is on the textarea', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Escape' })
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
  })

  it('stops recording when focus is NOT in the composer', () => {
    // Starting a recording means clicking the mic BUTTON, so focus sits there,
    // not in the textarea. A textarea-scoped handler never fires, and the
    // panel's "Esc to cancel" hint would be a lie.
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
  })

  it('prefers onVoiceCancel (discard) over onVoiceToggle (commit) when both exist', () => {
    // Esc CANCELS: it must route to the discard path, not the mic-button commit
    // path, so an abandoned dictation is thrown away rather than transcribed.
    const onVoiceToggle = vi.fn()
    const onVoiceCancel = vi.fn()
    renderWithProviders(
      <ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} onVoiceCancel={onVoiceCancel} />,
    )
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('detaches the listener when recording stops', () => {
    const onVoiceToggle = vi.fn()
    const { rerender } = renderWithProviders(
      <ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />,
    )
    rerender(<ChatInput {...base} voiceRecording={false} onVoiceToggle={onVoiceToggle} />)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('does nothing when not recording (Escape keeps its other meanings)', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording={false} onVoiceToggle={onVoiceToggle} />)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Escape' })
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('yields to an open slash menu — that picker owns Escape to close itself', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    const ta = screen.getByLabelText('Message input')
    // Typing "/" opens the slash menu (setSlashMenuOpen in onChange).
    fireEvent.change(ta, { target: { value: '/' } })
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(onVoiceToggle).not.toHaveBeenCalled()
  })

  it('lets a DESCENDANT consume Escape first (proves bubble, not capture, phase)', () => {
    // The discriminating test. A descendant that preventDefaults on Escape --
    // what Radix menus/popovers do when they close -- must win. Under CAPTURE
    // registration the document handler runs BEFORE the descendant and would
    // fire, so this test fails if capture is ever reinstated. Note the event is
    // dispatched from inside the component and is NOT pre-cancelled: cancelling
    // it beforehand would make the test pass under either phase.
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    const ta = screen.getByLabelText('Message input')
    const consume = (e: Event) => e.preventDefault()
    ta.addEventListener('keydown', consume)
    try {
      ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
      expect(onVoiceToggle).not.toHaveBeenCalled()
    } finally {
      ta.removeEventListener('keydown', consume)
    }
  })

  it('does not let the same Escape reach a window-level handler', () => {
    // SnipOverlay binds a window keydown that cancels the snip and does NOT
    // check defaultPrevented. document bubbles on to window, so without
    // stopPropagation() one Escape would stop recording AND cancel a snip
    // started during recording.
    const onVoiceToggle = vi.fn()
    const windowHandler = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    window.addEventListener('keydown', windowHandler)
    try {
      document.body.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      )
      expect(onVoiceToggle).toHaveBeenCalledTimes(1)
      expect(windowHandler).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowHandler)
    }
  })

  it('still fires when nothing consumed Escape', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    document.body.dispatchEvent(ev)
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
  })

  it('defers to an open dialog — Modal, CommandPalette and SnipOverlay all bind window Escape', () => {
    // Escape belongs to the topmost dismissible surface. Those overlays own it,
    // so recording defers to them rather than claiming it. All three carry
    // role="dialog", so one presence probe defers to every one of them.
    const onVoiceToggle = vi.fn()
    const dialogHandler = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    window.addEventListener('keydown', dialogHandler)
    try {
      document.body.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      )
      // Recording continues; the dialog's own window handler still gets the key.
      expect(onVoiceToggle).not.toHaveBeenCalled()
      expect(dialogHandler).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('keydown', dialogHandler)
      dialog.remove()
    }
  })

  it('resumes claiming Escape once the dialog closes', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    expect(onVoiceToggle).not.toHaveBeenCalled()
    dialog.remove()
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
  })

  it('does not fire without an onVoiceToggle handler', () => {
    renderWithProviders(<ChatInput {...base} voiceRecording />)
    expect(() => fireEvent.keyDown(document.body, { key: 'Escape' })).not.toThrow()
  })
})

describe('ChatInput — send is gated while a batch transcript is pending', () => {
  it('does NOT send on Enter while voiceTranscribing (transcript not landed yet)', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput {...base} value="foo" connected voiceTranscribing onSend={onSend} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('sends on Enter once transcription has finished', () => {
    const onSend = vi.fn()
    const { rerender } = renderWithProviders(
      <ChatInput {...base} value="foo bar" connected voiceTranscribing onSend={onSend} />,
    )
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<ChatInput {...base} value="foo bar" connected voiceTranscribing={false} onSend={onSend} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })
})
