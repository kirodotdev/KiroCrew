/**
 * TeamCrewsGroup: the remote Crews reachable through the Kiro relay, at the end
 * of the Crew switcher's one "Remote Crews" list: under the crews this dashboard
 * reaches itself (SSH, SSM), or under a heading of its own when there are none. Each
 * row reads "<type> · <status>", the type Shared (group icon) or Private (lock
 * icon), and its end says which of three states it is in: Not connected, Connected
 * (open in this window, not in front) or a check (in front). Which account the list was read for is said on Settings >
 * Remote Crew, not here: the group lists every remote Crew, not only that
 * account's.
 *
 * Renders nothing until a list is loaded: a signed-out, refused or failed read
 * hides it quietly (`useRelayViewerCrews`), so the switcher is unchanged then.
 * Connect hands only the endpoint ID on: in the desktop app to the shell, which
 * shows that Crew as a tab in the main window (a shell without the pane calls
 * opens a window of its own); in a browser to the gateway, which answers the URL
 * of a new tab it relays the Crew on. Only a click opens one. A Team Crew
 * is not a destination in this dashboard: its rows are plain menu items, never
 * pinned, never in the stable order and never in the unread count.
 *
 * Loaded lazily from the top bar, so the relay hook stays out of the app core.
 * An SSH Crew's own switcher, which is the one on screen while that Crew is in
 * front, gets the rows the window relays to it instead (`EmbeddedTeamCrewsGroup`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { useRenderedLocationIsCurrent } from '../hooks/useRenderedLocationIsCurrent'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Check, ExternalLink, Lock, Users } from 'lucide-react'
import type { RelayCrewEntry } from '../api/client'
import {
  clearRelayCrewsUnseen, isRelayCrewOnline, isSignedOutRefusal, openRelayCrewTab, recordRelayCrewsSeen, relayCrewStatusText,
  relayRefusalText, useRelayViewerCrews,
} from '../hooks/useRelayViewerCrews'
import { useAppDispatch, useAppSelector } from '../store'
import { relayOpenRequests, relayPaneBridge, useRelayTabs } from '../lib/relayCrewPanes'
import InstanceTabBar from './InstanceTabBar'
import { DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator } from './ui/dropdown-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { i18nT } from '../i18n/t'
import { joinSentences } from '../i18n/format'
import { accountProviderLabel } from './kasProviderLabel'
import { useGuardedLeave } from './NavigationLeaveGuard'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { postToHost } from '../lib/hostRelay'
import { OPEN_RELAY_CREW_PARAM, PANE_RELAY_PREFIX, type PaneRelayCrew } from '../lib/paneRelayCrews'

/** A Remote Crews row's state, from whether its tab is open, in front, and the
 *  relay's real connection. `ROW` below is the one place each state's look and
 *  action are written, as in the PR's table. */
export type RelayRowState = 'current' | 'connected' | 'openOffline' | 'connect' | 'unavailable'

export function relayRowState(open: boolean, current: boolean, online: boolean): RelayRowState {
  if (open) return current ? 'current' : online ? 'connected' : 'openOffline'
  return online ? 'connect' : 'unavailable'
}

/** Per state: dimmed, the mark at the row's end, and what choosing it does. */
export const ROW: Record<RelayRowState, { dimmed: boolean; end: 'check' | 'connected' | 'notConnected' | null; action: 'show' | 'open' | 'none' }> = {
  current: { dimmed: false, end: 'check', action: 'show' },
  connected: { dimmed: false, end: 'connected', action: 'show' },
  openOffline: { dimmed: false, end: null, action: 'show' },
  connect: { dimmed: false, end: 'notConnected', action: 'open' },
  unavailable: { dimmed: true, end: null, action: 'none' },
}

/** The way from a signed-out refusal to the Kiro account tab. A menu item, not a
 *  link inside one: the item's own click would replace the link's guarded one,
 *  and the leave guard must still ask before unsaved work is left. */
function KiroSignInMenuItem() {
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  return (
    <DropdownMenuItem
      data-testid="relay-crew-sign-in"
      onSelect={() => guardedLeave(() => navigate(KIRO_SIGN_IN_PATH), KIRO_SIGN_IN_PATH)}
    >
      {i18nT('settings.tabs.kiroAccount.go_to')}
    </DropdownMenuItem>
  )
}

/** One Team Crew. A failed Open keeps the menu open and shows why under the row.
 *  The agent hand-off is a sibling menu item, so the keyboard reaches it. The row
 *  has no close of its own: an open pane past the warm cap closes by itself. */
