import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ChevronUp, MoreHorizontal } from 'lucide-react'
import { useSortable, arrayMove } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import type { DragStartEvent, DragEndEvent } from '@dnd-kit/core'
import { useDndSensors } from '../../hooks/useDndSensors'
import { usePreviewFlagRevision } from '../../hooks/usePreviewFlag'
import { APP_NAV_ORDER_KEY, buildReorderBaseline, mergeVisibleReorder, readAppNavOrder, useAppNavHidden } from '../../lib/appNavHidden'
import { useNavPinned } from '../../lib/navPinned'
import { haptic } from '../../lib/haptic'
import { surfacePreviewEnabled } from '../../surfaces/registry'
import { safeSetItem } from '../../utils/safeStorage'
import { i18nT } from '../../i18n/t'
import { NAV_ITEMS } from './navItems'
import { useNavTip } from './navTip'

/** One app row as the rail's Apps group lists it. */
export interface AppNavRow { path: string; id: string; label: string; group: string; icon: React.ReactElement; appName?: string }

/**
 * The rail's advertised destinations and the Apps group's order: which surfaces
 * the rail may show (preview flags, pinned sub-items), the Apps group merged with
 * the installed apps in the user's saved order minus the rows they unpinned, and
 * the drag-reorder that writes that order back.
 */
