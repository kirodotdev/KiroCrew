/**
 * The "..." menu on a Bots-group row of the expanded desktop rail: Profile and
 * Remove from the sidebar, the mate twin of an installed app's rail row menu
 * (`appRailMenu.tsx`: Details + Unpin). Decision:
 * docs/decisions/2026-11-13-a-sidebar-bot-rows-menu-carries-profile-and-remove-from-the-sidebar.md.
 *
 * The trigger follows the rail's one rule for a control beside a row, the one
 * `appRowPopoutAction.tsx` documents: a SIBLING of the row's `role="button"`
 * element at the trailing end of the `relative group/approw` wrapper that holds
 * both (a button inside a button is one control to assistive tech), revealed on
 * hover and keyboard focus, and always drawn, at a larger target, where there is
 * no hover. Presses stop at the trigger and at the open menu, which sits inside
 * the sortable row in React's tree though it renders in a portal, so neither
 * picks the row up for a drag. Keys are not stopped: the rail has no keyboard
 * sensor.
 */
import { useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { MoreHorizontal, StarOff, UserRound } from 'lucide-react'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import { useGuardedLeave, useIsCurrentUrl } from '../../components/NavigationLeaveGuard'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import type { OpenMateSessionsState } from '../../lib/crewMode'
import { i18nT } from '../../i18n/t'

/** The press events dnd-kit's mouse and touch sensors start a drag from. */
const stop = (e: React.SyntheticEvent) => e.stopPropagation()

/** The rail row that takes focus once Remove has taken this row off the rail. */
type FocusAfterRemove = { nav: Element; navId: string }

/** The bots row a dock row's `NavItem` carries (`MateNavItem`'s `navId`). */
export const mateNavId = (m: Pick<MemberRosterRow, 'slug'>) => `mate-${m.slug}`

/**
 * The next dock bot row, else the previous one, else the rail's own Bots row
 * (`NAV_ITEMS` id `members`), which stays mounted when the dock empties and
 * returns no rows. Recorded by nav id: a fold re-forming can remount a row.
 */
function focusTargetAfterRemoving(row: HTMLElement): FocusAfterRemove | null {
  const nav = row.closest('[role="navigation"]')
  if (!nav) return null
  const rows = [...nav.querySelectorAll<HTMLElement>('[data-dock-mate-row]')]
  const at = rows.indexOf(row)
  const neighbour = rows[at + 1] ?? rows[at - 1]
  return { nav, navId: neighbour?.dataset.dockMateRow || 'members' }
}

function focusRecordedTarget({ nav, navId }: FocusAfterRemove) {
  const navRows = [...nav.querySelectorAll<HTMLElement>('[data-onboarding-nav]')]
  const target = navRows.find(el => el.dataset.onboardingNav === navId)
    ?? navRows.find(el => el.dataset.onboardingNav === 'members')
  target?.focus()
}

export function MateRowMenu({ m, onRemove }: {
  m: MemberRosterRow
  /** Unstars `m`: the dock's own optimistic write. */
  onRemove: (m: MemberRosterRow) => void
}) {
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const isCurrentUrl = useIsCurrentUrl()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const focusAfterRemove = useRef<FocusAfterRemove | null>(null)
  const label = i18nT('components.matesDock.row_menu', { name: crewDisplayName(m) })
  const path = `/members?member=${encodeURIComponent(m.name)}`

  const openProfile = () => {
    // Asked when the address changes, and marked `leaveGranted` once answered,
    // as a rail row click is (navRow.tsx).
    const here = isCurrentUrl(path)
    guardedLeave(() => {
      const state: OpenMateSessionsState & { leaveGranted?: true } = { openMateSessions: true, tab: 'profile', ...(here ? {} : { leaveGranted: true }) }
      navigate(path, { replace: here, state })
    }, path)
  }

  return (
    <DropdownMenu>
      {/* A plain button, as the pop-out action and the roster star are: `Btn`'s
          border and padding are not the rail's trailing-control look. */}
      <DropdownMenuTrigger asChild>
        <button
          ref={triggerRef}
          type="button"
          data-testid="mate-row-menu-trigger"
          aria-label={label}
          title={label}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 z-10 flex items-center justify-center w-6 h-6 rounded-md border-none cursor-pointer bg-transparent text-muted hover:text-text hover:bg-bg-hover transition-[opacity,color,background-color] duration-150 opacity-0 group-hover/approw:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100 [@media(hover:none)]:opacity-100 [@media(hover:none)]:w-8 [@media(hover:none)]:h-8 [@media(hover:none)]:right-1"
          onPointerDown={stop}
          onMouseDown={stop}
          onTouchStart={stop}
          onClick={stop}
        >
          <MoreHorizontal size={14} aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        side="right"
        // The rail's sensors start from these two; `pointerdown` must reach the
        // document, where the menu's outside-press check resets.
        onMouseDown={stop}
        onTouchStart={stop}
        onCloseAutoFocus={event => {
          const target = focusAfterRemove.current
          if (!target) return
          focusAfterRemove.current = null
          event.preventDefault()
          focusRecordedTarget(target)
        }}
      >
        <DropdownMenuItem data-testid="mate-row-menu-profile" onSelect={openProfile}>
          <UserRound size={12} aria-hidden /> {i18nT('pages.membersPage.profile_tab')}
        </DropdownMenuItem>
        <DropdownMenuItem
          data-testid="mate-row-menu-remove"
          onSelect={() => {
            const rowEl = triggerRef.current?.closest<HTMLElement>('[data-dock-mate-row]')
            focusAfterRemove.current = rowEl ? focusTargetAfterRemoving(rowEl) : null
            onRemove(m)
          }}
        >
          <StarOff size={12} aria-hidden /> {i18nT('components.matesDock.remove_from_sidebar')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