function TeamCrewItem({ crew, refusal, onOpen, onShow, current = false, ownWindowMark = !relayPaneBridge() }: {
  crew: RelayCrewEntry
  refusal: { text: string; code?: string } | undefined
  onOpen: (endpointId: string) => void
  /** The Crew opens in a window of its own, so the row says so. */
  ownWindowMark?: boolean
  /** Brings this Crew's open tab forward; set while it is open. */
  onShow?: () => void
  /** This Crew's tab is the one in front. */
  current?: boolean
}) {
  const { t } = useTranslation()
  const online = isRelayCrewOnline(crew)
  const kind = crew.crewType.toUpperCase()
  const type = kind === 'TEAM'
    ? t('components.relayCrews.type_team')
    : kind === 'PERSONAL' ? t('components.relayCrews.type_personal') : crew.crewType
  // Shared (a group) or Private (a lock); a type this build does not know gets no icon.
  const TypeIcon = kind === 'TEAM' ? Users : kind === 'PERSONAL' ? Lock : null
  const state = relayRowState(!!onShow, current, online)
  const row = ROW[state]
  const stateId = `relay-crew-state-${crew.endpointId}`
  const statusId = `relay-crew-status-${crew.endpointId}`
  return (
    <>
      <div className="flex items-center">
      <DropdownMenuItem
        className="flex-1 min-w-0"
        data-testid="relay-crew-row"
        data-online={online ? 'true' : 'false'}
        // Not Radix's `disabled`, which drops the row from the focus order and its
        // tooltip with it: an unavailable row stays focusable and says why it won't open.
        offline={row.dimmed}
        aria-disabled={row.action === 'none' ? true : undefined}
        // The Crew in front is marked as the local rows are: a checked radio item.
        {...(state === 'current' ? { role: 'menuitemradio', 'aria-checked': true } : {})}
        aria-label={row.action === 'open' ? t('components.relayCrews.connect_crew', { name: crew.name }) : crew.name}
        aria-describedby={state === 'connected' ? stateId : state === 'openOffline' ? statusId : undefined}
        title={state === 'unavailable' ? t('components.relayCrews.refusal_offline') : undefined}
        onSelect={(e: Event) => {
          // The menu closes only once the Crew's window opened; a refusal stays on screen.
          // Selecting the row is the only thing that connects it: nothing here
          // connects a listed Crew on its own.
          e.preventDefault()
          if (row.action === 'open') onOpen(crew.endpointId)
          else if (row.action === 'show') onShow?.()
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
          <span id={statusId} className="truncate text-[12px] text-muted">
            {TypeIcon && <TypeIcon className="lucide-inline mr-1" aria-hidden data-testid="relay-crew-type-icon" data-type={kind} />}
            {type} · {relayCrewStatusText(crew)}
          </span>
        </span>
        {/* The end mark is the state's own (`ROW`). */}
        {row.end === 'check' ? (
          <Check className="lucide-inline shrink-0 text-accent" aria-hidden data-testid="relay-crew-current" />
        ) : row.end === 'connected' ? (
          <span id={stateId} className="shrink-0 text-[12px] text-muted" data-testid="relay-crew-connected">
            {t('components.relayCrews.connected')}
          </span>
        ) : row.end === 'notConnected' && (
          // A state, as Connected is, not a second action: choosing the row opens it.
          <span className="shrink-0 text-[12px] text-muted" aria-hidden data-testid="relay-crew-not-connected">
            {t('components.relayCrews.not_connected')}
          </span>
        )}
        {/* With panes on the Crew opens in this window, so no own-window mark. */}
        {ownWindowMark && (
          <span className="shrink-0 text-muted" title={t('components.relayCrews.opens_own_window')}>
            <ExternalLink className="lucide-inline" aria-hidden />
          </span>
        )}
      </DropdownMenuItem>
      </div>
      {refusal && (isSignedOutRefusal(refusal.code) ? (
        // Signed out is a state with a way forward, not a failure: no alert, no
        // hand-off, and the way forward is a menu item to the Kiro account tab.
        <>
          <p role="status" className="m-0 px-3 pb-1 text-[12px] text-muted" data-testid="relay-crew-refusal">{refusal.text}</p>
          <KiroSignInMenuItem />
        </>
      ) : (
        <>
          <div className="px-3 pb-1">
            <ErrorNotice id={`relay-crew-refusal-${crew.endpointId}`} variant="inline" message={refusal.text} testId="relay-crew-refusal" />
          </div>
          <ErrorNoticeMenuItem Item={DropdownMenuItem} message={refusal.text} describedBy={`relay-crew-refusal-${crew.endpointId}`} />
        </>
      ))}
    </>
  )
}

export function TeamCrewsGroup({ crews, failed = false, refreshOnOpen, close, headed = false }: {
  crews: RelayCrewEntry[]
  /** The menu already drew the "Remote Crews" heading over its own crews, so these
   *  rows continue that one list instead of opening a second group. */
  headed?: boolean
  /** The latest read failed; the rows are the last list loaded. */
  failed?: boolean
  /** Reads the list again; the group mounts each time the menu opens. */
  refreshOnOpen: () => void
  /** Closes the switcher's menu. */
  close: () => void
}) {
  const { t } = useTranslation()
  // The menu shows what it last read, so a Crew that came online since would still read offline.
  useEffect(() => { refreshOnOpen() }, [refreshOnOpen])
  const queryClient = useQueryClient()
  // With rows kept, say they are the last list rather than that nothing loaded.
  const failedText = t(crews.length > 0 ? 'components.relayCrews.refresh_failed' : 'components.relayCrews.refusal_list_generic')
  const dispatch = useAppDispatch()
  const [refusals, setRefusals] = useState<Record<string, { text: string; code?: string }>>({})
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
  const relayTabs = useRelayTabs()
  const relayActive = useAppSelector(s => s.instances?.relayActive ?? null)
  const onOpen = useCallback((endpointId: string) => {
    setRefusals(prev => {
      const { [endpointId]: _dropped, ...rest } = prev
      return rest
    })
    const name = crews.find(c => c.endpointId === endpointId)?.name ?? ''
    void openRelayCrewTab(endpointId, name, dispatch).then(res => {
      if (res.ok) close()
      else setRefusals(prev => ({ ...prev, [endpointId]: { text: relayRefusalText(res.error), code: res.error } }))
    })
  }, [close, crews, dispatch])
  return (
    <div role="group" aria-label={t('components.relayCrews.list_label')} data-testid="team-crews-group">
      {!headed && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>
            {t('components.relayCrews.group_team_crews')}
          </DropdownMenuLabel>
        </>
      )}
      {failed && (
        <>
          <div className="px-3 pb-1">
            <ErrorNotice id="relay-crews-refresh-failed" variant="inline" message={failedText} testId="relay-crews-refresh-failed" />
          </div>
          <ErrorNoticeMenuItem Item={DropdownMenuItem} message={failedText} describedBy="relay-crews-refresh-failed" />
        </>
      )}
      {seenFailed && (
        <>
          <div className="px-3 pb-1">
            <ErrorNotice id="relay-crews-seen-failed" variant="inline" message={t('components.relayCrews.seen_not_saved')} testId="relay-crews-seen-failed" />
          </div>
          <ErrorNoticeMenuItem Item={DropdownMenuItem} message={t('components.relayCrews.seen_not_saved')} describedBy="relay-crews-seen-failed" />
        </>
      )}
      {crews.length === 0 && !failed && <div className="px-3 py-1.5 text-[12px] text-muted">{t('components.relayCrews.empty')}</div>}
      {crews.length > 0 && crews.map(c => (
            <TeamCrewItem
              key={c.endpointId} crew={c} refusal={refusals[c.endpointId]} onOpen={onOpen}
              onShow={relayTabs.tabs.some(tab => tab.id === c.endpointId) ? () => { relayTabs.select(c.endpointId); close() } : undefined}
              current={relayActive === c.endpointId}
            />
          ))}
    </div>
  )
}

/** The Remote Crews group in an SSH Crew's own switcher, from the rows the window
 *  relays (lib/paneRelayCrews.ts). A pick goes up to that window, which shows or
 *  opens the Crew from its own top bar, which also says why when it is refused. */
export function EmbeddedTeamCrewsGroup({ crews, close, headed = false }: {
  crews: PaneRelayCrew[]
  close: () => void
  /** As `TeamCrewsGroup`'s: the pane's own crews already opened the one list. */
  headed?: boolean
}) {
  const { t } = useTranslation()
  const pick = (endpointId: string) => {
    postToHost({ type: 'mc-switch-instance', v: 1, id: `${PANE_RELAY_PREFIX}${endpointId}` })
    close()
  }
  return (
    <div role="group" aria-label={t('components.relayCrews.list_label')} data-testid="team-crews-group">
      {!headed && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>{t('components.relayCrews.group_team_crews')}</DropdownMenuLabel>
        </>
      )}
      {crews.map(c => (
        <TeamCrewItem
          key={c.endpointId} crew={{ ...c, crewUrl: '' }} refusal={undefined} onOpen={pick}
          onShow={c.open ? () => pick(c.endpointId) : undefined} current={c.current} ownWindowMark={false}
        />
      ))}
    </div>
  )
}

