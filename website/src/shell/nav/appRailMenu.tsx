import { useId, useMemo, useRef, type ReactElement } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowUp, Info, PinOff } from 'lucide-react'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuSeparator, ContextMenuTrigger } from '../../components/ui/context-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from '../../components/ErrorNotice'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { toggleAppNavHidden } from '../../lib/appNavHidden'
import { i18nT } from '../../i18n/t'
import { buildUpdateMap, registryQueryFn } from '../../pages/apps/useAppsData'
import { reportForError } from '../../utils/errorReport'
import type { AppNavRow } from './appRail'

/** Where focus goes once an Unpin has removed the row its menu opened on. */
type FocusAfterUnpin = { nav: Element; navId: string | null }

const detailPath = (name: string) => `/apps/detail/${name}`

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
 * Keeps an Escape that closes the menu from reaching the phone nav drawer. The
 * drawer's document listener closes the drawer on any Escape.
 */
const keepEscapeInMenu = (event: KeyboardEvent) => event.stopPropagation()

/**
 * Wraps one installed-app row of the rail in a right-click menu with Details and
 * Unpin. When the registry reports a newer version, Details carries the Library
 * tile's update badge, and the badge's tooltip is also the item's accessible
 * description. When the registry list fails to load, the menu shows the
 * failure in an `ErrorNotice`, so a failed read is not mistaken for "no
 * update", and offers the agent hand-off as a menu item of its own. The menu
 * starts no update. Update and Sync stay on the detail page that Details
 * opens.
 *
 * Renders `children` unchanged for a row that is not an installed app. The
 * trigger is disabled while any rail row is being dragged. A touch hold
 * therefore lifts a draggable row for reorder and opens the menu only on a row
 * that cannot be dragged.
 */
export function AppRailMenu({ row, dragActive, children }: {
  row: AppNavRow
  dragActive: boolean
  /** The row element. It receives the trigger's handlers and must spread them onto its DOM node. */
  children: ReactElement
}) {
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  // The registry query the Discover badge's observer fetches. With
  // `refetchOnMount: false`, a row that mounts after the list has loaded reads
  // the cache and sends no request of its own.
  const { data: registryData, error: registryError } = useQuery({
    queryKey: ['registry'],
    queryFn: registryQueryFn,
    staleTime: 5 * 60_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })
  const updateMap = useMemo(() => buildUpdateMap(registryData?.apps ?? []), [registryData])
  const focusAfterUnpin = useRef<FocusAfterUnpin | null>(null)
  const updateDescriptionId = useId()
  const registryErrorId = useId()
  const name = row.appName
  if (!name) return children
  const newVersion = updateMap.get(name)
  const updateText = newVersion === undefined
    ? null
    : i18nT('components.appstore.installedAppCard.update_to', { version: newVersion })
  const registryFailure = registryError == null
    ? null
    : { message: i18nT('pages.appsPage.failed_to_load_registry'), report: reportForError(registryError) }
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={dragActive} data-app-rail-row="">
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent
        onEscapeKeyDown={keepEscapeInMenu}
        onCloseAutoFocus={event => {
          const target = focusAfterUnpin.current
          if (!target) return
          focusAfterUnpin.current = null
          event.preventDefault()
          focusRecordedTarget(target)
        }}
      >
        <ContextMenuItem
          data-testid="app-rail-menu-details"
          aria-describedby={updateText ? updateDescriptionId : undefined}
          onSelect={() => guardedLeave(() => navigate(detailPath(name)), detailPath(name))}
        >
          <Info size={12} aria-hidden /> {i18nT('pages.libraryPage.tile_details')}
          {updateText && (
            <span
              aria-hidden
              title={updateText}
              data-testid="app-rail-menu-update-badge"
              className="ml-auto w-[15px] h-[15px] rounded-full bg-accent text-accent-fg flex items-center justify-center"
            >
              <ArrowUp size={9} strokeWidth={3} aria-hidden />
            </span>
          )}
        </ContextMenuItem>
        {/* The description target sits outside the item and is `hidden`, so its
            text stays out of the item's name and typeahead text. A screen reader
            still reads a hidden description target. */}
        {updateText && <span id={updateDescriptionId} hidden>{updateText}</span>}
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
