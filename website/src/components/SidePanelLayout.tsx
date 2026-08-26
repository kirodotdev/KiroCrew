import React from 'react'
import { useSearchParams, useLocation, useNavigate } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { NavBackBar } from './NavBackBar'
import { hasSubSelection, deleteSubSelection, COARSE_TOUCH_TARGET, SUBNAV_PUSH_STATE } from './subNavParams'
import { useIsMobile } from '../hooks/useIsMobile'
import { useVisualViewport } from '../hooks/useVisualViewport'
import { safeGetSessionItem, safeSetSessionItem } from '../utils/safeStorage'

import { i18nT } from '../i18n/t'
export interface SidePanelTab {
  key: string
  label: string
  icon: React.ReactNode
  description?: string
  /** Presence dot after the label (e.g. About while an update is available). */
  dot?: boolean
  /** Optional group label. Desktop nav renders an uppercase header above the
   *  first tab of each new group; tabs without a group render header-less.
   *  Mobile ignores groups (flat pill row). */
  group?: string
  /** Render a divider above this tab in the desktop nav (e.g. before About). */
  dividerBefore?: boolean
  /** Contain THIS tab's pane to the viewport instead of letting the page grow:
   *  the tab's own header stays put and the pane's `overflow-y-auto` children
   *  do the scrolling. Per-tab because a page can legitimately mix the two —
   *  Settings is one long scrolling form on sixteen tabs and a two-column
   *  archive with its own rail on one, and a page-wide switch would have to
   *  break one of them. The page-level `fixedContent` prop still forces it for
   *  every tab. */
  fixedContent?: boolean
  /** THIS tab's pane hosts a SettingsSubNav, so a second-level selection param
   *  (?sub= or a legacy alias) means a deeper level is showing its own back
   *  bar and the shell's chrome must step aside. Opt-in per tab: without it,
   *  `channel`/`section` would be globally reserved words for every
   *  SidePanelLayout consumer (Developer, Capabilities, Schedule) — a page
   *  adding an unrelated ?section= param would silently lose its mobile
   *  chrome, and nothing on that page would flag it. */
  hostsSubNav?: boolean
}

interface SidePanelLayoutProps {
  title: string
  tabs: readonly SidePanelTab[]
  defaultTab?: string
  /** Stable id under which this page's last visited tab is remembered for the
   *  rest of the browser session, so navigating away and back returns to it
   *  instead of snapping to the first tab. Omit to disable remembering.
   *  Must NOT be localized — it is a storage key, not a label. */
  rememberKey?: string
  footer?: React.ReactNode
  headerRight?: React.ReactNode
  /** Where the mobile layout docks `headerRight`. 'header' (default) keeps it
   *  in the title rows of BOTH levels — right for action buttons (e.g.
   *  Capabilities' Restart), which must stay reachable inside a tab.
   *  'bottom-float' renders it ONLY on the root list, inside the iOS-26-style
   *  floating glass capsule — right for a search field whose results
   *  deep-link anywhere (Settings opts in). Desktop ignores this. */
  headerRightDock?: 'header' | 'bottom-float'
  /** When true, content area uses overflow-hidden + flex layout for Virtuoso/fixed-height children */
  fixedContent?: boolean
  children: (activeTab: string) => React.ReactNode
}

/** sessionStorage namespace for the per-page remembered tab. Session-scoped on
 *  purpose: returning to a page inside one sitting should resume where you
 *  left off, but a fresh launch should open on the page's own first tab rather
 *  than somewhere you were days ago. */
/** How the host is presenting a `headerRight` control. 'bottom-float' is the
 *  mobile root list's iOS-26-style floating bottom capsule: the control should
 *  render full-width, chrome-less (the capsule owns the border/blur), and open
 *  any dropdown UPWARD — at the bottom of the screen a downward panel is
 *  off-screen. */
export const SidePanelDockContext = React.createContext<'header' | 'bottom-float'>('header')

const TAB_MEMORY_PREFIX = 'kirocrew:sidepanel-tab:'

