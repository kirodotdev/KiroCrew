/**
 * SharedCrewsCards: Settings > Remote Crew's "Shared Crews" card, the Crews the
 * signed-in person can open through the Kiro relay, and the "Set up a new Shared
 * Crew" card under it that points at the AWS Management Console.
 *
 * The list is the one behind the Crew switcher's Team Crews group
 * (`useRelayViewerCrews`, one cached read of `GET /api/relay-viewer/crews`), so
 * the two surfaces cannot disagree. Both cards render nothing while that hook
 * is hidden: no desktop bridge, or the route 404s because
 * `tunnel.relay_viewer_enabled` is off. Rows open a Crew the same way the
 * switcher does (`openRelayCrew`, the endpoint ID only).
 *
 * shortcut: no role chip and no member count. The crews answer carries neither
 * (`_entry` in `src/kiro_crew/dashboard/handlers/relay_viewer.py`); add them
 * here once CPS and the route return them.
 */
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Users } from 'lucide-react'
import type { RelayCrewEntry } from '../api/client'
import {
  clearRelayCrewsUnseen, isRelayCrewOnline, openRelayCrewTab, recordRelayCrewsSeen, relayCrewStatusText, relayRefusalText, relayRetryable, useRelayViewerCrews,
} from '../hooks/useRelayViewerCrews'
import { useAppDispatch } from '../store'
import { Btn, Card } from './ui'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

/** The AWS Management Console home. A reviewer call: no Kiro or Amazon Q
 *  Developer console link exists in the codebase to reuse. */
const AWS_CONSOLE_URL = 'https://console.aws.amazon.com/'

function SharedCrewRow({ crew, refusal, onOpen }: {
  crew: RelayCrewEntry
  refusal: string | undefined
  onOpen: (endpointId: string) => void
}) {
  const { t } = useTranslation()
  const online = isRelayCrewOnline(crew)
  const kind = crew.crewType.toUpperCase()
  const type = kind === 'TEAM'
    ? t('components.relayCrews.type_team')
    : kind === 'PERSONAL' ? t('components.relayCrews.type_personal') : crew.crewType
  return (
    <li>
      <button
        type="button"
        data-testid="shared-crew-row"
        data-online={online ? 'true' : 'false'}
        // Focusable while offline, so its tooltip still says why it won't open.
        aria-disabled={online ? undefined : true}
        aria-label={t('components.relayCrews.open_crew', { name: crew.name })}
        title={online ? t('components.relayCrews.opens_own_window') : t('components.relayCrews.refusal_offline')}
        onClick={() => { if (online) onOpen(crew.endpointId) }}
        className={`w-full flex items-center gap-2.5 px-2 py-2 rounded-md text-left ${online ? 'hover:bg-bg-hover' : 'opacity-70 cursor-default'}`}
      >
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${online ? 'bg-[var(--ok)]' : 'bg-[var(--muted)]'}`} aria-hidden />
        <span className="flex flex-col min-w-0 flex-1">
          <span className="truncate text-[13px] text-text">{crew.name}</span>
          <span className="truncate text-[12px] text-muted">{relayCrewStatusText(crew)}</span>
        </span>
        <span className="shrink-0 text-[11px] px-1.5 rounded-full bg-bg-hover text-muted">{type}</span>
        <ExternalLink className="lucide-inline shrink-0 text-muted" aria-hidden />
      </button>
      {refusal && (
        // askAgent on: this tab's only unsaved inputs (the add-crew and edit-crew
        // forms) live in the store, so the hand-off's navigation loses nothing.
        <ErrorNotice variant="inline" message={refusal} className="px-2 pb-1" testId="shared-crew-refusal" askAgent />
      )}
    </li>
  )
}

/** The list refusal in this card's own words: the codes whose shared text names
 *  Team Crews get Shared Crews wording, every other code keeps its shared text. */
function sharedListRefusalText(code: string | undefined): string {
  switch ((code || '').toUpperCase()) {
    case 'SIGNED_OUT': return i18nT('components.sharedCrews.refusal_signed_out')
    case 'NOT_SET_UP': return i18nT('components.sharedCrews.refusal_not_configured')
    default: return relayRefusalText(code, i18nT('components.sharedCrews.refusal_list_generic'))
  }
}

export default function SharedCrewsCards() {
  const { t } = useTranslation()
  const { hidden, crews, refusal, retry, retrying } = useRelayViewerCrews()
  const [refusals, setRefusals] = useState<Record<string, string>>({})
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const onOpen = useCallback((endpointId: string) => {
    setRefusals(prev => {
      const { [endpointId]: _dropped, ...rest } = prev
      return rest
    })
    const name = crews.find(c => c.endpointId === endpointId)?.name ?? ''
    void openRelayCrewTab(endpointId, name, dispatch).then(res => {
      if (!res.ok) {
        setRefusals(prev => ({ ...prev, [endpointId]: relayRefusalText(res.error) }))
        return
      }
      // Opened, so it is seen, as an Open from the switcher records it.
      void recordRelayCrewsSeen([endpointId]).then(({ saved, failed }) => {
        void clearRelayCrewsUnseen(queryClient, saved)
        if (failed) setRefusals(prev => ({ ...prev, [endpointId]: i18nT('components.relayCrews.seen_not_saved') }))
      })
    })
  }, [crews, dispatch, queryClient])
  if (hidden) return null
  return (
    <>
      <Card data-testid="shared-crews-card">
        <div className="flex items-center gap-2 text-text font-medium mb-1">
          <Users className="lucide-inline" /> {t('components.sharedCrews.title')}
        </div>
        <p className="text-[12px] text-muted mb-2">{t('components.sharedCrews.managed_by_admin')}</p>
        {refusal ? (
          // shortcut: Try again is not held back for a throttle's Retry-After as the
          // switcher's notice is; an early press just reads the same refusal again.
          <div className="py-1 flex flex-wrap items-center gap-3">
            {/* askAgent on: a list read failed, and this tab's drafts live in the store. */}
            <ErrorNotice variant="inline" impact="partial" message={sharedListRefusalText(refusal.code)} testId="shared-crews-refusal" askAgent />
            {relayRetryable(refusal.code) && (
              <Btn onClick={retry} disabled={retrying} data-testid="shared-crews-retry">
                {t('components.relayCrews.try_again')}
              </Btn>
            )}
          </div>
        ) : crews.length === 0 ? (
          <div className="text-[13px] text-muted py-1" data-testid="shared-crews-empty">{t('components.sharedCrews.empty')}</div>
        ) : (
          <ul aria-label={t('components.sharedCrews.title')}>
            {crews.map(c => (
              <SharedCrewRow key={c.endpointId} crew={c} refusal={refusals[c.endpointId]} onOpen={onOpen} />
            ))}
          </ul>
        )}
      </Card>
      <Card data-testid="shared-crews-setup-card">
        <div className="text-text font-medium mb-1">{t('components.sharedCrews.setup_title')}</div>
        <p className="text-[12px] text-muted mb-2">{t('components.sharedCrews.setup_desc')}</p>
        <a className="text-accent text-[13px] font-medium hover:underline inline-flex items-center gap-1" href={AWS_CONSOLE_URL} target="_blank" rel="noreferrer">
          {t('components.sharedCrews.open_console')} <ExternalLink className="lucide-inline" aria-hidden />
        </a>
      </Card>
    </>
  )
}
