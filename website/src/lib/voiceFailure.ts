import { i18nT } from '../i18n/t'
import { recordError, type ErrorReport } from '../utils/errorReport'

export interface VoiceFailure {
  slot: string | null
  code: string
  request_id?: string
  report: ErrorReport
}

export const voiceFailureMessage = (code: string) => {
  const speak = i18nT('pages.chat.assistantMessage.speak')
  if (code === 'voice_playback_blocked') {
    return i18nT('components.voicePlaybackNotice.blocked', {
      moreActions: i18nT('pages.chat.assistantMessage.more_actions'),
      speak,
    })
  }
  // No Amazon Polly consent is recorded for the selected profile and region.
  // Retrying cannot help until the operator confirms it, so the sentence names
  // that step.
  if (code === 'voice_consent_required') {
    return i18nT('components.voicePlaybackNotice.consent_required', { speak })
  }
  return i18nT('components.voicePlaybackNotice.failed', { speak })
}

/** Journal at the playback owner: ChatPage may be unmounted when audio fails.
 * The notice receives this same report, so presenting it never records it twice. */
export function reportVoiceFailure(detail: Omit<VoiceFailure, 'report'>): void {
  const report = recordError({
    source: 'system', message: voiceFailureMessage(detail.code),
    code: detail.code, endpoint: '/api/voice/synthesize',
  })
  window.dispatchEvent(new CustomEvent<VoiceFailure>('voice-error', { detail: { ...detail, report } }))
}
