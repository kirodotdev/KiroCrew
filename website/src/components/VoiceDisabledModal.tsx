import { Mic } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'

import { i18nT } from '../i18n/t'
interface Props {
  /** Whether the modal is open */
  open: boolean
  /** Close without navigating */
  onClose: () => void
  /** Navigate the user to the STT setting (Settings -> Voice) */
  onOpenSettings: () => void
}

/**
 * Shown when the user clicks the mic but server-side speech-to-text is
 * disabled. Recording while STT is off would capture audio that never gets
 * transcribed, so instead of silently failing we explain why and link to the
 * setting that turns it on.
 */
export default function VoiceDisabledModal({ open, onClose, onOpenSettings }: Props) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('components.voiceDisabledModal.turn_on_voice_input')}
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
            {i18nT('components.voiceDisabledModal.speech_to_text_is_not_enabled_yet_so_the_microph')}
          </p>
          <p className="text-muted">
            {i18nT('components.voiceDisabledModal.enable_it_under')} <span className="text-text font-medium">{i18nT('components.voiceDisabledModal.settings_voice')}</span>{i18nT('components.voiceDisabledModal.then_click_the_mic_to_dictate_into_the_message_b')}
          </p>
        </div>
      </div>
    </Modal>
  )
}
