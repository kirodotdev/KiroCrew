/**
 * SharedCrewsCards: Settings > Remote Crew's "Shared with me" section, the Crews
 * the signed-in person can open through the Kiro relay, and the "Set up a new
 * Shared Crew" card under it that points at the AWS Management Console.
 *
 * The list is the one behind the Crew switcher's Team Crews group
 * (`useRelayViewerCrews`, one cached read of `GET /api/relay-viewer/crews`), so
 * the two surfaces cannot disagree. Both cards render nothing while that hook
 * is hidden: no list is loaded, or the read was refused (signed out, remote
 * access off). Rows
 * open a Crew the same way the switcher does (`openRelayCrewTab`, the endpoint ID
 * only): a tab in the desktop app's main window, a new tab in a browser.
 *
 * shortcut: no role chip and no member count. The crews answer carries neither
 * (`_entry` in `src/kiro_crew/dashboard/handlers/relay_viewer.py`); add them
 * here once CPS and the route return them.
 */
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { useInRouterContext } from 'react-router-dom'
import { ArrowRight, ExternalLink, Users } from 'lucide-react'
import type { RelayCrewEntry } from '../api/client'
import {
  clearRelayCrewsUnseen, isRelayCrewOnline, openRelayCrewTab, recordRelayCrewsSeen, relayCrewStatusText, relayRefusalText, useRelayViewerCrews,
} from '../hooks/useRelayViewerCrews'
import { useAppDispatch } from '../store'
import { relayPaneBridge } from '../lib/relayCrewPanes'
import { Card, PanelSectionHeader } from './ui'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'
import { KIRO_SIGN_IN_TARGET } from '../pages/developer/kiroSignInLink'
import { SettingsLink } from './SettingsLink'

/** The AWS Management Console home. A reviewer call: no Kiro or Amazon Q
 *  Developer console link exists in the codebase to reuse. */
const AWS_CONSOLE_URL = 'https://console.aws.amazon.com/'

/** A refusal that says the person is signed out: the way to sign in. */
function signedOut(code: string | undefined): boolean {
  return (code || '').toUpperCase() === 'SIGNED_OUT'
}

/** The way from a signed-out refusal to the Kiro account tab, where the sign-in is. */
function KiroAccountLink() {
  const inRouter = useInRouterContext()
  if (!inRouter) return null
  return (
    <SettingsLink
      {...KIRO_SIGN_IN_TARGET}
      data-testid="shared-crews-kiro-account-link"
      className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline focus-ring-accent"
    >
      {i18nT('settings.tabs.kiroAccount.go_to')}
      <ArrowRight className="lucide-inline" aria-hidden="true" />
    </SettingsLink>
  )
}

/** A row's notice: the text it shows, and the refusal code behind it when an
 *  open was refused (none for a seen-state save that failed). */
interface RowRefusal { text: string; code?: string }

function SharedCrewRow({ crew, refusal, onOpen }: {
  crew: RelayCrewEntry
  refusal: RowRefusal | undefined
  onOpen: (endpointId: string) => void
}) {
  const { t } = useTranslation()
  const online = isRelayCrewOnline(crew)
  const inWindow = relayPaneBridge() !== null
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
        // With the pane calls the Crew opens in this window, so no own-window title or mark.
        title={online ? (inWindow ? undefined : t('components.relayCrews.opens_own_window')) : t('components.relayCrews.refusal_offline')}
        onClick={() => { if (online) onOpen(crew.endpointId) }}
        className={`w-full flex items-center gap-3 px-2 py-2 rounded-md text-left ${online ? 'hover:bg-bg-hover' : 'opacity-70 cursor-default'}`}
      >
        <span className="w-8 h-8 shrink-0 grid place-items-center rounded-md bg-accent-subtle text-accent" aria-hidden>
          <Users size={16} />
        </span>
        <span className="flex flex-col min-w-0 flex-1">
          <span className="truncate text-sm font-medium text-text-strong">{crew.name}</span>
          <span className="flex items-center gap-1.5 min-w-0 text-[12px] text-muted">
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${online ? 'bg-[var(--ok)]' : 'bg-[var(--muted)]'}`} aria-hidden />
            <span className="truncate">{relayCrewStatusText(crew)}</span>
            <span aria-hidden>·</span>
            <span className="shrink-0 text-[11px] px-1.5 rounded border border-border">{type}</span>
          </span>
        </span>
        {/* The whole row is the Open button; this is its visible label. */}
        {online && (
          <span className="shrink-0 inline-flex items-center gap-1 rounded-md bg-accent text-accent-fg text-[12px] font-medium px-2.5 py-1" aria-hidden>
            {t('components.sharedCrews.open')} {!inWindow && <ExternalLink size={12} />}
          </span>
        )}
      </button>
      {refusal && (
        // askAgent on: this tab's only unsaved inputs (the add-crew and edit-crew
        // forms) live in the store, so the hand-off's navigation loses nothing.
        <ErrorNotice variant="inline" message={refusal.text} className="px-2 pb-1" testId="shared-crew-refusal" askAgent />
      )}
      {signedOut(refusal?.code) && <div className="px-2 pb-1"><KiroAccountLink /></div>}
    </li>
  )
}

export default function SharedCrewsCards() {
  const { t } = useTranslation()
  const { hidden, crews, failed } = useRelayViewerCrews()
  const [refusals, setRefusals] = useState<Record<string, RowRefusal>>({})
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
        setRefusals(prev => ({ ...prev, [endpointId]: { text: relayRefusalText(res.error), code: res.error } }))
        return
      }
      // Opened, so it is seen, as an Open from the switcher records it.
      void recordRelayCrewsSeen([endpointId]).then(({ saved, failed }) => {
        void clearRelayCrewsUnseen(queryClient, saved)
        if (failed) setRefusals(prev => ({ ...prev, [endpointId]: { text: i18nT('components.relayCrews.seen_not_saved') } }))
      })
    })
  }, [crews, dispatch, queryClient])
  if (hidden) return null
  return (
    <>
      <section aria-label={t('components.sharedCrews.section_title')} className="space-y-2">
        <PanelSectionHeader label={t('components.sharedCrews.section_title')} count={crews.length} />
        <Card data-testid="shared-crews-card">
          <p className="text-[12px] text-muted mb-2">{t('components.sharedCrews.managed_by_admin')}</p>
          {/* askAgent on: this tab's drafts live in the store. The rows below are the last list loaded. */}
          {failed && <ErrorNotice variant="inline" impact={crews.length > 0 ? 'partial' : 'blocking'} message={t(crews.length > 0 ? 'components.relayCrews.refresh_failed' : 'components.relayCrews.refusal_list_generic')} className="pb-2" testId="shared-crews-refresh-failed" askAgent />}
          {crews.length === 0 ? (
            !failed && <div className="text-[13px] text-muted py-1" data-testid="shared-crews-empty">{t('components.sharedCrews.empty')}</div>
          ) : (
            <ul aria-label={t('components.sharedCrews.section_title')} className="divide-y divide-border">
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
      </section>
    </>
  )
}
