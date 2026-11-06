import { useId, useMemo, useRef, useState, type ReactElement } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowUp, CheckCheck, ExternalLink, Info, LayoutGrid, PinOff, PowerOff, Trash2 } from 'lucide-react'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuSeparator, ContextMenuTrigger } from '../../components/ui/context-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../components/ErrorNotice'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { toggleAppNavHidden } from '../../lib/appNavHidden'
import { i18nT } from '../../i18n/t'
import { registryQueryFn } from '../../pages/apps/useAppsData'
import { libraryRailState, type LibraryRailAction } from '../../pages/apps/libraryRailIntent'
import { appAttentionNoteTimestamps } from '../../appNotificationBadges'
import { useAppDispatch, useAppSelector } from '../../store'
import { ackNotification } from '../../store/notificationsSlice'
import { reportForError } from '../../utils/errorReport'
import { offlineMenuRow } from '../../utils/offline'
import { useConnected } from '../../hooks/useConnected'
import { OfflineReasonRow } from '../../components/OfflineMenuReason'
import type { AppNavRow } from './appRail'
import { appRailMenuItems, type AppRailStatus } from './appRailStatus'

/** Where focus goes once an Unpin has removed the row its menu opened on. */
type FocusAfterUnpin = { nav: Element; navId: string | null }

const detailPath = (name: string) => `/apps/detail/${name}`
const LIBRARY_PATH = '/apps/library'

/**
 * Picks the row that takes focus after an Unpin removes `row`: the next app row,
 * else the previous one, else the Library row. Recorded by nav id because the
 * row can remount when the overflow list re-forms.
 */
function focusTargetAfterRemoving(row: HTMLElement): FocusAfterUnpin | null {
  const nav = row.closest('[role="navigation"]')
  if (!nav) return null
  const rows = [...nav.querySelectorAll<HTMLElement>('[data-app-rail-row]')]
  const at = rows.indexOf(row)
  const neighbour = rows[at + 1] ?? rows[at - 1]
  const navId = neighbour?.querySelector<HTMLElement>('[data-onboarding-nav]')?.dataset.onboardingNav ?? null
  return { nav, navId }
}

function focusRecordedTarget({ nav, navId }: FocusAfterUnpin) {
  const navRows = [...nav.querySelectorAll<HTMLElement>('[data-onboarding-nav]')]
  const target = navRows.find(el => el.dataset.onboardingNav === navId)
    ?? navRows.find(el => el.dataset.onboardingNav === 'apps-library')
  target?.focus()
}

/**
 * Opens the menu of the app row whose nav id is *navId*, at *point*.
 *
 * For the touch hold that the rail's drag sensor claimed: the trigger is
 * disabled (Radix marks it `data-disabled`) while that drag is live, and the
 * drag's end re-enables it on a later render, so the request waits for that,
 * a few frames at most. It goes through the trigger's own `contextmenu`
 * handler, the path a right-click takes. The drag ghost carries the same nav
 * id but no trigger, so it is skipped.
 */
export function openAppRailMenuAt(navId: string, point: { x: number; y: number }, attemptsLeft = 5) {
  setTimeout(() => {
    const trigger = [...document.querySelectorAll<HTMLElement>('[data-onboarding-nav]')]
      .filter(el => el.dataset.onboardingNav === navId)
      .map(el => el.closest<HTMLElement>('[data-app-rail-row]'))
      .find(el => el !== null)
    if (!trigger) return
    if (trigger.hasAttribute('data-disabled')) {
      if (attemptsLeft > 0) openAppRailMenuAt(navId, point, attemptsLeft - 1)
      return
    }
    trigger.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: point.x, clientY: point.y,
    }))
  }, 16)
}

/**
 * Keeps an Escape that closes the menu from reaching the phone nav drawer. The
 * drawer's document listener closes the drawer on any Escape.
 */
const keepEscapeInMenu = (event: KeyboardEvent) => event.stopPropagation()

/**
 * Wraps one installed-app row of the rail in its actions menu, opened by a
 * right-click, the context-menu key, or a touch hold (see `stillTouchHoldPoint`).
 * Which items show is `appRailMenuItems`; the menu adds no action path of its
 * own:
 *
 * - Update goes to the detail page with `autoAction: 'update'`, the Library's
 *   own hand-off, so the update runs with that page's log and trust consent.
 * - Disable and Uninstall go to the Library with a router-state request (see
 *   `libraryRailIntent`), where the result, any error and the undo show. The
 *   Library opens its uninstall confirmation, so this menu removes nothing.
 * - Mark notifications as read acknowledges, through the bell's own thunk,
 *   exactly the notes behind the row's derived count. The menu stays open
 *   until the acknowledgements settle and shows a failure in an `ErrorNotice`.
 *
 * When the registry list fails to load, the menu shows the failure in an
 * `ErrorNotice`, so a failed read is not mistaken for "no update", and offers
 * the agent hand-off as a menu item of its own.
 *
 * Renders `children` unchanged for a row that is not an installed app. The
 * trigger is disabled while any rail row is being dragged.
 */