/** The top bar's Crew switcher with the Team Crews group, while a list is loaded and
 *  the group has a Crew to show or a signed-in account to say none is shared with. */
export default function TeamCrewsSwitcher({ variant }: { variant: 'strip' | 'inline' }) {
  const { hidden, crews, account, failed, refreshOnOpen } = useRelayViewerCrews()
  const [openRefusal, dismissOpenRefusal] = useOpenFromNote(crews)
  // The hook sends an account only with a loaded list.
  const accountLabel = account ? accountProviderLabel(account.accountType, account.startUrl) : ''
  const showGroup = !(hidden || (crews.length === 0 && !accountLabel && !failed))
  // The rows the group draws; while hidden it draws none, so an open Crew keeps the bar's row.
  const listedKey = showGroup ? crews.map(c => c.endpointId).join('\n') : ''
  const listedRelayCrews = useMemo(() => new Set(listedKey ? listedKey.split('\n') : []), [listedKey])
  return (
    <InstanceTabBar
      variant={variant}
      listedRelayCrews={listedRelayCrews}
      extraNew={!hidden && crews.some(c => c.unseen)}
      // Nothing while hidden or loading, or for an empty list with no account
      // label, so the bar stays as it is without the relay. A loaded empty list with an
      // account label still mounts the group, so it says there is nothing to open.
      extraGroups={!showGroup ? undefined : (close, headed) => (
        <TeamCrewsGroup crews={crews} failed={failed} refreshOnOpen={refreshOnOpen} close={close} headed={headed} />
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


/** Open the Crew a note's Open action names, or another dashboard window handed
 *  to this one, as its row would; a failed open says why in the top bar. The
 *  query is removed once it is read, so a reload or Back does not open it again.
 *  Nothing waits for a list read: the gateway lists the Crews again on every
 *  open and refuses one that isn't granted (or a signed-out account), so an
 *  open is sent at once and a later sign-in brings nothing back. */
function useOpenFromNote(crews: RelayCrewEntry[]): [string | null, () => void] {
  const [params, setParams] = useSearchParams()
  const renderedLocationIsCurrent = useRenderedLocationIsCurrent()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const [refusal, setRefusal] = useState<string | null>(null)
  const wanted = params.get(OPEN_RELAY_CREW_PARAM)
  // Read when an open starts, so a list update does not re-subscribe the hand-off.
  const crewsRef = useRef(crews)
  crewsRef.current = crews
  const openCrew = useCallback((endpointId: string) => {
    const nameNow = () => crewsRef.current.find(c => c.endpointId === endpointId)?.name
    setRefusal(null)
    void openRelayCrewTab(endpointId, nameNow() ?? endpointId, dispatch).then(res => {
      if (!res.ok) {
        // Named when the cached list has it by now: the switcher beside it still shows what was in front.
        const name = nameNow()
        setRefusal(name === undefined
          ? relayRefusalText(res.error)
          : joinSentences([
            i18nT('components.relayCrews.open_crew_failed', { name }),
            relayRefusalText(res.error),
          ]))
        return
      }
      void recordRelayCrewsSeen([endpointId]).then(({ saved, failed }) => {
        void clearRelayCrewsUnseen(queryClient, saved)
        // The Crew opened; only its New mark wasn't saved, so it will show again.
        if (failed) setRefusal(i18nT('components.relayCrews.seen_not_saved'))
      })
    })
  }, [dispatch, queryClient])
  useEffect(() => relayOpenRequests(openCrew), [openCrew])
  // The query lingers for a render after it is removed, so one Open opens once.
  const handled = useRef<string | null>(null)
  useEffect(() => {
    if (!wanted) {
      handled.current = null
      return
    }
    // A render that lags a navigation still in flight must not write the old page back.
    if (handled.current === wanted || !renderedLocationIsCurrent()) return
    handled.current = wanted
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete(OPEN_RELAY_CREW_PARAM)
      return next
    }, { replace: true })
    openCrew(wanted)
  }, [wanted, setParams, openCrew, renderedLocationIsCurrent])
  return [refusal, () => setRefusal(null)]
}
