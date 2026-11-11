import { useAppSelector } from '../../store'
import { i18nT } from '../../i18n/t'
import { offlineReason } from '../../utils/offlineReason'

/* The composer's offline placeholder. A relay that ended the session or
   refused it gets the same words as the shell's offline notice, since "will
   not send, reconnecting" would contradict it; a plain drop keeps the
   composer's own wording. The cause is read only while the gateway itself is
   down: the composer's own `connected` can be a crew window's tunnel
   (CrewChatWindow), which the relay's cause says nothing about. */
export function useOfflinePlaceholder(): string {
  const offlineCause = useAppSelector(s => (s.dashboard.connected ? null : s.dashboard.offlineCause))
  return offlineCause ? offlineReason(offlineCause) : i18nT('components.chatInput.gateway_offline_message_will_not_send')
}
