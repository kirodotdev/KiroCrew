import { useEffect, useState } from 'react'
import { i18nT } from '../i18n/t'
import { voiceFailureMessage, type VoiceFailure } from '../lib/voiceFailure'
import ErrorNotice from './ErrorNotice'
import { SettingsLink } from './SettingsLink'

/** Playback can fail after the synthesize HTTP request has already succeeded. */
export default function VoicePlaybackNotice({ slot, onBlockedSlotChange }: {
  slot: string | null | undefined
  onBlockedSlotChange?: (slot: string | null) => void
}) {
  const [failure, setFailure] = useState<VoiceFailure | null>(null)
  useEffect(() => {
    setFailure(null)
    if (!slot) return
    const failed = (event: Event) => {
      const detail = (event as CustomEvent<VoiceFailure>).detail
      if (detail && detail.slot === slot) {
        setFailure(detail)
      }
    }
    const started = (event: Event) => {
      if ((event as CustomEvent<{ slot: string }>).detail?.slot === slot) setFailure(null)
    }
    const stopped = () => setFailure(null)
    window.addEventListener('voice-error', failed)
    window.addEventListener('voice-synthesis-start', started)
    window.addEventListener('voice-stop', stopped)
    return () => {
      window.removeEventListener('voice-error', failed)
      window.removeEventListener('voice-synthesis-start', started)
      window.removeEventListener('voice-stop', stopped)
    }
  }, [slot])

  const currentFailure = failure && failure.slot === slot ? failure : null
  const blockedSlot = currentFailure?.code === 'voice_playback_blocked' ? currentFailure.slot : null
  useEffect(() => {
    onBlockedSlotChange?.(blockedSlot)
    return () => onBlockedSlotChange?.(null)
  }, [blockedSlot, onBlockedSlotChange])
  const message = currentFailure ? voiceFailureMessage(currentFailure.code) : null
  if (!message) return null
  return <div className="mx-4 mt-2 mb-0 rounded-lg border border-danger/40 bg-danger/10" data-testid="voice-playback-error">
    <ErrorNotice
      variant="inline"
      className={`w-full px-3 pt-2 ${currentFailure?.code === 'voice_playback_blocked' ? 'pb-2' : ''} [@media(hover:none)]:[&_button]:min-h-10 [@media(hover:none)]:[&_button]:min-w-10`}
      message={message}
      report={currentFailure?.report}
      onDismiss={() => setFailure(null)}
      askAgent
    />
    {currentFailure?.code !== 'voice_playback_blocked' && (
      <SettingsLink
        tab="voice"
        highlight="voice.provider-2"
        className="mx-3 inline-flex min-h-10 max-w-[calc(100%-1.5rem)] items-center text-[13px] text-accent underline underline-offset-2"
      >
        {i18nT('components.voicePlaybackNotice.settings', {
          speak: i18nT('pages.chat.assistantMessage.speak'),
        })}
      </SettingsLink>
    )}
  </div>
}
