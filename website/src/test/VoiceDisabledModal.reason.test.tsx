import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import VoiceDisabledModal from '../components/VoiceDisabledModal'

/**
 * Regression cover for the "Transcription request failed." dead end.
 *
 * With stt.enabled=true but the provider binary absent, the backend answers
 * GET /api/config/stt with available:false and POST /api/stt/transcribe with
 * 503 {"error":"STT not available"}. Nothing consulted `available`, so the mic
 * recorded a full take and only the upload failed — surfacing a transport-shaped
 * message for a configuration problem.
 *
 * The gate now fires before recording (ChatPage's toggleVoice) and this modal
 * must explain the RIGHT thing: an "enable it" instruction is wrong for a user
 * who already has it enabled.
 */
describe('VoiceDisabledModal reason variants', () => {
  const noop = () => {}

  it("defaults to the disabled copy, so today's callers are unchanged", () => {
    render(<VoiceDisabledModal open onClose={noop} onOpenSettings={noop} />)
    expect(screen.getByText(/not enabled yet/i)).toBeInTheDocument()
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
  })

  it('names the provider and does NOT say "enable it" when unavailable', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="whisper" onClose={noop} onOpenSettings={noop} />,
    )
    // The body must state the real cause and name the provider.
    expect(screen.getByText(/isn't installed on this machine/i)).toBeInTheDocument()
    expect(screen.getByText(/whisper/i)).toBeInTheDocument()
    // And must NOT tell an already-enabled user to enable it.
    expect(screen.queryByText(/not enabled yet/i)).not.toBeInTheDocument()
  })

  it('titles the two states differently so the cause is visible at a glance', () => {
    const { unmount } = render(<VoiceDisabledModal open onClose={noop} onOpenSettings={noop} />)
    const disabledTitle = screen.getByText(/turn on voice input/i)
    expect(disabledTitle).toBeInTheDocument()
    unmount()

    render(<VoiceDisabledModal open reason="unavailable" provider="whisper" onClose={noop} onOpenSettings={noop} />)
    expect(screen.getByText(/provider not installed/i)).toBeInTheDocument()
    expect(screen.queryByText(/turn on voice input/i)).not.toBeInTheDocument()
  })

  it('still routes to Settings in the unavailable state', () => {
    const onOpenSettings = vi.fn()
    render(
      <VoiceDisabledModal open reason="unavailable" provider="mlx" onClose={noop} onOpenSettings={onOpenSettings} />,
    )
    screen.getByText(/open settings/i).click()
    expect(onOpenSettings).toHaveBeenCalledOnce()
  })
})
