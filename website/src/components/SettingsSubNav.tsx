import React, { useEffect } from 'react'
import { useSearchParams, useLocation, useNavigate } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { NavBackBar } from './NavBackBar'
import { SUBNAV_PARAM, SUBNAV_LEGACY_PARAMS, deleteSubSelection, COARSE_TOUCH_TARGET, SUBNAV_PUSH_STATE } from './subNavParams'
import { useContainerWidth } from '../hooks/useContainerWidth'
import { useIsMobile } from '../hooks/useIsMobile'

/** Below this container width the rail and the detail pane stack: the rail
 *  becomes the whole view and choosing an item replaces it (with a back
 *  link). Shared by every SubNav host so the responsive contract is uniform. */
const TWO_PANE_MIN_WIDTH = 760

export interface SubNavItem<K extends string = string> {
  key: K
  /** Already-localized row label. */
  label: string
  /** Leading glyph: a lucide icon (rail) or a brand logo (card). */
  icon?: React.ReactNode
  /** Already-localized group header; adjacent items sharing a group render
   *  under one header. Omit for an ungrouped flat list. */
  group?: string
  /** Live, FACTUAL second line (a count, a status, an expiry) — never a
   *  verdict. Rendered under the label; a ReactNode so hosts can carry a
   *  status dot or an "off by admin" chip. */
  summary?: React.ReactNode
  /** Dim the row (e.g. a policy-denied channel). Selection stays enabled so
   *  the disabled-explanation pane remains reachable. */
  dimmed?: boolean
}

interface SettingsSubNavProps<K extends string> {
  items: readonly SubNavItem<K>[]
  /** Detail pane for the current selection. `null` only in narrow mode while
   *  the list itself is the view. The child is reconciled by POSITION across
   *  width changes (same slot order in both layouts) so a width transition
   *  never remounts it — remounting would discard unsaved form drafts. */
  children: (active: K | null) => React.ReactNode
  /** Rail width in the two-pane layout. */
  railWidth?: number
  /** Accessible name for the listbox. */
  listLabel: string
  /** Narrow-mode back-link text; defaults to `listLabel`. Kept separate
   *  because hosts historically used a shorter word for the back action
   *  ("Channels") than for the list's accessible name ("Chat channels"). */
  backLabel?: string
  /** Content pinned to this SubNav in EVERY mode (e.g. Security's
   *  data-classification notice). A slot rather than a host-rendered sibling
   *  because its position depends on the responsive mode: above the list at
   *  rest, but BELOW the back bar when narrow mode has drilled in — a sibling
   *  above the component would sit above the back bar and displace the
   *  one-position back control the mobile stack promises. */
  banner?: React.ReactNode
}

/** Second-level navigation inside one Settings tab: a responsive list-detail
 *  container. Wide content area = persistent rail + detail side by side;
 *  narrow = the rail alone, drilling into a full-width detail view with a
 *  back button. Selection is URL-backed (?sub=<key>) so deep links survive
 *  reloads and the command palette / SettingsSearch can mount the right pane
 *  BEFORE the highlight hook queries the DOM. */