export function useAppRailOrder(appNavItems: AppNavRow[]) {
  const [appNavOrder, setAppNavOrder] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem(APP_NAV_ORDER_KEY) || '[]') } catch { return [] } })
  // Which app rows the user UNPINNED from the sidebar via the Library
  // launchpad grid (`mc-app-nav-hidden`, owned by `lib/appNavHidden.ts`).
  // The shared hook keeps this live under both propagation paths (same-tab
  // change event + cross-tab `storage`), so a pin toggle in LibraryPage
  // re-renders the rail immediately.
  const appNavHidden = useAppNavHidden()
  // Preview-gated surfaces (see `utils/previewFlags.ts`) must not be advertised
  // anywhere. `surfacePreviewEnabled` is a synchronous storage read, so the rail
  // needs this subscription to re-render when Settings > Developer > Feature Previews flips a flag —
  // otherwise the row would appear only after a reload. The revision also
  // invalidates the memo below, which a bare re-render would not recompute.
  const previewFlagRevision = usePreviewFlagRevision()
  // Which promotable sub-items the user has pinned to the rail. Live under both
  // propagation paths (same-tab event + cross-tab `storage`), so toggling the
  // pin control in a page header repaints the rail without a reload.
  const pinnedNavIds = useNavPinned()
  // ONE derivation feeding BOTH rail list paths (the Apps group just below and
  // the Main group in the rail markup `App.tsx` renders). Filtering per call site is what leaks an
  // unreleased surface: the first preview-gated Apps-group surface would have
  // shown up while only the Main branch was gated. The pinned test rides here
  // for the same reason — a `pinnable` sub-item filtered in only one branch
  // would appear on the rail in the other without the user pinning it.
  const advertisedNavItems = useMemo(
    () => NAV_ITEMS.filter(n => surfacePreviewEnabled(n) && (!n.pinnable || pinnedNavIds.has(n.id))),
    // The revision is an invalidation token: what `surfacePreviewEnabled` reads
    // lives in localStorage, not in React state, so nothing else here can
    // express the dep. The directive stays on ONE line directly above the deps
    // array -- `eslint-disable-next-line` targets the literal next line, so a
    // rationale wrapped after it aims the directive at its own continuation and
    // suppresses nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [previewFlagRevision, pinnedNavIds],
  )
  // Apps nav reorder is dnd-kit sortable (mirrors QueueStack): rows reflow to
  // open a gap as you drag, and a DragOverlay renders the floating ghost.
  // activeAppDragId tracks the app being dragged, for the overlay + source dim.
  const [activeAppDragId, setActiveAppDragId] = useState<string | null>(null)
  // Split mouse/touch sensors so touch can both scroll AND drag; the split and
  // its WebKit reasoning live in the shared hook. 8px of mouse travel is this
  // rail's own choice: a plain click has to reach NavItem navigation, so the
  // threshold sits higher than a list whose rows only select.
  const appDndSensors = useDndSensors({ distance: 8 })
  const { sortedAppGroup, sortedAppGroupAllIds } = useMemo(() => {
    // Drop rows the user unpinned in the Library launchpad BEFORE the
    // APPS_NAV_LIMIT slice downstream, so a hidden row never consumes a
    // visible slot. The hidden set only ever contains ids written by the
    // Library grid — `appNavTarget(app).id` values, byte-identical to the
    // ids these rows carry — so set membership can only hide grid-managed
    // app rows. The Discover/Library built-ins are not list rows here
    // (`hiddenFromNav`, rendered as the section-header accent links) and
    // can never be filtered out by this.
    //
    // `sortedAppGroupAllIds` is the same effective order WITHOUT the hidden
    // filter — what the rail would show if everything were pinned. It seeds
    // the drag-reorder merge so a hidden app's position survives even when
    // `mc-app-nav-order` is empty or has never listed it (its slot is then
    // implicit in this natural order, and persisting only the visible ids
    // would erase it).
    const all = [...advertisedNavItems.filter(n => n.group === 'Apps'), ...appNavItems]
    const orderMap = new Map(appNavOrder.map((id, i) => [id, i]))
    const sortedAll = appNavOrder.length === 0
      ? all
      : [...all].sort((a, b) => (orderMap.get(a.id) ?? 999) - (orderMap.get(b.id) ?? 999))
    return {
      sortedAppGroupAllIds: sortedAll.map(n => n.id),
      sortedAppGroup: sortedAll.filter(n => !appNavHidden.has(n.id)),
    }
  }, [advertisedNavItems, appNavItems, appNavOrder, appNavHidden])
  // dnd-kit fires this once the sensor's constraint is met (the 250ms touch hold
  // or the mouse distance), so the tap marks the pick-up itself, not the touch.
  // Touch is the only sensor with an engine under it; elsewhere haptic no-ops.
  const handleAppDragStart = useCallback((e: DragStartEvent) => { haptic('medium'); setActiveAppDragId(e.active.id as string) }, [])
  // Materialize implicit sidebar positions the moment an app is HIDDEN: once
  // an id is in the hidden set, its position must live in the persisted
  // order, because every later event that could erase the implicit source —
  // disabling the app (drops its nav row), uninstall, a reorder — happens
  // while the row is invisible. Persisting the full effective order at
  // hide-time makes `mc-app-nav-order` authoritative for hidden ids, closing
  // the whole class (hide→disable→drag→re-pin lands the app back in its
  // original slot). Guarded to ids that currently HAVE an effective row:
  // an id with none (already uninstalled) cannot be materialized and must
  // not retrigger the write.
  useEffect(() => {
    if (appNavHidden.size === 0) return
    // FRESH read, never the React copy: another tab may have reordered
    // since this tab last wrote, and a baseline seeded with the stale copy
    // would overwrite that tab's saved order (there is no cross-tab
    // propagation for the order key).
    const stored = readAppNavOrder()
    const persisted = new Set(stored)
    const materializable = [...appNavHidden].some(
      id => !persisted.has(id) && sortedAppGroupAllIds.includes(id))
    if (!materializable) return
    const next = buildReorderBaseline(stored, sortedAppGroupAllIds)
    // Persist FIRST and mirror into state only on success: a failed write
    // (quota, storage denied) leaves the fresh-read guard permanently
    // unsatisfied, so setting state anyway would re-trigger this effect with
    // a new array reference every render — an infinite update loop. Skipping
    // the state set on failure loses nothing visible (the baseline preserves
    // the currently rendered order), and the write is retried on the next
    // deps change.
    if (safeSetItem(APP_NAV_ORDER_KEY, JSON.stringify(next))) {
      setAppNavOrder(next)
    }
  }, [appNavHidden, sortedAppGroupAllIds])
  const handleAppDragEnd = useCallback((e: DragEndEvent) => {
    setActiveAppDragId(null)
    const { active, over } = e
    if (!over || active.id === over.id) return
    // Past the guard, so the tap means the rail really reordered.
    haptic('light')
    const ids = sortedAppGroup.map(n => n.id)
    const from = ids.indexOf(active.id as string)
    const to = ids.indexOf(over.id as string)
    if (from < 0 || to < 0) return
    const moved = arrayMove(ids, from, to)
    // `sortedAppGroup` excludes hidden (unpinned) rows, so persisting `moved`
    // alone would ERASE a hidden app's slot — re-pinning would dump it at the
    // end. The baseline is the FRESHLY-READ persisted order UNION the current
    // effective order: reading storage (not the React copy) keeps a reorder
    // made in another tab from being overwritten, the persisted array can
    // remember ids with no current nav row at all (a hidden app that is
    // temporarily DISABLED has no appNavItems entry), and the effective tail
    // carries never-reordered apps whose slot is only implicit
    // (see buildReorderBaseline / mergeVisibleReorder).
    const next = mergeVisibleReorder(
      buildReorderBaseline(readAppNavOrder(), sortedAppGroupAllIds), ids, moved)
    if (safeSetItem(APP_NAV_ORDER_KEY, JSON.stringify(next))) {
      setAppNavOrder(next)
    }
  }, [sortedAppGroup, sortedAppGroupAllIds])
  // Drag cancel (e.g. Escape) fires onDragCancel, NOT onDragEnd — clear the
  // active id here too, else the source row stays dimmed and the overlay ghost
  // lingers. Mirrors ChatSidebar's handleSidebarDragCancel.
  const handleAppDragCancel = useCallback(() => setActiveAppDragId(null), [])
  return {
    advertisedNavItems, sortedAppGroup, activeAppDragId, appDndSensors,
    handleAppDragStart, handleAppDragEnd, handleAppDragCancel,
  }
}

