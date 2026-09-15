import { Mic } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'
import { CopyCommand } from './agentHarness/CopyCommand'
import { modalUnavailableMessage } from '../lib/sttProviders'

import { i18nT } from '../i18n/t'
interface Props {
  /** Whether the modal is open */
  open: boolean
  /**
   * Why voice input is blocked, which decides the copy:
   *
   * - `'disabled'` — `stt.enabled` is false. The user must turn STT on.
   * - `'unavailable'` — STT is ON but the configured provider's binary is not
   *   installed (the backend's `available: false`). Telling this user to
   *   "enable it" is wrong — it IS enabled; they need a different provider or
   *   an install. Getting this wrong makes the failure unreadable: the mic
   *   records fine but the upload returns 503, surfacing as
   *   "Transcription request failed."
   */
  reason?: 'disabled' | 'unavailable'
  /** Configured provider name, named in the `'unavailable'` copy. */
  provider?: string
  /**
   * The backend's machine-readable availability `code` (e.g. `stt_extra_missing`).
   * The modal renders the shared per-code reason ONLY for a code whose wording is
   * surface-neutral (`modalUnavailableMessage`); a Settings-panel-only code (one
   * that says "above"/"below"/"Download it below") falls back to the generic
   * provider-named sentence, because the modal has no provider menu, enable toggle
   * or download control to point at — only the "Open settings" button below.
   */
  code?: string
  /**
   * The pip command the backend computed for fixing a missing voice extra
   * (`prereqs`), when it computed one. Shown verbatim in a copyable block so the
   * user can self-serve the fix.
   */
  installCommand?: string
  /** Close without navigating */
  onClose: () => void
  /** Navigate the user to the STT setting (Settings -> Voice) */
  onOpenSettings: () => void
}

/**
 * Shown when the user clicks the mic but server-side speech-to-text cannot
 * run. Recording while STT is unusable would capture audio that never gets
 * transcribed, so instead of silently failing we explain why and link to the
 * setting that fixes it.
 */
export default function VoiceDisabledModal({ open, reason = 'disabled', provider = '', code = '', installCommand = '', onClose, onOpenSettings }: Props) {
  const unavailable = reason === 'unavailable'
  // The per-code reason, but only for a code whose sentence does not point at a
  // control this modal lacks (`modalUnavailableMessage` returns '' for the
  // Settings-panel-only codes). Everything else falls back to the generic
  // provider-named sentence, whose remedy is the "Open settings" button below.
  const codeReason = unavailable ? modalUnavailableMessage(code) : ''

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={unavailable
        ? i18nT('components.voiceDisabledModal.voice_input_cannot_run')
        : i18nT('components.voiceDisabledModal.turn_on_voice_input')}
      maxWidth={440}
      footer={
        <>
          <Btn onClick={onClose}>{i18nT('components.voiceDisabledModal.not_now')}</Btn>
          <Btn primary onClick={onOpenSettings}>{i18nT('components.voiceDisabledModal.open_settings')}</Btn>
        </>
      }
    >
      <div className="flex gap-3.5">
        <div className="shrink-0 w-10 h-10 rounded-lg bg-accent/15 text-accent flex items-center justify-center">
          <Mic size={20} />
        </div>
        <div className="text-[13px] text-text leading-relaxed">
          <p className="mb-2">
            {unavailable
              ? (codeReason || i18nT('components.voiceDisabledModal.provider_is_not_installed_on_this_machine', { provider }))
              : i18nT('components.voiceDisabledModal.speech_to_text_is_not_enabled_yet_so_the_microph')}
          </p>
          {unavailable && installCommand ? (
            <div className="mb-2">
              <p className="text-muted mb-1.5">{i18nT('components.voiceDisabledModal.run_the_command_below_in_a_terminal_to_install_v')}</p>
              {/* The shared click-to-copy block (also used by the setup gate and
                  Settings → Agent Harness) — same look, Copied state and
                  copy-failure recovery everywhere, so this modal adds no third
                  copy affordance of its own. */}
              <CopyCommand><code>{installCommand}</code></CopyCommand>
            </div>
          ) : null}
          {/* The generic "pick another provider / install the current one" nudge
              is the right follow-up for the no-code fallback, but it CONTRADICTS a
              concrete pip command — telling a user who was just handed the exact
              fix to go hunt in settings instead. Hide it whenever a command is
              shown; the "Open settings" button stays either way. */}
          {unavailable
            ? (installCommand ? null : (
                <p className="text-muted">
                  {i18nT('components.voiceDisabledModal.pick_an_installed_provider_under_settings_voice')}
                </p>
              ))
            : (
                <p className="text-muted">
                  {i18nT('components.voiceDisabledModal.enable_it_under')} <span className="text-text font-medium">{i18nT('components.voiceDisabledModal.settings_voice')}</span>{i18nT('components.voiceDisabledModal.then_click_the_mic_to_dictate_into_the_message_b')}
                </p>
              )}
        </div>
      </div>
    </Modal>
  )
}
