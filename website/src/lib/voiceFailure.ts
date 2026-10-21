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
  // The host has no built-in speech engine, so the generic "try again" is
  // wrong: there is nothing to try until one is installed or another provider
  // is picked.
  if (code === 'voice_system_no_engine') {
    return i18nT('components.voicePlaybackNotice.system_no_engine', { speak })
  }
  // Amazon Polly does not offer the selected voice/engine in the selected
  // region — AWS rejects every sentence, so the fix is a different
  // voice/engine/region in the settings, not a retry.
  if (code === 'voice_polly_rejected') {
    return i18nT('components.voicePlaybackNotice.polly_rejected', { speak })
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