export function SettingsSubNav<K extends string>({
  items,
  children,
  railWidth = 248,
  listLabel,
  backLabel,
  banner,
}: SettingsSubNavProps<K>) {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const [containerRef, width] = useContainerWidth<HTMLDivElement>()
  const isMobile = useIsMobile()
  // null width = first paint before measurement; assume wide to avoid flashing
  // the narrow layout on desktop.
  const twoPane = width === null || width >= TWO_PANE_MIN_WIDTH

  // Canonical param wins; legacy aliases are read-only fallbacks so a stale
  // legacy value can never override an explicit ?sub=. The alias list is the
  // shared SUBNAV_LEGACY_PARAMS constant, not a per-host prop: values are
  // validated against `items` below and the hosts' key sets are disjoint, so
  // per-host scoping bought nothing while spelling the list twice.
  const raw = params.get(SUBNAV_PARAM) ?? SUBNAV_LEGACY_PARAMS.map(p => params.get(p)).find(v => v != null) ?? null
  const selectedKey = items.some(i => i.key === raw) ? (raw as K) : null
  // Wide mode always shows a detail pane; default to the first item.
  const effectiveKey = selectedKey ?? (twoPane ? items[0]?.key ?? null : null)

  // Self-heal an invalid selection value (?sub=garbage, or a legacy alias
  // whose key set belongs to a different host). SidePanelLayout yields its
  // chrome on param PRESENCE while the back bar here renders only for a
  // VALID key — leaving the bogus param in place would strand a mobile pane
  // with no navigation affordance at all. Replace, not push: a healed URL is
  // a correction, not a level.
  useEffect(() => {
    if (raw != null && selectedKey == null) {
      setParams(prev => {
        const next = new URLSearchParams(prev)
        deleteSubSelection(next)
        return next
      }, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raw, selectedKey])

  const select = (key: K | null) => {
    // Clearing the selection (the back bar) POPS when this stack pushed the
    // current entry: a replace-write would twin the list entry and dead-end
    // the next platform back-swipe. Replace remains for cold deep links.
    if (key == null && (location.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]) {
      navigate(-1)
      return
    }
    setParams(prev => {
      const next = new URLSearchParams(prev)
      if (key) next.set(SUBNAV_PARAM, key)
      else next.delete(SUBNAV_PARAM)
      // Writing supersedes the aliases: leaving a legacy param behind would make
      // the NEXT read ambiguous once ?sub= is later removed (back to list).
      for (const p of SUBNAV_LEGACY_PARAMS) next.delete(p)
      return next
      // Narrow-mode drill-in is a PUSH so the platform back gesture pops one
      // level, matching the iOS stack; the wide-mode rail click stays replace —
      // a selector should not mint history entries.
    }, {
      replace: twoPane || key == null,
      state: !twoPane && key != null ? { [SUBNAV_PUSH_STATE]: true } : undefined,
    })
  }

  // Canonicalize the wide-mode implicit selection into the URL. Without this,
  // shrinking the container below the two-pane breakpoint would flip
  // effectiveKey to null and drop the implicitly-selected pane to the bare
  // list. Gated on a REAL measurement (width !== null): the pre-measurement
  // paint optimistically renders wide, but writing before the ResizeObserver
  // reports would make a fresh narrow visit open a pane instead of the list.
  useEffect(() => {
    if (width !== null && twoPane && !selectedKey && items.length > 0) select(items[0].key)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, twoPane, selectedKey])

  // Adjacent items sharing a `group` render under one header (order in
  // `items` drives everything; entries of a group must stay adjacent).
  const grouped = items.reduce<{ group: string | undefined; items: SubNavItem<K>[] }[]>((acc, item) => {
    const last = acc[acc.length - 1]
    if (last && last.group === item.group) last.items.push(item)
    else acc.push({ group: item.group, items: [item] })
    return acc
  }, [])

  const row = (item: SubNavItem<K>) => {
    const active = twoPane && item.key === effectiveKey
    // Selection semantics belong to the wide rail alone: there a row picks
    // which pane is shown beside it (listbox/option + aria-selected). Narrow
    // rows NAVIGATE — they push a level, the same action as the mobile root
    // list — so they carry the root list's list/listitem vocabulary instead.
    // One widget must not announce as a selector at one level and a list at
    // the next.
    const btn = (
      <button
        key={twoPane ? item.key : undefined}
        type="button"
        role={twoPane ? 'option' : undefined}
        aria-selected={twoPane ? active : undefined}
        onClick={() => select(item.key)}
        // No title attribute: labels WRAP (line-clamp-2) rather than truncate,
        // and a title mirroring a hardcoded brand name (WeCom, Teams) registers
        // as an untranslated-attribute site on the i18n render gate.
        // min-h-11 under a coarse pointer = Apple HIG's 44pt touch minimum;
        // desktop pointer density is unchanged.
        className={`flex items-center gap-2.5 w-full px-2.5 py-2 ${COARSE_TOUCH_TARGET} rounded-md text-left cursor-pointer border-none transition-colors ${
          active ? 'bg-accent-subtle text-accent' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
        } ${item.dimmed ? 'opacity-60' : ''}`}
      >
        {item.icon && (
          // w-5 fits both small lucide glyphs (centered) and 20px brand logos
          // without clipping; monochrome glyphs pick up the row's active tint,
          // brand logos carry their own colors.
          <span className={`w-5 h-5 shrink-0 flex items-center justify-center ${active ? 'text-accent' : 'text-muted'}`}>
            {item.icon}
          </span>
        )}
        <span className="flex-1 min-w-0">
          {/* Wraps to two lines rather than truncating: verbose locales inflate
              the longest labels past any fixed rail width. `title` covers the
              pathological case. */}
          <span className="block text-[13px] font-medium line-clamp-2">{item.label}</span>
          {item.summary}
        </span>
        {!twoPane && <ChevronRight size={14} className="text-muted shrink-0" />}
      </button>
    )
    return twoPane ? btn : <div key={item.key} role="listitem">{btn}</div>
  }

  const list = (
    <nav className={twoPane ? 'shrink-0' : 'w-full'} style={twoPane ? { width: railWidth } : undefined}>
      <div className="flex flex-col gap-0.5" role={twoPane ? 'listbox' : 'list'} aria-label={listLabel}>
        {grouped.map(({ group, items: groupItems }, gi) =>
          group ? (
            // `group` is a valid owned child of listbox only; inside role=list
            // it would break the list -> listitem ownership chain, so the
            // narrow branch keeps a role-less (generic, AT-transparent)
            // wrapper and lets the visible header carry the grouping.
            <div key={group} role={twoPane ? 'group' : undefined} aria-label={twoPane ? group : undefined}>
              <div className="text-[11px] text-muted uppercase tracking-wider font-medium px-2.5 pt-2.5 pb-1 select-none">
                {group}
              </div>
              {groupItems.map(row)}
            </div>
          ) : (
            <React.Fragment key={`g${gi}`}>{groupItems.map(row)}</React.Fragment>
          ),
        )}
      </div>
    </nav>
  )

  // Both responsive modes render the SAME child slots in the same order
  // (list?, back-button?, pane-wrapper) so React reconciles the pane wrapper
  // by position and the detail pane is NEVER remounted by a width transition.
  // Only changing the selected key remounts a keyed pane, which is intended.
  return (
    <div ref={containerRef}>
      {/* Banner position tracks the mode: above everything at rest, but BELOW
        * the back bar when narrow mode has drilled in — the back control owns
        * the top-left at every level of the mobile stack. Slots stay
        * unconditional in structure ({cond && x} still occupies a child
        * position) so the pane wrapper keeps its stable index. */}
      {(twoPane || !effectiveKey) && banner}
      <div className={twoPane ? 'flex gap-6 items-start' : 'flex flex-col pb-[env(safe-area-inset-bottom)]'}>
        {(twoPane || !effectiveKey) && list}
        {!twoPane && effectiveKey && (
          // Bleed matches the MOBILE pane's px-4 pt-1 only: the narrow branch
          // is container-width-driven and also fires in a squeezed desktop
          // window whose pane pads px-6 — there the bar renders inset rather
          // than mis-bled by the wrong constant.
          <NavBackBar label={backLabel ?? listLabel} onBack={() => select(null)} className={isMobile ? '-mx-4 -mt-1' : ''} />
        )}
        {!twoPane && effectiveKey && banner}
        <div className={twoPane ? 'flex-1 min-w-0' : 'w-full'}>{children(effectiveKey)}</div>
      </div>
    </div>
  )
}