/** dnd-kit sortable wrapper for one Apps-nav row. Mirrors SortableFolderBlock in
 *  ChatSidebar: setNodeRef + sortable transform position the row so siblings
 *  reflow to open a gap as it's dragged; the source dims while a DragOverlay
 *  renders the floating ghost. Only `listeners` are spread (not `attributes`),
 *  so the inner NavItem keeps its own role="button"/tabIndex and no nested
 *  drag role is exposed on the wrapper (role="presentation"). Sensor activation
 *  constraints (see appDndSensors) let a plain click/tap reach NavItem
 *  navigation; only a deliberate mouse-drag or touch press-and-hold reorders. */
export function SortableAppNavRow({ id, children }: { id: string; children: ReactNode }) {
  const { setNodeRef, listeners, transform, transition, isDragging } = useSortable({ id })
  return (
    <div
      ref={setNodeRef}
      role="presentation"
      style={{
        transform: transform ? CSS.Transform.toString(transform) : undefined,
        transition: transition || undefined,
        opacity: isDragging ? 0.4 : 1,
        // 'manipulation' (not 'none') keeps native vertical scroll working when
        // a swipe starts on a row — the TouchSensor's press-and-hold delay is
        // what arms a drag, so the row doesn't need to suppress all gestures.
        touchAction: 'manipulation',
      }}
      {...listeners}
    >
      {children}
    </div>
  )
}

/** The "N more" / "Show less" Apps-overflow toggle. Mirrors NavItem: a text row
 *  when expanded, an icon-only button with a portaled hover label when the
 *  sidebar is collapsed, so the collapse-to-more behavior works in both modes. */
export function NavToggle({ collapsed, expanded, hiddenCount, onClick }: {
  collapsed: boolean; expanded: boolean; hiddenCount: number; onClick: () => void
}) {
  const { tip, tipOn, rowRef, showTip, hideTip, dismissTip, pointerProps: tipPointerProps } = useNavTip<HTMLButtonElement>(collapsed)
  // `hiddenCount === 0 && !expanded` happens when the only overflow item is the
  // active app (kept visible) — nothing is actually hidden, so the toggle just
  // offers to re-collapse rather than reveal "0 more".
  const showsCollapse = expanded || hiddenCount === 0
  const Icon = showsCollapse ? ChevronUp : MoreHorizontal
  const labelText = showsCollapse ? i18nT('app.show_less') : i18nT('app.n_more', { count: hiddenCount })
  const titleText = showsCollapse ? i18nT('app.show_fewer_apps') : i18nT('app.show_more_apps', { count: hiddenCount })
  return (
    <button ref={rowRef}
      className="group/nav relative flex items-center rounded-md cursor-pointer text-sm font-medium whitespace-nowrap gap-2.5 py-2 pl-3 pr-3 transition-colors duration-200 text-muted hover:text-text hover:bg-bg-hover/60 bg-transparent border-none w-full"
      // Dismiss the hover label on activation, without the fade-out. Unlike a
      // NavItem (which stays put when clicked, so the pointer is still
      // legitimately over it), activating this toggle re-flows the Apps list and
      // moves the row out from under a stationary cursor — no mouseleave is
      // dispatched, so the flyout used to hang at the old coordinates until the
      // click's focus was lost. Fading it out is not enough either: the label
      // text flips on activation, so a fading ghost flashes the OPPOSITE label.
      // This runs after the focus a pointer press produces (focus precedes
      // click), so it also clears a label that focus had just re-armed.
      onClick={() => { dismissTip(); onClick() }}
      aria-expanded={expanded}
      // WCAG 2.5.3 Label in Name: while the text label is visible the accessible
      // name must contain it, so the name IS the label; collapsed (icon-only)
      // mode uses the fuller title instead.
      aria-label={collapsed ? titleText : labelText}
      title={titleText}
      {...tipPointerProps}
      onMouseEnter={showTip}
      onMouseLeave={hideTip}
      // Surface the collapsed-mode hover label on keyboard focus too (button is
      // already focusable). Inert when expanded — showTip/hideTip gate on collapsed.
      onFocus={showTip}
      onBlur={hideTip}
    >
      <span className="w-4 h-4 flex items-center justify-center shrink-0 opacity-70"><Icon size={16} /></span>
      {/* Same reason as the nav-item label in `App.tsx`'s `NavItem`: clipped by `whitespace-nowrap
          overflow-hidden`, so the full string lives on `aria-label` (not `title` — see the
          getByTitle collision note on the NavItem span). */}
      {!collapsed && (
        <span aria-label={labelText} className="whitespace-nowrap overflow-hidden">
          {labelText}
        </span>
      )}
      {collapsed && tip && createPortal(
        <div
          className={`fixed flex items-center gap-2.5 pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text text-sm font-medium z-[9999] pointer-events-none whitespace-nowrap transition-opacity duration-150 ${tipOn ? 'opacity-100' : 'opacity-0'}`}
          style={{ top: tip.top, left: tip.left, height: tip.height }}
        >
          <span className="w-4 h-4 flex items-center justify-center shrink-0"><Icon size={16} /></span>
          {labelText}
        </div>,
        document.body
      )}
    </button>
  )
}
