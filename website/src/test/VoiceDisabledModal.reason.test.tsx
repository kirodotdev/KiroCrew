import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import VoiceDisabledModal from '../components/VoiceDisabledModal'
import { unavailableMessage } from '../lib/sttProviders'

/**
 * Covers the two voice-unavailable causes the modal must distinguish.
 *
 * With stt.enabled=true but the provider binary absent, the backend answers
 * GET /api/config/stt with available:false and POST /api/stt/transcribe with
 * 503 {"error":"STT not available"}. The gate fires before recording
 * (ChatPage's toggleVoice), so this modal must explain the RIGHT thing: an
 * "enable it" instruction is wrong for a user who already has it enabled.
 */
describe('VoiceDisabledModal reason variants', () => {
  const noop = () => {}

  it("defaults to the disabled copy, so today's callers are unchanged", () => {
    render(<VoiceDisabledModal open onClose={noop} onOpenSettings={noop} />)
    expect(screen.getByText(/not enabled yet/i)).toBeInTheDocument()
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
  })

  it('names the provider and does NOT say "enable it" when unavailable with no known code', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="whisper" onClose={noop} onOpenSettings={noop} />,
    )
    // With no code supplied, the body falls back to the provider-named sentence.
    expect(screen.getByText(/isn't installed on this machine/i)).toBeInTheDocument()
    expect(screen.getByText(/whisper/i)).toBeInTheDocument()
    // And must NOT tell an already-enabled user to enable it.
    expect(screen.queryByText(/not enabled yet/i)).not.toBeInTheDocument()
  })

  it('renders the backend per-code reason — the same wording Settings → Voice shows', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="whisper" code="stt_extra_missing" onClose={noop} onOpenSettings={noop} />,
    )
    // Identical to what Settings → Voice renders for this code.
    expect(screen.getByText(unavailableMessage('stt_extra_missing'))).toBeInTheDocument()
    // The generic provider-named fallback must NOT appear once a known code is present.
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
  })

  it('shows the computed install command in a copyable block with a lead-in', () => {
    render(
      <VoiceDisabledModal
        open
        reason="unavailable"
        provider="whisper"
        code="stt_extra_missing"
        installCommand="pip install 'kiro-crew[voice]'"
        onClose={noop}
        onOpenSettings={noop}
      />,
    )
    expect(screen.getByText("pip install 'kiro-crew[voice]'")).toBeInTheDocument()
    expect(screen.getByText(/run the command below in a terminal/i)).toBeInTheDocument()
  })

  it('does NOT show a command block for an unavailable cause with no pip command (e.g. only ffmpeg is missing)', () => {
    // stt_no_wheel_for_platform (or stt_import_failed / stt_model_missing) does
    // not produce a pip command; the composer derives installCommand='' in that
    // case (it never falls back to an ffmpeg prereq). The modal must then hide
    // the command block and its "install voice support" lead-in — running an
    // ffmpeg install would not fix any of those causes.
    render(
      <VoiceDisabledModal
        open
        reason="unavailable"
        provider="whisper"
        code="stt_no_wheel_for_platform"
        installCommand=""
        onClose={noop}
        onOpenSettings={noop}
      />,
    )
    expect(screen.queryByText(/run the command below in a terminal/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/apt-get install/i)).not.toBeInTheDocument()
    // The per-code reason still renders for this cause.
    expect(screen.getByText(unavailableMessage('stt_no_wheel_for_platform'))).toBeInTheDocument()
  })

  it('still routes to Settings in the unavailable state, with a code and command', () => {
    const onOpenSettings = vi.fn()
    render(
      <VoiceDisabledModal
        open
        reason="unavailable"
        provider="mlx"
        code="stt_extra_missing"
        installCommand="pip install 'kiro-crew[voice]'"
        onClose={noop}
        onOpenSettings={onOpenSettings}
      />,
    )
    screen.getByText(/open settings/i).click()
    expect(onOpenSettings).toHaveBeenCalledOnce()
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
})
