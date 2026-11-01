import { useEffect } from 'react'
import { matchPath, useLocation, useNavigate } from 'react-router-dom'
import { settingsPath } from '../components/settingsPath'
import { AGENT_HARNESS_SETTINGS_TAB } from './developer/kiroSignInLink'
import { DEVELOPER_FEATURE_PREVIEWS_SUB, DEVELOPER_SUBS } from './settings/developerSubs'
import { FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR } from './settings/FeaturePreviewsSection'

/** The tab key the coding-agent switch had on this route. It lives in
 *  Settings > Agent Harness. */
const LEGACY_AGENT_BACKEND_TAB = 'agent-backend'

/**
 * `/developer?tab=<key>` is a redirect into Settings > Developer: the old
 * route survives in bookmarks, docs, curated tips and command-palette
 * history. `?tab=` becomes the SubNav page segment and every other query
 * param (`highlight`, System's `plane`) rides along, so a deep link lands on
 * the same control. An unknown or absent tab lands on the panel's first page.
 * A REPLACE navigation, so back never returns to the redirecting URL. Passive
 * useEffect on purpose: react-router 7 drops navigations fired from
 * useLayoutEffect on first mount.
 */
export default function DeveloperPage() {
  const navigate = useNavigate()
  const { pathname, search } = useLocation()

  useEffect(() => {
    // Only the old route redirects; once the navigation lands, this
    // component may still be mounted for a frame and must not re-translate.
    // Same matching as the route, so `/developer/` and `/Developer` redirect too.
    if (!matchPath({ path: '/developer', end: true }, pathname)) return
    const qs = new URLSearchParams(search)
    const tab = qs.get('tab')
    qs.delete('tab')
    if (tab === LEGACY_AGENT_BACKEND_TAB) {
      const highlight = qs.get('highlight') ?? undefined
      navigate(settingsPath({ tab: AGENT_HARNESS_SETTINGS_TAB, highlight }), { replace: true })
      return
    }
    const sub = tab && DEVELOPER_SUBS.includes(tab) ? tab : null
    // The old Feature Previews link rings the whole section it names.
    const highlight = qs.get('highlight')
      ?? (sub === DEVELOPER_FEATURE_PREVIEWS_SUB ? `key:${FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR}` : undefined)
    qs.delete('highlight')
    const params = Object.fromEntries(qs.entries())
    navigate(settingsPath({ tab: 'developer', sub, params, highlight }), { replace: true })
  }, [pathname, search, navigate])

  return null
}
