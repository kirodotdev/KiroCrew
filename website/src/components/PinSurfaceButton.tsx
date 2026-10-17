/**
 * Promote the sub-item you are looking at onto the left nav rail.
 *
 * Rendered from a page's `headerRight`. It derives its own subject from the
 * URL rather than being told which sub-item is showing, because the URL is
 * already how `SidePanelLayout` addresses a tab — that keeps this out of
 * `SidePanelLayout` entirely, so the four other pages built on that component
 * are byte-for-byte unaffected.
 *
 * Renders NOTHING unless the current URL matches a registered `pinnable`
 * surface. Two consequences worth knowing:
 *   - a page with no promotable sub-items never shows the control, so dropping
 *     it into another `headerRight` is safe and inert until surfaces exist;
 *   - on desktop `SidePanelLayout` expresses its FIRST tab as the ABSENCE of
 *     `?tab=` -- it deletes the param when you select that tab and never writes
 *     it back -- so a bare landing URL is not a transient pre-selection state,
 *     it IS the first tab, permanently. The host therefore passes `defaultTab`
 *     and a missing param resolves to it. An earlier version of this comment
 *     read that absence as "no tab selected YET" and concluded the control
 *     should stay hidden; that shipped as a real gap on the landing view of
 *     /capabilities. A host that passes no `defaultTab` still gets an inert
 *     control, because the fallback has to come from whoever owns the tabs.
 */
import { Pin, PinOff } from 'lucide-react'
import { useLocation, useSearchParams } from 'react-router-dom'

import { i18nT } from '../i18n/t'
import { NAV_PINNED_LIMIT, toggleNavPinned, useNavPinned } from '../lib/navPinned'
import { getPinnableSurfaces, surfaceLabel } from '../surfaces/registry'

/** Split a pinnable surface's `route` into the pathname and tab it addresses. */
function routeParts(route: string): { pathname: string; tab: string | null } {
  const q = route.indexOf('?')
  if (q === -1) return { pathname: route, tab: null }
  return {
    pathname: route.slice(0, q),
    tab: new URLSearchParams(route.slice(q + 1)).get('tab'),
  }
}

export function PinSurfaceButton({ defaultTab }: { defaultTab?: string }) {
  const { pathname } = useLocation()
  const [params] = useSearchParams()
  const pinned = useNavPinned()

  // `SidePanelLayout` expresses "the first tab" as the ABSENCE of `?tab=` on
  // desktop -- it deletes the param when you select that tab (`:295`) and its
  // URL-sync effect never writes it back (`:369`). So the landing view of
  // /capabilities shows Crews with no param at all, and matching on the raw
  // param alone found no surface and hid this control on the single most
  // prominent sub-item. The host passes its own first tab, which is the same
  // array SidePanelLayout derives `first` from, so the two cannot drift.
  const activeTab = params.get('tab') ?? defaultTab ?? null
  const surface = getPinnableSurfaces().find(s => {
    const parts = routeParts(s.route)
    return parts.pathname === pathname && parts.tab === activeTab
  })
  if (!surface) return null

  const isPinned = pinned.has(surface.navId)
  const name = surfaceLabel(surface)
  // At the cap an unpinned row cannot be added, so the control is disabled
  // rather than accepting a click it would silently drop. Unpinning is never
  // refused, so a pinned row's control always stays live.
  const atLimit = !isPinned && pinned.size >= NAV_PINNED_LIMIT
  // A disabled control that still reads "Pin X to the sidebar" promises the very
  // action it refuses, and a blind read of the at-cap frame confirmed a reader
  // takes it for a live pin and clicks. At the cap the label states the reason
  // and the remedy instead.
  const label = atLimit
    ? i18nT('components.pinSurfaceButton.pin_limit_reached', { n: NAV_PINNED_LIMIT })
    : isPinned
      ? i18nT('pages.libraryPage.unpin_from_sidebar', { name })
      : i18nT('pages.libraryPage.pin_to_sidebar', { name })

  return (
    <button
      type="button"
      data-testid="pin-surface-button"
      onClick={() => toggleNavPinned(surface.navId)}
      disabled={atLimit}
      aria-pressed={isPinned}
      aria-label={label}
      title={label}
      className={`w-7 h-7 shrink-0 flex items-center justify-center rounded-md border-none bg-transparent transition-colors ${
        atLimit
          ? 'text-muted opacity-40 cursor-not-allowed'
          : isPinned
            ? 'text-accent hover:bg-bg-hover cursor-pointer'
            : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'
      }`}
    >
      {isPinned ? <PinOff size={15} /> : <Pin size={15} />}
    </button>
  )
}
