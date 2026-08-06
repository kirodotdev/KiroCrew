import React from 'react'
import { useSearchParams } from 'react-router-dom'
import { useIsMobile } from '../hooks/useIsMobile'
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
  /** When true, content area uses overflow-hidden + flex layout for Virtuoso/fixed-height children */
  fixedContent?: boolean
  children: (activeTab: string) => React.ReactNode
}

/** sessionStorage namespace for the per-page remembered tab. Session-scoped on
 *  purpose: returning to a page inside one sitting should resume where you
 *  left off, but a fresh launch should open on the page's own first tab rather
 *  than somewhere you were days ago. */
const TAB_MEMORY_PREFIX = 'kirocrew:sidepanel-tab:'

export default function SidePanelLayout({ title, tabs, defaultTab, rememberKey, footer, headerRight, fixedContent, children }: SidePanelLayoutProps) {
  const [params, setParams] = useSearchParams()
  const isMobile = useIsMobile()
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
  const setTab = (t: string) => {
    // Synchronously, in the same batched update as the param write: picking the
    // FIRST tab deletes the param, so a fallback still holding the previous tab
    // would render it for a frame AND get re-written into the URL by the sync
    // effect below — silently undoing the click.
    if (rememberKey) setFallbackTab(t)
    setParams(prev => {
      const next = new URLSearchParams(prev)
      if (t === first) next.delete('tab')
      else next.set('tab', t)
      return next
    }, { replace: true })
  }
  const meta = tabs.find(t => t.key === tab)

  // Keep the URL in step with the shown tab, so the address bar stays
  // copy-pasteable — including after an in-place param drop. Keyed on the
  // resolved tab rather than mount-only, and it cannot loop: writing the param
  // makes `rawTab` truthy, which short-circuits the next run. `tab === first`
  // writes nothing, matching `setTab`'s convention that the first tab is the
  // param-less state.
  //
  // Deliberately a passive effect, NOT useLayoutEffect: react-router 7 drops
  // navigations fired from a layout effect during the initial mount (its ready
  // flag is set in a passive effect) — see the same note on SettingsPage's
  // legacy tab remap.
  React.useEffect(() => {
    if (!rememberKey || rawTab || !tab || tab === first) return
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('tab', tab)
      return next
    }, { replace: true })
  }, [rememberKey, rawTab, tab, first, setParams])

  // Remember the tab that is effectively shown — in component state, so an
  // in-place param drop has something to fall back to, and in sessionStorage,
  // so a later visit restores it. Keying off the shown tab (not just an
  // explicit click) means a deep link (command palette, docs link) is
  // remembered too.
  React.useEffect(() => {
    if (!rememberKey || !tab) return
    setFallbackTab(tab)
    safeSetSessionItem(TAB_MEMORY_PREFIX + rememberKey, tab)
  }, [rememberKey, tab])

  return (
    <div className={`flex-1 min-h-0 flex overflow-hidden ${isMobile ? 'flex-col' : ''}`}>
      {isMobile ? (
        <div className="shrink-0 border-b border-border bg-bg px-4 pt-3 pb-0">
          <div className="flex items-center justify-between mb-2">
            <div className="text-lg font-bold text-text-strong">{title}</div>
            {headerRight}
          </div>
          <div className="flex gap-1 overflow-x-auto scrollbar-none pb-2">
            {tabs.map(t => (
              <button
                key={t.key}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium cursor-pointer border-none whitespace-nowrap transition-all ${
                  tab === t.key
                    ? 'bg-accent-subtle text-accent'
                    : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
                }`}
                onClick={() => setTab(t.key)}
              >
                <span className="w-3.5 h-3.5 shrink-0 flex items-center justify-center">{t.icon}</span>
                {t.label}
                {t.dot && <span className="w-1.5 h-1.5 bg-accent rounded-full shrink-0" role="status" aria-label={i18nT('components.sidePanelLayout.update_available')} />}
              </button>
            ))}
          </div>
          {footer && <div className="pt-2 pb-2">{footer}</div>}
        </div>
      ) : (
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
      )}

      <div className={`flex-1 min-w-0 min-h-0 flex flex-col ${fixedContent ? 'overflow-hidden' : 'overflow-y-auto'}`}>
        {!isMobile && (
        <div className="flex items-end justify-between gap-4 px-6 pt-2 pb-3 shrink-0">
          <div>
            <div className="text-2xl font-bold tracking-tight text-text-strong">{meta?.label || ''}</div>
            {meta?.description && <div className="text-muted text-sm mt-1">{meta.description}</div>}
          </div>
          {headerRight}
        </div>
        )}
        <div className={`${isMobile ? 'px-4' : 'px-6'} ${fixedContent ? 'flex-1 min-h-0 flex flex-col' : 'flex-1 pb-8'}`}>
          {children(tab)}
        </div>
      </div>
    </div>
  )
}
