/** How every list lane draws its session rows: each row behind its window stub
 *  with its paint-order stamp, the dormant section and its expander, the
 *  Older Sessions hint that ends a lane, and the crew groups below the `Local`
 *  rows. Built once per shell render (`createLaneRows`), so the stamps belong to
 *  that render's pass. */
import { Fragment } from 'react'
import { FolderBody } from '../../components/FolderBody'
import { formatRecentWindow } from '../recentWindow'
import { WindowedSessionRow } from '../chat/sessionRowWindow'
import type { StaleSplit } from '../staleCollapse'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import type { Slot } from './types'
import { sessionRowIdentity } from './rowIdentity'
import { type ConductorRowView } from './rows'
import type { CodeChannelCrewView } from './codeChannelCrew'
import { CrewGroupSection, LocalGroupHeader } from './CrewGroups'
import { DisclosureChevron, FOLDER_BODY_CLS, FOLDER_BODY_OPEN_PADDING, SIDEBAR_TEXT_CONTROL_CLS, STALE_TOGGLE_CLS } from './chrome'
import { SessionRow } from './SessionRow'
import type { SidebarScope } from './scope'

/** Rows at or past this paint ordinal share ONE `orderStamp`, so an insertion
 *  or reorder above them does not re-render them: they snap into their new
 *  position instead of springing there.
 *
 *  `orderStamp` exists so a displaced row re-renders and framer measures it
 *  (see `RowPlacement.orderStamp` in ./rows). Stamped as a plain ordinal, a New Chat landing at the
 *  top of a 160-session sidebar shifts every ordinal by one and voids all 160
 *  memo boundaries in one commit — 160 row bodies, 160 layout measurements and
 *  a group-wide spring — for a change whose visible effect is a handful of rows
 *  sliding down by one slot. The rows below the fold are displaced too, but
 *  nobody sees them move, so their spring buys nothing.
 *
 *  48 rows is roughly two sidebar viewports of 56px rows: the visible
 *  displacement stays continuous (the persistent-element rule in
 *  website/AGENTS.md is about what the user can see move), while the per-insert
 *  cost is bounded by this constant instead of growing with the session count.
 *  The deliberate casualty: a user scrolled deep into the list sees rows beyond
 *  the window snap rather than slide when something above them moves. The
 *  ordinal-bump above the window still has its usual cost, so this is a bound,
 *  not a fix for rows inside it. Pinned by ChatSidebar.rowMemo.test.tsx. */
export const SIDEBAR_DISPLACEMENT_WINDOW = 48

