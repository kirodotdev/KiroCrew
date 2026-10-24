import { useLocation } from 'react-router-dom'
import AppPopoutControl from './AppPopoutControl'
import { hasBuiltinComponent } from '../apps/builtinRegistry'
import { appRouteOf } from '../utils/appPopoutRoute'

/**
 * Mounts the pop-out control over whichever APP page the main dashboard is on,
 * and nothing on every other page.
 *
 * Rendered once by the shell as the first child of `<main>`, ahead of the
 * routes, rather than inside each app route element: that is what lets it be a
 * sticky row at the top of the page's scroll container without wrapping the
 * page in a layout box of its own (a wrapper would change the flex parent all
 * twenty-odd builtin pages size themselves against). Which page is an app is
 * read off the route the same way the router resolves it — the builtin
 * component registry for `/<builtin>/*`, the `/apps/:name` arm for installed
 * apps — so a page the router would not render as an app never gets the control.
 */
export default function AppPopoutHost() {
  const { pathname, search } = useLocation()
  const route = appRouteOf(pathname, hasBuiltinComponent)
  if (!route) return null
  // The route is the pop-out's identity; the page and query the user is on are
  // only where its window opens.
  return <AppPopoutControl route={route} initialPath={`${pathname}${search}`} />
}
