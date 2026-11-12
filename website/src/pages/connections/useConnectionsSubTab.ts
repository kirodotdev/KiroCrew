import { useEffect, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { safeGetSessionItem, safeSetSessionItem } from '../../utils/safeStorage'

/** The Connections page's two sub-tabs. */
export type ConnectionsSubTab = 'services' | 'mcp-servers'

/** The query param that names the sub-tab. `view` is the sub-view param SkillsTab
 *  already reads on the same Capabilities page, so the page has one convention. */
const SUB_TAB_PARAM = 'view'

/** sessionStorage key for the sub-tab last shown. Session-scoped for the same
 *  reason as SidePanelLayout's tab memory: coming back to Customize inside one
 *  sitting resumes where you were, while a fresh browser tab opens on Services. */
const SUB_TAB_MEMORY_KEY = 'kirocrew:connections-sub-tab'

/** Where a link lands a reader on the MCP Servers list (#21303). One plain
 *  string: an interpolated literal under an ALL-CAPS name trips the i18n lint. */
export const MCP_SERVERS_HREF = '/capabilities?tab=mcp&view=mcp-servers'

function readRemembered(): ConnectionsSubTab {
  return safeGetSessionItem(SUB_TAB_MEMORY_KEY) === 'mcp-servers' ? 'mcp-servers' : 'services'
}

/**
 * The Connections sub-tab, held in the URL so a link such as
 * {@link MCP_SERVERS_HREF} opens MCP Servers and a shared URL reopens the tab it
 * was copied from.
 *
 * `view=mcp-servers` picks MCP Servers outright. Any other URL resumes the
 * sub-tab last shown in this browser tab, Services in a fresh one, so the
 * sidebar's bare `/capabilities` and the `view=budget` a switch from Skills
 * carries over both bring a reader back to the list they left. A resumed MCP
 * Servers is written back into the URL; Services is written as NO param.
 *
 * Every write REPLACES the history entry, as SystemPage's `?plane=` does: the
 * two tabs are a selector, not a stack, so flipping between them must not pile
 * up Back steps.
 */
export function useConnectionsSubTab(): [ConnectionsSubTab, (tab: ConnectionsSubTab) => void] {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const raw = params.get(SUB_TAB_PARAM)
  // In state as well as in sessionStorage, so a param dropped while the page is
  // mounted (the sidebar entry clicked from Customize itself) keeps the tab.
  const [fallback, setFallback] = useState<ConnectionsSubTab>(readRemembered)
  const tab: ConnectionsSubTab = raw === 'mcp-servers' ? 'mcp-servers' : fallback

  // Remember whatever is shown, a deep link included, for the next visit.
  useEffect(() => {
    setFallback(tab)
    safeSetSessionItem(SUB_TAB_MEMORY_KEY, tab)
  }, [tab])

  // Write a resumed MCP Servers back into the URL. Keyed on `params`, not `raw`:
  // SidePanelLayout writes `?tab=` from its own effect in the same flush, built
  // from the older params, so it drops this write and the re-run makes it again.
  // The entry's state rides along: on a phone it is SidePanelLayout's drill-in
  // marker, which is what lets its back bar pop.
  useEffect(() => {
    if (tab !== 'mcp-servers' || raw === 'mcp-servers') return
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.set(SUB_TAB_PARAM, 'mcp-servers')
      return next
    }, { replace: true, state: location.state })
  }, [raw, tab, params, setParams, location.state])

  const setTab = (next: ConnectionsSubTab) => {
    // In the same batched update as the param write: picking Services deletes the
    // param, so a fallback still holding MCP Servers would bring it straight back.
    setFallback(next)
    setParams(prev => {
      const updated = new URLSearchParams(prev)
      if (next === 'mcp-servers') updated.set(SUB_TAB_PARAM, next)
      else updated.delete(SUB_TAB_PARAM)
      return updated
    }, { replace: true })
  }
  return [tab, setTab]
}
