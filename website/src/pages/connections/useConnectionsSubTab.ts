import { useSearchParams } from 'react-router-dom'

/** The Connections page's two sub-tabs. */
export type ConnectionsSubTab = 'services' | 'mcp-servers'

/** The query param that names the sub-tab. `view` is the sub-view param SkillsTab
 *  already reads on the same Capabilities page, so the page has one convention. */
const SUB_TAB_PARAM = 'view'

/** Where a link lands a reader on the MCP Servers list (#21303). One plain
 *  string: an interpolated literal under an ALL-CAPS name trips the i18n lint. */
export const MCP_SERVERS_HREF = '/capabilities?tab=mcp&view=mcp-servers'

/**
 * The Connections sub-tab, held in the URL instead of component state, so a link
 * such as {@link MCP_SERVERS_HREF} opens MCP Servers and a shared URL reopens the
 * tab it was copied from.
 *
 * Services is the default and is written as NO param: an absent or unknown value
 * reads as Services, and switching to Services deletes the param. A switch
 * REPLACES the history entry, as SystemPage's `?plane=` does: the two tabs are a
 * selector, not a stack, so flipping between them must not pile up Back steps.
 */
export function useConnectionsSubTab(): [ConnectionsSubTab, (tab: ConnectionsSubTab) => void] {
  const [params, setParams] = useSearchParams()
  const tab: ConnectionsSubTab = params.get(SUB_TAB_PARAM) === 'mcp-servers' ? 'mcp-servers' : 'services'
  const setTab = (next: ConnectionsSubTab) => setParams(prev => {
    const updated = new URLSearchParams(prev)
    if (next === 'mcp-servers') updated.set(SUB_TAB_PARAM, next)
    else updated.delete(SUB_TAB_PARAM)
    return updated
  }, { replace: true })
  return [tab, setTab]
}
