import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import VoiceDisabledModal from '../components/VoiceDisabledModal'
import { modalUnavailableMessage, unavailableMessage } from '../lib/sttProviders'

/**
 * Covers the two voice-unavailable causes the modal must distinguish, and the
 * rule that the modal only shows a per-code reason whose remedy it can actually
 * offer — never a Settings-panel sentence that points "above"/"below" at a
 * control the modal does not have.
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

  it('renders the per-code reason for a SURFACE-NEUTRAL code (same wording Settings → Voice shows)', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="whisper" code="stt_extra_missing" onClose={noop} onOpenSettings={noop} />,
    )
    // stt_extra_missing is modal-safe: its sentence names no missing control.
    expect(screen.getByText(modalUnavailableMessage('stt_extra_missing'))).toBeInTheDocument()
    expect(modalUnavailableMessage('stt_extra_missing')).toBe(unavailableMessage('stt_extra_missing'))
    // The generic provider-named fallback must NOT appear once a known code is present.
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
  })

  it('does NOT reuse a Settings-panel sentence that points at a control the modal lacks', () => {
    // stt_model_missing's Settings wording ends "Download it below." — but the
    // modal has no download control. It must NOT point there, and must NOT fall
    // through to the generic "provider isn't installed" sentence (wrong cause:
    // the provider IS installed, only the model is missing). The modal-specific
    // override names the real cause and sends the user to Open settings.
    render(
      <VoiceDisabledModal open reason="unavailable" provider="whisper" code="stt_model_missing" onClose={noop} onOpenSettings={noop} />,
    )
    expect(screen.getByText(modalUnavailableMessage('stt_model_missing'))).toBeInTheDocument()
    expect(screen.queryByText(/download it below/i)).not.toBeInTheDocument()
    // Not the wrong-cause generic fallback.
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
    // The correct cause: the model, not the provider.
    expect(screen.getByText(/speech model isn't downloaded/i)).toBeInTheDocument()
  })

  it('shows the computed install command in a copyable block with a lead-in and a copy button', () => {
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
    // A real copy control, not a bare <pre> the user has to select by hand.
    expect(screen.getByRole('button', { name: /copy command/i })).toBeInTheDocument()
  })

  it('hides the generic "pick another provider" nudge when a concrete install command is shown', () => {
    // The nudge "Pick an installed provider … or install the current one"
    // contradicts a specific pip command — telling a user just handed the fix to
    // go hunt in settings. It is hidden whenever a command is present.
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
    expect(screen.queryByText(/pick an installed provider/i)).not.toBeInTheDocument()
  })

  it('shows the generic nudge when there is NO install command (nothing to contradict it)', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="whisper" onClose={noop} onOpenSettings={noop} />,
    )
    expect(screen.getByText(/pick an installed provider/i)).toBeInTheDocument()
  })

  it('does NOT show a command block for an unavailable cause with no pip command (e.g. only ffmpeg is missing)', () => {
    // stt_no_wheel_for_platform does not produce a pip command; the composer derives
    // installCommand='' in that case (it never falls back to an ffmpeg prereq).
    // The modal must then hide the command block and its lead-in.
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
    // stt_no_wheel_for_platform is modal-safe (self-contained remedy), so its reason
    // renders. Assert the actual sentence — not just modalUnavailableMessage(code),
    // which would pass vacuously if the code were unknown and the message were ''.
    expect(modalUnavailableMessage('stt_no_wheel_for_platform')).toMatch(/no prebuilt speech recogniser/i)
    expect(screen.getAllByText(/no prebuilt speech recogniser/i).length).toBeGreaterThan(0)
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
    expect(screen.getByText(/turn on voice input/i)).toBeInTheDocument()
    unmount()

    // Neutral title covers every cause (import, CPU, model, crash), not just a
    // missing provider.
    render(<VoiceDisabledModal open reason="unavailable" provider="whisper" onClose={noop} onOpenSettings={noop} />)
    expect(screen.getByText(/voice input can't run/i)).toBeInTheDocument()
    expect(screen.queryByText(/turn on voice input/i)).not.toBeInTheDocument()
  })
})
