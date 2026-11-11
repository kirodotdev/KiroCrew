/**
 * TeamCrewsGroup: the desktop app's remote Crews reachable through the Kiro
 * relay, as the Team Crews group at the end of the Crew switcher's menu.
 *
 * Renders nothing unless the desktop bridge exists AND the gateway's crews route
 * answers (it 404s while `tunnel.relay_viewer_enabled` is off), so the default
 * switcher is unchanged. Open hands only the endpoint ID to the desktop shell,
 * which opens that Crew in its own window. A Team Crew is not a destination in
 * this dashboard: its rows are plain menu items, never pinned, never in the
 * stable order and never in the unread count.
 *
 * Loaded lazily from the top bar, so the relay hook stays out of the app core.
 * Only the top-level bar gets the group: an SSH pane's own switcher never
 * receives the Team Crew list.
 *
 * shortcut: each Crew opens in its own window. The row's `onOpen` is the one
 * switch point if Team Crews later open in the main window.
 */
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ExternalLink } from 'lucide-react'
import type { RelayCrewEntry } from '../api/client'
import {
  isRelayCrewOnline, openRelayCrew, relayCrewStatusText, relayRefusalText, relayRetryable, useRelayViewerCrews,
  type RelayListRefusal,
} from '../hooks/useRelayViewerCrews'
import InstanceTabBar from './InstanceTabBar'
import { DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator } from './ui/dropdown-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'

/** One Team Crew. A failed Open keeps the menu open and shows why under the row.
 *  The agent hand-off is a sibling menu item, so the keyboard reaches it. */
