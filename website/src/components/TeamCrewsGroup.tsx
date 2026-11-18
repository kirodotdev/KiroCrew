/**
 * TeamCrewsGroup: the desktop app's remote Crews reachable through the Kiro
 * relay, as the "Remote Crews" group at the end of the Crew switcher's menu. Each
 * row reads "<type> · <status>", the type Shared (group icon) or Private (lock
 * icon), and its action reads Connect.
 *
 * Renders nothing unless the desktop bridge exists AND the gateway's crews route
 * answers (it 404s while `tunnel.relay_viewer_enabled` is off), so the default
 * switcher is unchanged. Connect hands only the endpoint ID to the desktop shell,
 * which opens that Crew in its own window. Only a click opens one. A Team Crew
 * is not a destination in this dashboard: its rows are plain menu items, never
 * pinned, never in the stable order and never in the unread count.
 *
 * Loaded lazily from the top bar, so the relay hook stays out of the app core.
 * Only the top-level bar gets the group: an SSH pane's own switcher never
 * receives the Team Crew list.
 *
 * shortcut: each Crew opens in its own window. The row's `onOpen` is the one
 * switch point if Team Crews later open in the main window.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { useRenderedLocationIsCurrent } from '../hooks/useRenderedLocationIsCurrent'
import { useSearchParams } from 'react-router-dom'
import { ExternalLink, Lock, LogIn, Users } from 'lucide-react'
import type { RelayCrewEntry, RelayViewerAccount } from '../api/client'
import {
  clearRelayCrewsUnseen, isRelayCrewOnline, openRelayCrew, openRelayCrewTab, recordRelayCrewsSeen, relayCrewStatusText,
  relayRefusalText, relayRetryable, useRelayViewerCrews, type RelayListRefusal,
} from '../hooks/useRelayViewerCrews'
import { useAppDispatch } from '../store'
import { relayPaneBridge } from '../lib/relayCrewPanes'
import InstanceTabBar from './InstanceTabBar'
import { DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator } from './ui/dropdown-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { i18nT } from '../i18n/t'
import { useOpenKiroSignIn } from '../pages/developer/useOpenKiroSignIn'
import { joinSentences } from '../i18n/format'
import { accountProviderLabel } from './kasProviderLabel'

/** One Team Crew. A failed Open keeps the menu open and shows why under the row.
 *  The agent hand-off is a sibling menu item, so the keyboard reaches it. */