export default function SidePanelLayout({ title, tabs, defaultTab, rememberKey, footer, headerRight, headerRightDock = 'header', fixedContent, children }: SidePanelLayoutProps) {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  // Keyboard avoidance for the bottom-float dock. iOS Safari shrinks only the
  // VISUAL viewport when the keyboard opens (the layout viewport `fixed`
  // resolves against keeps its height — see CommandPalette.tsx), so a
  // bottom-anchored capsule would sit behind the keyboard while its upward
  // results panel is exactly what the user is trying to read. Lift it by the
  // hidden gap. 0 on desktop and whenever no keyboard is up.
  const vv = useVisualViewport()
  const keyboardInset =
    typeof window === 'undefined' ? 0 : Math.max(0, window.innerHeight - vv.offsetTop - vv.height)
  const rawTab = params.get('tab')
  const first = defaultTab || tabs[0]?.key || ''

  // Read the remembered tab ONCE, before any effect can overwrite it. Reading
  // it lazily inside an effect instead would race the persist effect below,
  // which fires on the same mount with the not-yet-restored tab.
  const [remembered] = React.useState(() => (rememberKey ? safeGetSessionItem(TAB_MEMORY_PREFIX + rememberKey) : null))

  // The tab to show whenever the URL carries no `?tab=`. Seeded from the
  // remembered tab DURING THE FIRST RENDER, so the remembered pane is what
  // actually paints — restoring from an effect instead would mount the first
  // tab's pane for a frame (a visible flash, and real wasted work when that
  // pane fetches: Overview loads memory + usage metrics).
  //
  // It stays in step with whatever is shown, rather than being a one-shot,
  // because the param can vanish while this component is still MOUNTED: ⌘+,
  // runs `navigate('/settings')` and the sidebar entry is that same route, so
  // an already-open page keeps its layout alive and simply loses its param. A
  // one-shot restore fell back to the first tab there — snapping the pane to
  // Overview and letting the persist effect below overwrite the stored tab
  // with `overview`, destroying the very preference this exists to keep.
  const [fallbackTab, setFallbackTab] = React.useState<string | null>(() =>
    rememberKey && !rawTab && remembered && tabs.some(t => t.key === remembered) ? remembered : null,
  )

  const tab = rawTab && tabs.some(t => t.key === rawTab) ? rawTab : (fallbackTab || first)
  // Mobile is a two-level iOS-style navigation: NO explicit ?tab= means the
  // ROOT LIST (all tabs, grouped, tap to drill), an explicit one means the
  // drilled-in detail. The remembered tab deliberately does NOT auto-drill on
  // mobile — iOS Settings always opens at its root, and a phone visit that
  // teleports into last week's tab reads as being lost, not resumed.
  const mobileTab = rawTab && tabs.some(t => t.key === rawTab) ? rawTab : null
  const setTab = (t: string) => {
    // Synchronously, in the same batched update as the param write: picking the
    // FIRST tab deletes the param, so a fallback still holding the previous tab
    // would render it for a frame AND get re-written into the URL by the sync
    // effect below — silently undoing the click.
    if (rememberKey) setFallbackTab(t)
    setParams(prev => {
      const next = new URLSearchParams(prev)
      // A second-level selection is scoped to the tab that hosts it. One that
      // rides across a tab change strands a phone view whose new tab hosts no
      // SubNav: the chrome yields to a back bar that never renders.
      deleteSubSelection(next)
      // Mobile always writes the param explicitly — the param-less state IS the
      // root list there, so the desktop convention (first tab = no param) would
      // make the first tab unreachable.
      if (t === first && !isMobile) next.delete('tab')
      else next.set('tab', t)
      return next
      // Mobile drill-in is a PUSH (a real history entry), so the platform back
      // gesture pops to the root list the way an iOS stack does; desktop tab
      // switching stays replace — the rail is a selector, not a stack. The
      // state marker is what lets the back control POP this entry instead of
      // writing a duplicate on top of it.
    }, { replace: !isMobile, state: isMobile ? { [SUBNAV_PUSH_STATE]: true } : undefined })
  }
  /** Mobile back: return to the root list. If THIS stack pushed the current
   *  entry, pop it — a replace-write here would leave [root, root] twins in
   *  history and the next platform back-swipe would visibly do nothing. The
   *  replace path remains for entries we did not mint (cold deep links),
   *  where `history.back()` would exit the app. */
  const backToRoot = () => {
    if ((location.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]) {
      navigate(-1)
      return
    }
    setParams(prev => {
      const next = new URLSearchParams(prev)
      deleteSubSelection(next)
      next.delete('tab')
      return next
    }, { replace: true })
  }
  const meta = tabs.find(t => t.key === tab)

  // Adjacent tabs sharing a `group` render under one header in the mobile
  // root list (order in `tabs` drives everything, same contract as the
  // desktop rail's header rendering).
  const groupedTabs = tabs.reduce<{ group: string | undefined; items: SidePanelTab[] }[]>((acc, t) => {
    const last = acc[acc.length - 1]
    if (last && last.group === t.group) last.items.push(t)
    else acc.push({ group: t.group, items: [t] })
    return acc
  }, [])

  // Whether the shown pane is contained rather than page-scrolled. The
  // page-level prop is unconditional; the per-tab flag is honoured on desktop
  // only, because it exists for panes that put a fixed rail beside a scrolling
  // detail column and the mobile layout has no width for that pane to keep. On
  // a phone the rail and the detail sit in ~150px each, so containing them
  // would hand the reader two thumb-sized scrollers where one page scroll works.
  const fixed = !!fixedContent || (!isMobile && !!meta?.fixedContent)

  // Keep the URL in step with the shown tab, so the address bar stays
  // copy-pasteable — including after an in-place param drop. Keyed on the
  // resolved tab rather than mount-only, and it cannot loop: writing the param
  // makes `rawTab` truthy, which short-circuits the next run. `tab === first`
  // writes nothing, matching `setTab`'s convention that the first tab is the
  // param-less state. Desktop-only: on mobile the param-less state is the
  // ROOT LIST, and this write would silently teleport it into the
  // remembered tab.
  //
  // Deliberately a passive effect, NOT useLayoutEffect: react-router 7 drops
  // navigations fired from a layout effect during the initial mount (its ready
  // flag is set in a passive effect) — see the same note on SettingsPage's
  // legacy tab remap.
  React.useEffect(() => {
    if (isMobile || !rememberKey || rawTab || !tab || tab === first) return
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('tab', tab)
      return next
    }, { replace: true })
  }, [isMobile, rememberKey, rawTab, tab, first, setParams])

  // Remember the tab that is effectively shown — in component state, so an
  // in-place param drop has something to fall back to, and in sessionStorage,
  // so a later visit restores it. Keying off the shown tab (not just an
  // explicit click) means a deep link (command palette, docs link) is
  // remembered too. On mobile only an EXPLICIT drill-in is remembered:
  // at the root list `tab` merely resolves to the first tab, and persisting
  // that would overwrite the desktop preference with 'overview' on every
  // phone visit.
  React.useEffect(() => {
    if (!rememberKey || !tab) return
    if (isMobile && !rawTab) return
    setFallbackTab(tab)
    safeSetSessionItem(TAB_MEMORY_PREFIX + rememberKey, tab)
  }, [rememberKey, tab, isMobile, rawTab])

  // ── Mobile: iOS-style two-level navigation ──
  // Root (no ?tab=): the page title + a grouped vertical list of every tab,
  // each row an icon + label + chevron. Drilled in (?tab=<key>): a sticky
  // accent back bar ("‹ Settings") over the tab's own header and pane. The
  // horizontal pill strip this replaces hid fifteen of nineteen tabs behind a
  // scroll; a vertical root list shows the whole map, the way iOS Settings does.
  if (isMobile) {
    if (!mobileTab) {
      return (
        // pb-24 on the SCROLL CONTAINER (below the footer, not on the list):
        // clearance for the fixed search capsule must protect the LAST in-flow
        // element, and the version footer renders after the list.
        <div className="flex-1 min-h-0 overflow-y-auto px-4 pb-24 relative">
          <div className="flex items-center justify-between gap-3 pt-3 pb-1">
            <div className="text-2xl font-bold tracking-tight text-text-strong">{title}</div>
            {headerRight && headerRightDock === 'header' && headerRight}
          </div>
          {/* role=list, not listbox: these rows NAVIGATE (push a level), they
            * are not a selection — and a listbox may contain only options and
            * groups, which the separators and headers here are not. Groups
            * carry the header text as their accessible name; the visual header
            * stays aria-hidden so it is not announced twice. */}
          <div role="list" aria-label={title} className="flex flex-col gap-0.5 pb-2">
            {groupedTabs.map(({ group, items }, gi) => {
              const rows = items.map(t => (
                <div key={t.key} role="listitem">
                  {t.dividerBefore && <div className="h-px bg-border mx-2.5 my-2" role="separator" />}
                  <button
                    className={`flex items-center gap-2.5 w-full px-2.5 py-2.5 ${COARSE_TOUCH_TARGET} rounded-md text-[14px] text-left font-medium cursor-pointer border-none bg-transparent text-text transition-colors hover:bg-bg-hover`}
                    onClick={() => setTab(t.key)}
                  >
                    <span className="w-5 h-5 shrink-0 flex items-center justify-center text-muted">{t.icon}</span>
                    <span className="flex-1 min-w-0 truncate">{t.label}</span>
                    {t.dot && <span className="w-2 h-2 bg-accent rounded-full shrink-0" role="status" aria-label={i18nT('components.sidePanelLayout.update_available')} />}
                    <ChevronRight size={15} className="text-muted-strong shrink-0" />
                  </button>
                </div>
              ))
              return group ? (
                <div key={group} role="group" aria-label={group}>
                  <div className="text-[11px] text-muted uppercase tracking-wider font-medium px-2.5 pt-3 pb-1 select-none" aria-hidden="true">
                    {group}
                  </div>
                  {rows}
                </div>
              ) : (
                <React.Fragment key={`g${gi}`}>{rows}</React.Fragment>
              )
            })}
          </div>
          {footer && <div className="pb-4">{footer}</div>}
          {/* iOS-26-style floating bottom search: a glass capsule pinned above
            * the home-indicator area with the safe-area utility family (the
            * guard test keys on `*-safe*` — a hand-rolled env() spelling is
            * invisible to it, and left-0/right-0 would sit under a landscape
            * notch). pointer-events split so the empty gutter around the
            * capsule stays scrollable. */}
          {headerRight && headerRightDock === 'bottom-float' && (
            <div
              className="fixed bottom-safe-or-[14px] left-safe right-safe z-20 px-5 pointer-events-none"
              // Translate, not `bottom`: the safe-area class must stay the
              // at-rest anchor (safeArea.guard.test pins the *-safe* family),
              // and the transform composes with it only while a keyboard
              // occludes the visual viewport.
              style={keyboardInset > 0 ? { transform: `translateY(-${keyboardInset}px)` } : undefined}
            >
              <div className="pointer-events-auto mx-auto max-w-sm rounded-full border border-border shadow-lg backdrop-blur-xl bg-[color-mix(in_srgb,var(--bg-elevated)_92%,transparent)]">
                <SidePanelDockContext.Provider value="bottom-float">
                  {headerRight}
                </SidePanelDockContext.Provider>
              </div>
            </div>
          )}
        </div>
      )
    }
    // iOS push-stack semantics: ONE back button per level, pointing one level
    // up. When a pane's own SubNav has drilled a further level in, THIS
    // level's chrome — the "‹ Settings" bar and the tab's big title — steps
    // aside entirely, leaving the SubNav's "‹ Channels" bar as the only
    // navigation. Two stacked back bars is exactly the misread a stack exists
    // to prevent. The level test honours the legacy aliases too: old bookmarks
    // still carry ?channel=/?section=, and reading only the canonical name
    // would stack the bars on exactly those links. Gated on the tab's own
    // hostsSubNav declaration: chrome yields only where a SubNav exists to
    // replace it — on any other tab a stray selection param must NOT strand
    // the pane without navigation.
    const subDrilled = !!meta?.hostsSubNav && hasSubSelection(params)
    return (
      <div className={`flex-1 min-w-0 min-h-0 flex flex-col ${fixed ? 'overflow-hidden' : 'overflow-y-auto'}`}>
        {!subDrilled && <NavBackBar label={title} onBack={backToRoot} />}
        {!subDrilled && (
        <div data-testid="mobile-detail-header" className="flex items-end justify-between gap-4 px-4 pb-2 shrink-0">
          <div>
            <div className="text-2xl font-bold tracking-tight text-text-strong">{meta?.label || ''}</div>
            {meta?.description && <div className="text-muted text-sm mt-1">{meta.description}</div>}
          </div>
          {/* header-docked controls (e.g. Capabilities' Restart) stay reachable
            * inside a tab; a bottom-float search lives on the root only —
            * its results deep-link anywhere, so no per-tab copy is needed. */}
          {headerRight && headerRightDock === 'header' && headerRight}
        </div>
        )}
        <div data-testid="side-panel-pane" className={`px-4 pt-1 ${fixed ? 'flex-1 min-h-0 flex flex-col' : 'flex-1 pb-8'}`}>
          {children(tab)}
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 min-h-0 flex overflow-hidden">
      <nav className="w-[200px] shrink-0 border-r border-border bg-bg overflow-y-auto pt-1 pb-3 px-3 flex flex-col gap-0.5">
          <div className="text-lg font-bold text-text-strong px-2.5 py-2 mb-1">{title}</div>
          {tabs.map((t, i) => (
            <React.Fragment key={t.key}>
              {t.dividerBefore && <div className="h-px bg-border mx-2.5 my-2" role="separator" />}
              {t.group && tabs[i - 1]?.group !== t.group && (
                <div className="text-[11px] text-muted uppercase tracking-wider font-medium px-2.5 pt-2.5 pb-1 select-none" aria-hidden="true">
                  {t.group}
                </div>
              )}
              <button
                className={`flex items-center gap-2.5 w-full px-2.5 py-2 rounded-md text-[13px] text-left font-medium cursor-pointer border-none transition-all ${
                  tab === t.key
                    ? 'bg-accent-subtle text-accent'
                    : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
                }`}
                onClick={() => setTab(t.key)}
              >
                <span className={`w-4 h-4 shrink-0 flex items-center justify-center ${tab === t.key ? 'text-accent' : 'text-muted'}`}>
                  {t.icon}
                </span>
                {t.label}
                {t.dot && <span className="ml-auto w-2 h-2 bg-accent rounded-full shrink-0" role="status" aria-label={i18nT('components.sidePanelLayout.update_available')} />}
              </button>
            </React.Fragment>
          ))}
          {footer && <div className="mt-auto pt-3 px-2.5">{footer}</div>}
        </nav>

      <div className={`flex-1 min-w-0 min-h-0 flex flex-col ${fixed ? 'overflow-hidden' : 'overflow-y-auto'}`}>
        <div data-testid="side-panel-header" className="flex items-end justify-between gap-4 px-6 pt-2 pb-3 shrink-0">
          <div>
            <div className="text-2xl font-bold tracking-tight text-text-strong">{meta?.label || ''}</div>
            {meta?.description && <div className="text-muted text-sm mt-1">{meta.description}</div>}
          </div>
          {headerRight}
        </div>
        <div data-testid="side-panel-pane" className={`px-6 ${fixed ? 'flex-1 min-h-0 flex flex-col' : 'flex-1 pb-8'}`}>
          {children(tab)}
        </div>
      </div>
    </div>
  )
}
