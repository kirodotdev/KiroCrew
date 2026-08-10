import { Mic } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'

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
   * - `'remote'` — this dashboard is a remote instance in an iframe. Voice
   *   input captures audio on the gateway host, not the parent machine, so
   *   cross-instance voice won't work. The user should use the local dashboard.
   */
  reason?: 'disabled' | 'unavailable' | 'remote'
  /** Configured provider name, named in the `'unavailable'` copy. */
  provider?: string
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
export default function VoiceDisabledModal({ open, reason = 'disabled', provider = '', onClose, onOpenSettings }: Props) {
  const unavailable = reason === 'unavailable'
  const remote = reason === 'remote'
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={remote
        ? i18nT('components.voiceDisabledModal.voice_not_available_remote')
        : unavailable
          ? i18nT('components.voiceDisabledModal.voice_provider_not_installed')
          : i18nT('components.voiceDisabledModal.turn_on_voice_input')}
      maxWidth={440}
      footer={
        <>
          <Btn onClick={onClose}>{i18nT('components.voiceDisabledModal.not_now')}</Btn>
          {!remote && <Btn primary onClick={onOpenSettings}>{i18nT('components.voiceDisabledModal.open_settings')}</Btn>}
        </>
      }
    >
      <div className="flex gap-3.5">
        <div className="shrink-0 w-10 h-10 rounded-lg bg-accent/15 text-accent flex items-center justify-center">
          <Mic size={20} />
        </div>
        <div className="text-[13px] text-text leading-relaxed">
          <p className="mb-2">
            {remote
              ? i18nT('components.voiceDisabledModal.voice_remote_instance_body')
              : unavailable
                ? i18nT('components.voiceDisabledModal.provider_is_not_installed_on_this_machine', { provider })
                : i18nT('components.voiceDisabledModal.speech_to_text_is_not_enabled_yet_so_the_microph')}
          </p>
          <p className="text-muted">
            {remote
              ? i18nT('components.voiceDisabledModal.voice_remote_instance_hint')
              : unavailable
                ? i18nT('components.voiceDisabledModal.pick_an_installed_provider_under_settings_voice')
                : <>{i18nT('components.voiceDisabledModal.enable_it_under')} <span className="text-text font-medium">{i18nT('components.voiceDisabledModal.settings_voice')}</span>{i18nT('components.voiceDisabledModal.then_click_the_mic_to_dictate_into_the_message_b')}</>}
          </p>
        </div>
      </div>
    </Modal>
  )
}