/** The lane row renderers for one shell render. */
export function createLaneRows(shell: SidebarScope) {
  const { collapsedCrews, collapsedSpaces, connected, crewEnable, historyOpen, hoverPinRef, isActiveRow, isStaleExempt,
    listNarrowed, openHistoryPane, rowActions, rowAnimEnabled, rowMotionHeld, rowViews, setStaleExpanded, shownCrewGroups,
    shownCrewRows, sidebarRows, staleCollapseMs, staleExpanded, staticRows, toggleCrewCollapsed,
    toggleSpaceCollapsed } = shell
  // The two halves render either side of the expander, so a held row crossing it
  // leaves the sub-list the anchor was measured against. Freeze the side instead.
  const holdStaleSide = (split: StaleSplit<Slot>): StaleSplit<Slot> => {
    const pin = hoverPinRef.current
    if (!pin || pin.scope !== 'list') return split
    const from = pin.staleSide ? split.fresh : split.stale
    // `pin.key` and `seenOrder` are origin-qualified (captured from
    // `data-session-row`), so every slot must be matched through
    // `sessionRowIdentity` — a raw `s.key` compare would miss a peer row and
    // collide two rows that share a raw key.
    const at = from.findIndex(s => sessionRowIdentity(s) === pin.key)
    // Absent from the side it does not belong on is the normal case, in every
    // container that does not hold the row as well as before it migrates.
    if (at < 0) return split
    const rank = new Map(pin.seenOrder.map((k, i) => [k, i]))
    const mine = rank.get(pin.key)
    if (mine == null) return split
    const to = pin.staleSide ? split.stale : split.fresh
    // Reseat by the CAPTURED order, so it returns to the position it was read at
    // rather than to the end of the half it is going back to.
    const seat = to.reduce((n, s) => {
      const r = rank.get(sessionRowIdentity(s))
      return n + (r != null && r < mine ? 1 : 0)
    }, 0)
    const seated = to.slice()
    seated.splice(Math.min(seat, seated.length), 0, from[at])
    const rest = from.filter(s => sessionRowIdentity(s) !== pin.key)
    return pin.staleSide ? { fresh: rest, stale: seated } : { fresh: seated, stale: rest }
  }

  // The row model's dormant split (inert while narrowed or under a non-date sort),
  // then the hover hold on top of it.
  const splitStale = (list: Slot[]): StaleSplit<Slot> =>
    holdStaleSide(sidebarRows.staleSplit(list, { collapseMs: staleCollapseMs, isExempt: isStaleExempt }))
  const renderStaleSection = (containerId: string, staleSlots: Slot[], depth: number, containerName?: string): React.ReactNode => {
    if (staleSlots.length === 0) return null
    const open = staleExpanded.has(containerId)
    const regionId = `stale-rows-${containerId}`
    const lblId = `stale-lbl-${containerId}`
    const ctxId = `stale-ctx-${containerId}`
    // One pluralised sentence carries the count AND says where the rows went,
    // so a folder badge of "2" over one visible row plus "1 dormant session
    // hidden" visibly adds up. A bare noun + count pill read as a category,
    // not as "the rest are in here" (gui-user-test friction on this row).
    const count = staleSlots.length
    const label = open
      ? i18nT('pages.chatSidebar.stale_collapse_row_shown', { count })
      : i18nT('pages.chatSidebar.stale_collapse_row_hidden', { count })
    // The threshold is otherwise only named in the sort/filter menu; the same
    // compact window label ("7d") ties the row back to that setting.
    const windowLabel = formatRecentWindow(staleCollapseMs)
    const hint = open
      ? i18nT('pages.chatSidebar.stale_collapse_row_hint_open', { window: windowLabel })
      : i18nT('pages.chatSidebar.stale_collapse_row_hint', { window: windowLabel })
    return (
      <Fragment key={`stale-${containerId}`}>
        {/* aria-labelledby composes the visible sentence + a visually-hidden
            container name, so AT announces "Show 1 sleeping session <folder>"
            — an aria-label would override the button contents and re-composing
            it per locale is exactly the concatenation trap the i18n rules ban. */}
        <button type="button"
          aria-expanded={open}
          aria-controls={regionId}
          aria-labelledby={`${lblId} ${ctxId}`}
          title={hint}
          data-testid={`stale-expander-${containerId}`}
          onClick={() => setStaleExpanded(prev => {
            const next = new Set(prev)
            if (next.has(containerId)) next.delete(containerId); else next.add(containerId)
            return next
          })}
          data-stale-toggle="" className={STALE_TOGGLE_CLS}>
          <DisclosureChevron open={open} size={12} />
          <span id={lblId} className="tabular-nums">{label}</span>
          <span id={ctxId} className="sr-only">{containerName
            ? i18nT('pages.chatSidebar.stale_collapse_ctx_in_name', { name: containerName })
            : i18nT('pages.chatSidebar.stale_collapse_row_ungrouped')}</span>
        </button>
        {/* The controlled region always exists so aria-controls never dangles
            in the collapsed state; only the rows are conditionally mounted. */}
        <div id={regionId} data-stale-region={containerId} hidden={!open}>{open && staleSlots.map(s => renderSessionRow(s, depth, false))}</div>
      </Fragment>
    )
  }
  // ── end stale-session collapse ─────────────────────────────────────────────

  // In-flow discovery affordance for the Older Sessions pane: a text row that
  // follows the LAST session of a lane. It triggers the same action as the
  // persistent footer, but answers a different question. The footer is a
  // structural control pinned under the scroll area for a user who already
  // knows the pane exists; this row sits where a user scanning the list runs
  // out of rows without finding their chat — which is exactly where a session
  // evicted from the open-tab list (idle eviction, restart, cleanup, a closed
  // tab) has gone. A new user has no other cue that sessions move anywhere, so
  // the row is what connects "my chat is gone" to "it is one click below".
  // Hidden while the pane is open: the pane itself is then the continuation.
  const renderOlderSessionsHint = (lane: string): React.ReactNode => {
    if (historyOpen) return null
    return (
      <button
        type="button"
        data-testid={`older-sessions-hint-${lane}`}
        onClick={openHistoryPane}
        {...uiLocation('sessions.show-all-older')}
        className={`mt-1 mx-1 px-2 py-1.5 ${SIDEBAR_TEXT_CONTROL_CLS}`}
      >
        {i18nT('pages.chatSidebar.show_all_older_sessions')}
      </button>
    )
  }

  // scope namespaces the Framer layoutId per render location. A multi-tag slot
  // can render in several columns at once; same layoutId in one LayoutGroup
  // collides (Framer paints one, hides the rest). Distinct scope = distinct id.
  // Paint-order stamp threaded through every row this render — see
  // RowPlacement.orderStamp for why the memo boundary needs it.
  // Stamps are keyed by (scope, row) and remembered for this render's closure.
  // Folder blocks are rendered by a CHILD (SortableFolderBlock /
  // SortableSubfolderBlock call `renderFolderBlock` from their own render), and
  // dnd-kit re-renders those children on its own, e.g. when it re-measures a
  // droppable after a row mounts. That deferred call runs after this render's
  // pass has stamped every row, so a running counter would hand every row in
  // the block the clamped stamp: all of them re-render with row animation off,
  // then again when the next shell render restores the real stamps. Reusing the
  // stamp the pass already assigned keeps the deferred render's row view
  // identical, so the rows bail out of it. `sessionRowStampCount` is the running
  // total across scopes, which is what the clamp counts.
  const sessionRowStamps = new Map<string, Map<string, number>>()
  let sessionRowStampCount = 0
  const renderSessionRow = (s: Slot, _indent: number, showDivider: boolean, scope = 'list', navScope = scope, holdContainer = navScope, conductor?: ConductorRowView, folderConductor?: boolean, crew?: CodeChannelCrewView) => {
    // Clamped, not raw: rows past the window share a stamp and bail out of a
    // displacement above them (see SIDEBAR_DISPLACEMENT_WINDOW).
    let scopeStamps = sessionRowStamps.get(scope)
    if (!scopeStamps) {
      scopeStamps = new Map<string, number>()
      sessionRowStamps.set(scope, scopeStamps)
    }
    const stampKey = sessionRowIdentity(s)
    let orderStamp = scopeStamps.get(stampKey)
    if (orderStamp === undefined) {
      orderStamp = Math.min(sessionRowStampCount, SIDEBAR_DISPLACEMENT_WINDOW)
      sessionRowStampCount += 1
      scopeStamps.set(stampKey, orderStamp)
    }
    const view = rowViews.view(s, {
      scope, navScope, holdContainer, showDivider, orderStamp, conductor, folderConductor, crew,
      // staticRows (the compositor drawer) folds into the one row-animation
      // gate: projection under a WAAPI-driven ancestor mis-attributes the
      // panel's motion to the rows, so the drawer disables row animation
      // wholesale. Outside it, enroll only the first two-viewport paint
      // window: every later row shares the clamped stamp and snaps, keeping
      // Framer's projection registry bounded at every total list size.
      rowAnimEnabled: rowAnimEnabled && orderStamp < SIDEBAR_DISPLACEMENT_WINDOW && !staticRows,
      rowMotionHeld,
    })
    // Windowed: far from the viewport the row renders as a cheap stub (see
    // pages/chat/sessionRowWindow). Each window root (the lane, or one board
    // column) mounts its own first rows at once so the initial paint shows real
    // rows; the row model's `windowOf` names the rows that never stub.
    const { title, keepMounted } = rowViews.windowOf(s)
    return (
      <WindowedSessionRow key={view.identity} rowId={view.identity} slotKey={s.key} navScope={navScope} holdContainer={holdContainer}
        title={title} keepMounted={keepMounted}>
        <SessionRow view={view} actions={rowActions} />
      </WindowedSessionRow>
    )
  }

  // The per-machine group chrome each list lane draws: `Local` above the lane's
  // own rows, the crew groups below them. Both are null with no crew group.
  const localGroupHeader = shownCrewGroups.length > 0 ? <LocalGroupHeader /> : null
  const renderCrewGroups = (): React.ReactNode => [crewEnable.notice, ...shownCrewGroups.map(g => (
    <CrewGroupSection key={g.id} group={g} rows={shownCrewRows.get(g.id) ?? []}
      collapsed={collapsedCrews.has(g.id)} onToggle={toggleCrewCollapsed} hideWhenEmpty={listNarrowed}
      chevron={<DisclosureChevron open={!collapsedCrews.has(g.id)} size={11} />}
      onEnable={crewEnable.onEnable}
      attention={{ isRunning: sidebarRows.isRunning, connected, actions: rowActions }}
      spaces={{
        collapsed: collapsedSpaces, onToggle: toggleSpaceCollapsed,
        // A local folder's own body: connector line, inset and collapse motion.
        body: (open, children) => <FolderBody padding={FOLDER_BODY_OPEN_PADDING} open={open}><div className={FOLDER_BODY_CLS}>{children}</div></FolderBody>,
      }}
      renderRows={(rows, scope) => rows.map((s, i) => renderSessionRow(
        s, 0, i < rows.length - 1 && !isActiveRow(s) && !isActiveRow(rows[i + 1]), scope,
      ))} />
  ))]
  return { splitStale, renderStaleSection, renderOlderSessionsHint, renderSessionRow, localGroupHeader, renderCrewGroups }
}

/** What `createLaneRows` hands the folder blocks and the lanes. */
export type LaneRows = ReturnType<typeof createLaneRows>