function TeamCrewItem({ crew, refusal, onOpen }: {
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
  // Shared (a group) or Private (a lock); a type this build does not know gets no icon.
  const TypeIcon = kind === 'TEAM' ? Users : kind === 'PERSONAL' ? Lock : null
  return (
    <>
      <DropdownMenuItem
        data-testid="relay-crew-row"
        data-online={online ? 'true' : 'false'}
        // Not Radix's `disabled`, which drops the row from the focus order and its
        // tooltip with it: an offline row stays focusable and says why it won't open.
        offline={!online}
        aria-disabled={online ? undefined : true}
        aria-label={t('components.relayCrews.connect_crew', { name: crew.name })}
        title={online ? undefined : t('components.relayCrews.refusal_offline')}
        onSelect={(e: Event) => {
          // The menu closes only once the Crew's window opened; a refusal stays on screen.
          // Selecting the row is the only thing that connects it: nothing here
          // connects a listed Crew on its own.
          e.preventDefault()
          if (online) onOpen(crew.endpointId)
        }}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${online ? 'bg-[var(--ok)]' : 'bg-[var(--muted)]'}`}
          aria-hidden
        />
        <span className="flex flex-col min-w-0 flex-1">
          <span className="flex items-center gap-1.5 min-w-0">
            <span className="truncate">{crew.name}</span>
            {crew.unseen ? (
              <span data-testid="relay-crew-new" className="shrink-0 text-[11px] font-semibold text-accent">
                {t('components.relayCrews.new_crew')}
              </span>
            ) : null}
          </span>
          <span className="truncate text-[12px] text-muted">
            {TypeIcon && <TypeIcon className="lucide-inline mr-1" aria-hidden data-testid="relay-crew-type-icon" data-type={kind} />}
            {type} · {relayCrewStatusText(crew)}
          </span>
        </span>
        {online && (
          <span className="shrink-0 text-[12px] font-medium text-accent" aria-hidden data-testid="relay-crew-connect">
            {t('components.relayCrews.connect')}
          </span>
        )}
        {/* With panes on the Crew opens in this window, so no own-window mark. */}
        {!relayPaneBridge() && (
          <span className="shrink-0 text-muted" title={t('components.relayCrews.opens_own_window')}>
            <ExternalLink className="lucide-inline" aria-hidden />
          </span>
        )}
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
function RelayListRefusalNotice({ refusal, onRetry, retrying, close }: {
  refusal: RelayListRefusal
  onRetry: () => void
  /** A read is in flight: Try again stays off until it answers. */
  retrying: boolean
  /** Closes the switcher's menu, before the way to the Kiro account tab leaves the page. */
  close: () => void
}) {
  const { t } = useTranslation()
  const openKiroSignIn = useOpenKiroSignIn()
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
      {/* Signed out of Kiro: Sign in first, on Settings > Kiro account. It brings
          the local Crew forward and asks its leave guard (useOpenKiroSignIn).
          The agent hand-off stays too, since a backend that does not run on
          the Kiro sign-in can still take it. */}
      {refusal.code === 'SIGNED_OUT' && (
        <DropdownMenuItem
          data-testid="relay-crews-list-sign-in"
          aria-describedby="relay-crews-list-refusal"
          onSelect={() => {
            close()
            openKiroSignIn()
          }}
        >
          <LogIn size={14} aria-hidden="true" />
          {t('components.relayCrews.sign_in')}
        </DropdownMenuItem>
      )}
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

export function TeamCrewsGroup({ crews, account, refusal, retry, retrying, refreshOnOpen, close }: {
  crews: RelayCrewEntry[]
  /** The account the list was read for, shown as one muted line while the list is loaded. */
  account?: RelayViewerAccount | null
  refusal: RelayListRefusal | null
  retry: () => void
  retrying: boolean
  /** Reads the list again; the group mounts each time the menu opens. */
  refreshOnOpen: () => void
  /** Closes the switcher's menu. */
  close: () => void
}) {
  const { t } = useTranslation()
  // The menu shows what it last read, so a Crew that came online since would still read offline.
  useEffect(() => { refreshOnOpen() }, [refreshOnOpen])
  const queryClient = useQueryClient()
  // Left out on a refusal and for an account with no label. No e-mail: the gateway sends none.
  const provider = !refusal && account ? accountProviderLabel(account.accountType, account.startUrl) : ''
  const dispatch = useAppDispatch()
  const [refusals, setRefusals] = useState<Record<string, string>>({})
  // The group renders only while the menu is open, so showing it is seeing its
  // new Crews: the gateway is told now, and the marks stay until the menu closes
  // so the person can still tell which rows are new. Only IDs the gateway saved
  // lose their mark; a reply that lands after the menu closed clears them then.
  const sentUnseen = useRef<Set<string>>(new Set())
  const savedUnseen = useRef<string[]>([])
  const open = useRef(true)
  const [seenFailed, setSeenFailed] = useState(false)
  const unseenKey = crews.filter(c => c.unseen).map(c => c.endpointId).join(',')
  useEffect(() => {
    const fresh = unseenKey ? unseenKey.split(',').filter(id => !sentUnseen.current.has(id)) : []
    if (fresh.length === 0) return
    fresh.forEach(id => sentUnseen.current.add(id))
    void recordRelayCrewsSeen(fresh).then(({ saved, failed }) => {
      if (!open.current) {
        void clearRelayCrewsUnseen(queryClient, saved)
        return
      }
      savedUnseen.current.push(...saved)
      if (failed) setSeenFailed(true)
    })
  }, [unseenKey, queryClient])
  useEffect(() => {
    // The ref object, not its value: the cleanup must see every ID saved by then.
    const saved = savedUnseen
    open.current = true
    return () => {
      open.current = false
      void clearRelayCrewsUnseen(queryClient, saved.current)
    }
  }, [queryClient])
  const onOpen = useCallback((endpointId: string) => {
    setRefusals(prev => {
      const { [endpointId]: _dropped, ...rest } = prev
      return rest
    })
    const name = crews.find(c => c.endpointId === endpointId)?.name ?? ''
    void openRelayCrewTab(endpointId, name, dispatch).then(res => {
      if (res.ok) close()
      else setRefusals(prev => ({ ...prev, [endpointId]: relayRefusalText(res.error) }))
    })
  }, [close, crews, dispatch])
  return (
    <div role="group" aria-label={t('components.relayCrews.list_label')} data-testid="team-crews-group">
      <DropdownMenuSeparator />
      <DropdownMenuLabel>
        {t('components.relayCrews.group_team_crews')}
      </DropdownMenuLabel>
      {provider && (
        <div className="px-3 pb-1 break-words text-[12px] text-muted" data-testid="team-crews-account" title={t('app.signed_in_with', { provider })}>
          {t('app.signed_in_with', { provider })}
        </div>
      )}
      {seenFailed && (
        <>
          <div className="px-3 pb-1">
            <ErrorNotice id="relay-crews-seen-failed" variant="inline" message={t('components.relayCrews.seen_not_saved')} testId="relay-crews-seen-failed" />
          </div>
          <ErrorNoticeMenuItem Item={DropdownMenuItem} message={t('components.relayCrews.seen_not_saved')} describedBy="relay-crews-seen-failed" />
        </>
      )}
      {refusal
        ? <RelayListRefusalNotice refusal={refusal} onRetry={retry} retrying={retrying} close={close} />
        : crews.length === 0
          ? <div className="px-3 py-1.5 text-[12px] text-muted">{t('components.relayCrews.empty')}</div>
          : crews.map(c => (
            <TeamCrewItem key={c.endpointId} crew={c} refusal={refusals[c.endpointId]} onOpen={onOpen} />
          ))}
    </div>
  )
}

/** The top bar's Crew switcher with the Team Crews group, while the relay viewer is on
 *  and the group has a Crew, a notice (sign-in, setup or load failure) or the signed-in account to show. */
export default function TeamCrewsSwitcher({ variant }: { variant: 'strip' | 'inline' }) {
  const { hidden, crews, account, refusal, retry, retrying, refreshOnOpen } = useRelayViewerCrews()
  const [openRefusal, dismissOpenRefusal] = useOpenFromNote(hidden || refusal !== null, crews)
  // The same label the group's line shows; the hook sends an account only with a loaded list.
  const accountLabel = account ? accountProviderLabel(account.accountType, account.startUrl) : ''
  return (
    <InstanceTabBar
      variant={variant}
      extraNew={!hidden && refusal === null && crews.some(c => c.unseen)}
      // Nothing while hidden or loading, or for an empty list with no notice and no account
      // label, so the bar stays as it is without the relay. A loaded empty list with an
      // account label still mounts the group, so it says who is signed in.
      extraGroups={hidden || (refusal === null && crews.length === 0 && !accountLabel) ? undefined : close => (
        <TeamCrewsGroup crews={crews} account={account} refusal={refusal} retry={retry} retrying={retrying} refreshOnOpen={refreshOnOpen} close={close} />
      )}
      extraNotice={openRefusal ? (
        <ErrorNotice
          variant="inline"
          // The bar's list-failure notice's classes: only the message shrinks, so the
          // icon, the hand-off and the dismiss stay whole; the tooltip has it all.
          className="tb-crew-notice ml-2 min-w-0 max-w-[420px]"
          messageClassName="line-clamp-1 tb-crew-notice-msg"
          message={openRefusal}
          messageTooltip={openRefusal}
          onDismiss={dismissOpenRefusal}
          // As the bar's own list-failure notice: the bar holds no draft.
          askAgent
          testId="relay-crew-open-refusal"
        />
      ) : undefined}
    />
  )
}

/** The query a "You've been added" note's Open action sets on the chat page
 *  (`relay_viewer/crew_notices.py`). */
export const OPEN_RELAY_CREW_PARAM = 'relay_crew'

/** Open the Crew a note's Open action names, as its row would. The query is
 *  removed once it is decided, so a reload or Back does not open it again.
 *  `waiting` while the list is hidden or its read failed: the Open stays in the
 *  query until a read has succeeded. The gateway lists the Crews again on every
 *  open and refuses one that isn't granted, so a Crew missing from the cached
 *  list is opened all the same and a failed open says why in the top bar. */
function useOpenFromNote(waiting: boolean, crews: RelayCrewEntry[]): [string | null, () => void] {
  const [params, setParams] = useSearchParams()
  const renderedLocationIsCurrent = useRenderedLocationIsCurrent()
  const queryClient = useQueryClient()
  const [refusal, setRefusal] = useState<string | null>(null)
  const wanted = params.get(OPEN_RELAY_CREW_PARAM)
  // The query lingers for a render after it is removed, so one Open opens once.
  const handled = useRef<string | null>(null)
  useEffect(() => {
    if (!wanted) {
      handled.current = null
      return
    }
    // A render that lags a navigation still in flight must not write the old page back.
    if (waiting || handled.current === wanted || !renderedLocationIsCurrent()) return
    handled.current = wanted
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete(OPEN_RELAY_CREW_PARAM)
      return next
    }, { replace: true })
    // Named when the cached list has it: the switcher beside it still shows the local Crew.
    const name = crews.find(c => c.endpointId === wanted)?.name
    setRefusal(null)
    void openRelayCrew(wanted).then(res => {
      if (!res.ok) {
        setRefusal(name === undefined
          ? relayRefusalText(res.error)
          : joinSentences([
            i18nT('components.relayCrews.open_crew_failed', { name }),
            relayRefusalText(res.error),
          ]))
        return
      }
      void recordRelayCrewsSeen([wanted]).then(({ saved, failed }) => {
        void clearRelayCrewsUnseen(queryClient, saved)
        // The Crew opened; only its New mark wasn't saved, so it will show again.
        if (failed) setRefusal(i18nT('components.relayCrews.seen_not_saved'))
      })
    })
  }, [wanted, waiting, crews, setParams, queryClient, renderedLocationIsCurrent])
  return [refusal, () => setRefusal(null)]
}
