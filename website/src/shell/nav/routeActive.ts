import { useMemo } from 'react'

/** The chat route, spelled once. Two things key off it on the phone -- the
 *  single-bar header variant and the shell nav drawer's swipe gate -- and a
 *  drift between two spellings is exactly how "two drawers for one gesture"
 *  would come back. */
export const isChatRoute = (pathname: string) => pathname === '/chat' || pathname.startsWith('/chat/') || pathname === '/'

/**
 * Which rail row is lit, derived from the URL: the Apps namespace split between
 * Discover and Library, the promoted-sub-item rule, and whether the route owns
 * its own scrolling.
 */
export function useRouteActiveModel(pathname: string, search: string, advertisedNavItems: Array<{ path: string }>) {
  const activePath = pathname
  // App Store split (PR1): two sidebar entries share the /apps namespace.
  //  - Library owns /apps/library and everything under it.
  //  - Discover owns the store root plus the detail/migrate flows — both are
  //    storefront surfaces reached from Discover cards, not installed-app UI.
  //  - Installed-app pages (/apps/:name) highlight NEITHER entry: each
  //    installed app has its own rail row (sortedAppGroup, prefix
  //    match), and before the split the store entry already used an exact
  //    `=== '/apps'` match, so an app page never lit the store link. Keeping
  //    that mapping means exactly one row lights at a time.
  const libraryNavActive = activePath === '/apps/library' || activePath.startsWith('/apps/library/')
  const discoverNavActive = activePath === '/apps' || activePath.startsWith('/apps/-/') || activePath.startsWith('/apps/detail/') || activePath.startsWith('/apps/migrate/')
  const isChat = isChatRoute(activePath)
  // /webhooks is a full-height rail-and-detail shell (like /capabilities), so it
  // owns its own scrolling and must not sit inside <main>'s scroll container.
  const needsFixedHeight = isChat || activePath === '/settings' || activePath.startsWith('/settings/') || activePath === '/developer' || activePath === '/capabilities' || activePath === '/webhooks'
  // Which rail row is the current one. A promoted sub-item's `path` carries its
  // host panel's tab param (`/capabilities?tab=steering`), so the pathname-only
  // comparison every other row uses can never match it and the row would never
  // paint as active while you were standing on it. Rows WITHOUT a param take
  // the original test unchanged — including `/apps`, which must not match its
  // own children the way the general prefix test would.
  // A promoted sub-item row and its HOST row would otherwise both pass their own
  // active test on the same URL: the host's is a prefix match on `/capabilities`,
  // the promoted row's an exact `?tab=` match. The rail then paints two rows as
  // "where I am", which answers the question with neither. Screenshot evidence
  // is what caught it, so the host yields to the promoted row that owns the tab.
  const promotedTabOwner = useMemo(() => {
    const tab = new URLSearchParams(search).get('tab')
    if (!tab) return null
    const owner = advertisedNavItems.find(n => {
      const q = n.path.indexOf('?')
      return q !== -1
        && n.path.slice(0, q) === activePath
        && new URLSearchParams(n.path.slice(q + 1)).get('tab') === tab
    })
    return owner ? activePath : null
  }, [advertisedNavItems, activePath, search])

  const navRowActive = (path: string): boolean => {
    const q = path.indexOf('?')
    if (q !== -1) {
      const wanted = new URLSearchParams(path.slice(q + 1)).get('tab')
      return activePath === path.slice(0, q)
        && new URLSearchParams(search).get('tab') === wanted
    }
    if (path === '/apps') return activePath === '/apps'
    const selfActive = activePath === path || activePath.startsWith(path + '/')
    // Only the host of a currently-showing promoted row yields, so every other
    // param-free row keeps its original behaviour byte for byte.
    return selfActive && promotedTabOwner === path ? false : selfActive
  }
  return { activePath, libraryNavActive, discoverNavActive, isChat, needsFixedHeight, navRowActive }
}
