import { Mic } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'
import { unavailableMessage } from '../lib/sttProviders'

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
   * When it names a reason this build knows, the modal renders the SAME per-code
   * sentence Settings → Voice shows (via `unavailableMessage`) instead of the
   * generic provider-named fallback — one wording for one cause across surfaces.
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
  // Prefer the backend's per-code reason — identical wording to Settings → Voice
  // — and only fall back to the generic provider-named sentence when no known
  // code is present. `unavailableMessage` returns '' for a code this build does
  // not recognise, which is what routes those through the fallback.
  const codeReason = unavailable ? unavailableMessage(code) : ''
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={unavailable
        ? i18nT('components.voiceDisabledModal.voice_provider_not_installed')
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
              <pre className="text-[12px] bg-bg-subtle border border-border rounded-md p-2 overflow-x-auto"><code>{installCommand}</code></pre>
            </div>
          ) : null}
          <p className="text-muted">
            {unavailable
              ? i18nT('components.voiceDisabledModal.pick_an_installed_provider_under_settings_voice')
              : <>{i18nT('components.voiceDisabledModal.enable_it_under')} <span className="text-text font-medium">{i18nT('components.voiceDisabledModal.settings_voice')}</span>{i18nT('components.voiceDisabledModal.then_click_the_mic_to_dictate_into_the_message_b')}</>}
          </p>
        </div>
      </div>
    </Modal>
  )
}
