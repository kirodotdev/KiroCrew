/** Above the composer after this tab lost a send race: someone else's message
 *  was accepted first, nothing of this tab's send was, and the draft is back in
 *  the composer untouched. The sender chooses: Send now (a new submission at
 *  the current state, which may queue behind the running turn or a limit), or
 *  Dismiss and keep editing. Nothing here sends on its own. */
import { clearSendRaceNotice, useSendRaceNotice } from '../../../api/sendRace'
import { principalName } from '../../../api/sharedCrewUi'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'

export function SendRaceNotice({ sessionKey, onSendNow }: { sessionKey: string | null | undefined; onSendNow: (sessionKey: string) => void }) {
  const notice = useSendRaceNotice(sessionKey)
  if (!sessionKey || !notice) return null
  const name = principalName(notice.winner.principal) || i18nT('sharedCrew.unattributed')
  return (
    <div
      role="status"
      className="w-full px-3 py-1 flex items-center gap-2 min-w-0 text-[12px] leading-5"
      data-testid="send-race-notice"
      data-winner-message={notice.winner.message_id}
    >
      <span className="text-text min-w-0 truncate" title={i18nT('sharedCrew.send_race_lost', { name })}>
        {i18nT('sharedCrew.send_race_lost', { name })}
      </span>
      <Btn
        className="ml-auto shrink-0 h-6 px-2 text-[12px]"
        onClick={() => { clearSendRaceNotice(sessionKey); onSendNow(sessionKey) }}
        data-testid="send-race-send-now"
      >
        {i18nT('sharedCrew.send_race_send_now')}
      </Btn>
      <Btn
        className="shrink-0 h-6 px-2 text-[12px]"
        onClick={() => clearSendRaceNotice(sessionKey)}
        data-testid="send-race-dismiss"
      >
        {i18nT('sharedCrew.send_race_dismiss')}
      </Btn>
    </div>
  )
}