export function AppRailMenu({ row, dragActive, status, children }: {
  row: AppNavRow
  dragActive: boolean
  /** The app's rail status (`useRailBadges`), the source of its row's marks. */
  status: AppRailStatus | undefined
  /** The row element. It receives the trigger's handlers and must spread them onto its DOM node. */
  children: ReactElement
}) {
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const dispatch = useAppDispatch()
  // Offline, the items that change the app are refused the way the Library
  // tile's are: dimmed, named in their tooltip, and the menu stays open with
  // the reason row.
  const connected = useConnected()
  // The registry query the Discover badge's observer fetches, read here only
  // for its error. With `refetchOnMount: false`, a row that mounts after the
  // list has loaded reads the cache and sends no request of its own.
  const { error: registryError } = useQuery({
    queryKey: ['registry'],
    queryFn: registryQueryFn,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })
  const notes = useAppSelector(s => s.notifications.items)
  const name = row.appName
  const unreadTs = useMemo(() => (name ? appAttentionNoteTimestamps(notes, name) : []), [notes, name])
  const focusAfterUnpin = useRef<FocusAfterUnpin | null>(null)
  // A Library request hands focus to the app's tile, so the menu must not take
  // it back to the rail row as it closes. Set only once the leave guard lets
  // the navigation run, and cleared on every open, so a refused or
  // late-confirmed leave cannot carry it into a later close.
  const leavingForLibrary = useRef(false)
  const registryErrorId = useId()
  // Mark notifications as read keeps the menu open until every
  // acknowledgement settles, so a failure shows in the menu that started it.
  // `acking` holds the batch in flight: the bell marks each note read at once
  // and puts it back on failure, so the item must not depend on `unreadTs`
  // while the batch runs.
  const [acking, setAcking] = useState<readonly string[] | null>(null)
  const [ackFailed, setAckFailed] = useState(false)
  const ackErrorId = useId()
  const contentRef = useRef<HTMLDivElement>(null)
  if (!name) return children
  const items = appRailMenuItems(status, unreadTs.length)
  const go = (path: string, state?: object, onLeave?: () => void) => guardedLeave(() => {
    onLeave?.()
    navigate(path, state ? { state } : undefined)
  }, path)
  const toLibrary = (action: LibraryRailAction) =>
    go(LIBRARY_PATH, libraryRailState({ app: name, action }), () => { leavingForLibrary.current = true })
  const markRead = (event: Event) => {
    event.preventDefault()
    if (acking) return
    const batch = unreadTs
    setAckFailed(false)
    setAcking(batch)
    void Promise.all(batch.map(ts => dispatch(ackNotification(ts)))).then(results => {
      setAcking(null)
      if (results.some(result => ackNotification.rejected.match(result))) {
        setAckFailed(true)
        return
      }
      // The wrapper owns the menu's open state, so close it the way a person
      // does: Radix listens for Escape on the document.
      contentRef.current?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
  }
  const offlineVerb = i18nT('utils.offline.manage_apps')
  const updateLabel = status?.newVersion === undefined
    ? ''
    : i18nT('components.appstore.installedAppCard.update_to', { version: status.newVersion })
  const ackFailureMessage = ackFailed ? i18nT('app.app_rail_mark_read_failed') : null
  const registryFailure = registryError == null
    ? null
    : { message: i18nT('pages.appsPage.failed_to_load_registry'), report: reportForError(registryError) }
  return (
    <ContextMenu onOpenChange={open => {
      if (!open) return
      leavingForLibrary.current = false
      setAckFailed(false)
    }}>
      <ContextMenuTrigger asChild disabled={dragActive} data-app-rail-row="">
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent
        ref={contentRef}
        onEscapeKeyDown={keepEscapeInMenu}
        onCloseAutoFocus={event => {
          if (leavingForLibrary.current) {
            leavingForLibrary.current = false
            event.preventDefault()
            return
          }
          const target = focusAfterUnpin.current
          if (!target) return
          focusAfterUnpin.current = null
          event.preventDefault()
          focusRecordedTarget(target)
        }}
      >
        <ContextMenuItem data-testid="app-rail-menu-open" onSelect={() => go(row.path)}>
          <ExternalLink size={12} aria-hidden /> {i18nT('app.app_rail_open')}
        </ContextMenuItem>
        {items.includes('update') && status?.newVersion !== undefined && (
          <ContextMenuItem
            data-testid="app-rail-menu-update"
            {...offlineMenuRow(connected, offlineVerb, updateLabel, () => go(detailPath(name), { autoAction: 'update' }))}
          >
            <ArrowUp size={12} aria-hidden /> {updateLabel}
          </ContextMenuItem>
        )}
        <ContextMenuItem data-testid="app-rail-menu-details" onSelect={() => go(detailPath(name))}>
          <Info size={12} aria-hidden /> {i18nT('pages.libraryPage.tile_details')}
        </ContextMenuItem>
        <ContextMenuItem data-testid="app-rail-menu-library" onSelect={() => toLibrary('focus')}>
          <LayoutGrid size={12} aria-hidden /> {i18nT('app.app_rail_open_in_library')}
        </ContextMenuItem>
        {(items.includes('mark-read') || acking !== null) && (
          <ContextMenuItem
            data-testid="app-rail-menu-mark-read"
            aria-describedby={ackFailureMessage ? ackErrorId : undefined}
            onSelect={markRead}
          >
            <CheckCheck size={12} aria-hidden /> {i18nT('app.app_rail_mark_read')}
          </ContextMenuItem>
        )}
        {ackFailureMessage && (
          <>
            <div className="px-2 py-1.5 max-w-[260px]">
              <ErrorNotice
                id={ackErrorId}
                variant="inline"
                className="whitespace-normal"
                message={ackFailureMessage}
                testId="app-rail-menu-mark-read-error"
              />
            </div>
            <ErrorNoticeMenuItem Item={ContextMenuItem} message={ackFailureMessage} describedBy={ackErrorId} />
          </>
        )}
        <ContextMenuSeparator />
        <ContextMenuItem
          data-testid="app-rail-menu-disable"
          {...offlineMenuRow(connected, offlineVerb, i18nT('components.appstore.installedAppCard.disable'), () => toLibrary('disable'))}
        >
          <PowerOff size={12} aria-hidden /> {i18nT('components.appstore.installedAppCard.disable')}
        </ContextMenuItem>
        <ContextMenuItem
          data-testid="app-rail-menu-unpin"
          onSelect={() => {
            // The open menu's trigger carries `data-state="open"`, and only one
            // menu is open at a time.
            const rowEl = document.querySelector<HTMLElement>('[data-app-rail-row][data-state="open"]')
            focusAfterUnpin.current = rowEl ? focusTargetAfterRemoving(rowEl) : null
            toggleAppNavHidden(row.id)
          }}
        >
          <PinOff size={12} aria-hidden /> {i18nT('pages.libraryPage.unpin')}
        </ContextMenuItem>
        {items.includes('uninstall') && (
          <>
            <ContextMenuSeparator />
            {/* The danger color sits on the item's content, not on the item:
                `ContextMenuItem` has no danger variant, and its own text color
                is the primitive's to own. */}
            <ContextMenuItem
              data-testid="app-rail-menu-uninstall"
              {...offlineMenuRow(connected, offlineVerb, i18nT('app.app_rail_uninstall'), () => toLibrary('uninstall'))}
            >
              <span className="flex items-center gap-2 text-danger">
                <Trash2 size={12} aria-hidden /> {i18nT('app.app_rail_uninstall')}
              </span>
            </ContextMenuItem>
          </>
        )}
        {!connected && <OfflineReasonRow testId="app-rail-menu-offline-reason" className="w-0 min-w-full px-2 py-1" />}
        {registryFailure && (
          <>
            <ContextMenuSeparator />
            <div className="px-2 py-1.5 max-w-[260px]">
              <ErrorNotice
                id={registryErrorId}
                variant="inline"
                className="whitespace-normal"
                message={registryFailure.message}
                report={registryFailure.report}
                testId="app-rail-menu-registry-error"
              />
            </div>
            <ErrorNoticeMenuItem
              Item={ContextMenuItem}
              message={registryFailure.message}
              report={registryFailure.report}
              describedBy={registryErrorId}
            />
          </>
        )}
      </ContextMenuContent>
    </ContextMenu>
  )
}