function TeamCrewItem({ crew, refusal, onOpen }: {
  crew: RelayCrewEntry
  refusal: string | undefined
  onOpen: (endpointId: string) => void
}) {
  const { t } = useTranslation()
  const online = isRelayCrewOnline(crew)
  const type = crew.crewType.toUpperCase() === 'TEAM'
    ? t('components.relayCrews.type_team')
    : crew.crewType.toUpperCase() === 'PERSONAL' ? t('components.relayCrews.type_personal') : crew.crewType
  return (
    <>
      <DropdownMenuItem
        data-testid="relay-crew-row"
        data-online={online ? 'true' : 'false'}
        // Not Radix's `disabled`, which drops the row from the focus order and its
        // tooltip with it: an offline row stays focusable and says why it won't open.
        offline={!online}
        aria-disabled={online ? undefined : true}
        aria-label={t('components.relayCrews.open_crew', { name: crew.name })}
        title={online ? undefined : t('components.relayCrews.refusal_offline')}
        onSelect={(e: Event) => {
          // The menu closes only once the window opened; a refusal stays on screen.
          e.preventDefault()
          if (online) onOpen(crew.endpointId)
        }}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${online ? 'bg-[var(--ok)]' : 'bg-[var(--muted)]'}`}
          aria-hidden
        />
        <span className="flex flex-col min-w-0 flex-1">
          <span className="truncate">{crew.name}</span>
          <span className="truncate text-[12px] text-muted">{type} · {relayCrewStatusText(crew)}</span>
        </span>
        <span className="shrink-0 text-muted" title={t('components.relayCrews.opens_own_window')}>
          <ExternalLink className="lucide-inline" aria-hidden />
        </span>
      </DropdownMenuItem>
      {refusal && (
        <>
          <div className="px-3 pb-1">
            <ErrorNotice id={`relay-crew-refusal-${crew.endpointId}`} variant="inline" message={refusal} testId="relay-crew-refusal" />
          </div>
          <ErrorNoticeMenuItem Item={DropdownMenuItem} message={refusal} describedBy={`relay-crew-refusal-${crew.endpointId}`} />
        </>
      )}
    </>
  )
}

/** The list's refusal: its plain text, Try again where a resend can help (off
 *  until a throttle's Retry-After passes), and CPS's request ID when CPS sent one,
 *  so a server failure can be reported. Try again is a menu item so the keyboard
 *  reaches it, and the request ID is shown open, not under a collapsed Details. */
function RelayListRefusalNotice({ refusal, onRetry, retrying }: {
  refusal: RelayListRefusal
  onRetry: () => void
  /** A read is in flight: Try again stays off until it answers. */
  retrying: boolean
}) {
  const { t } = useTranslation()
  const { retryAt } = refusal
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (retryAt === null) return
    const ms = retryAt - Date.now()
    if (ms <= 0) return
    // At least retryAt, so a timer that fires a little early still turns the button on.
    const timer = setTimeout(() => setNow(Math.max(Date.now(), retryAt)), ms)
    return () => clearTimeout(timer)
  }, [retryAt])
  const waiting = retryAt !== null && retryAt > now
  return (
    <>
      <div className="px-3 pb-1">
        <ErrorNotice id="relay-crews-list-refusal" variant="inline" message={refusal.text} testId="relay-crews-list-refusal" />
      </div>
      <ErrorNoticeMenuItem Item={DropdownMenuItem} message={refusal.text} describedBy="relay-crews-list-refusal" />
      {refusal.requestId && (
        <div className="px-3 pb-1 text-[11px] text-muted font-mono select-all" data-testid="relay-crews-list-details">
          {t('components.relayCrews.request_id', { id: refusal.requestId })}
        </div>
      )}
      {relayRetryable(refusal.code) && (
        <DropdownMenuItem
          disabled={waiting || retrying}
          data-testid="relay-crews-list-retry"
          onSelect={(e: Event) => {
            e.preventDefault()
            onRetry()
          }}
        >
          {t('components.relayCrews.try_again')}
        </DropdownMenuItem>
      )}
    </>
  )
}

export function TeamCrewsGroup({ crews, refusal, retry, retrying, close }: {
  crews: RelayCrewEntry[]
  refusal: RelayListRefusal | null
  retry: () => void
  retrying: boolean
  /** Closes the switcher's menu. */
  close: () => void
}) {
  const { t } = useTranslation()
  const [refusals, setRefusals] = useState<Record<string, string>>({})
  const onOpen = useCallback((endpointId: string) => {
    setRefusals(prev => {
      const { [endpointId]: _dropped, ...rest } = prev
      return rest
    })
    void openRelayCrew(endpointId).then(res => {
      if (res.ok) close()
      else setRefusals(prev => ({ ...prev, [endpointId]: relayRefusalText(res.error) }))
    })
  }, [close])
  return (
    <div role="group" aria-label={t('components.relayCrews.list_label')} data-testid="team-crews-group">
      <DropdownMenuSeparator />
      <DropdownMenuLabel>
        {t('components.relayCrews.group_team_crews')}
      </DropdownMenuLabel>
      {refusal
        ? <RelayListRefusalNotice refusal={refusal} onRetry={retry} retrying={retrying} />
        : crews.length === 0
          ? <div className="px-3 py-1.5 text-[12px] text-muted">{t('components.relayCrews.empty')}</div>
          : crews.map(c => (
            <TeamCrewItem key={c.endpointId} crew={c} refusal={refusals[c.endpointId]} onOpen={onOpen} />
          ))}
    </div>
  )
}

/** The top bar's Crew switcher with the Team Crews group, while the relay viewer is on
 *  and the group has a Crew or a notice (sign-in, setup or load failure) to show. */
export default function TeamCrewsSwitcher({ variant }: { variant: 'strip' | 'inline' }) {
  const { hidden, crews, refusal, retry, retrying } = useRelayViewerCrews()
  return (
    <InstanceTabBar
      variant={variant}
      // An empty list passes nothing, so the bar stays as it is without the relay.
      extraGroups={hidden || (refusal === null && crews.length === 0) ? undefined : close => (
        <TeamCrewsGroup crews={crews} refusal={refusal} retry={retry} retrying={retrying} close={close} />
      )}
    />
  )
}
