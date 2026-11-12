/** destinationVars — interpolation variables for prose that names where in
 *  the dashboard something lives ("… under {{capabilities}} → {{tab}}").
 *
 *  Rail entries and tab labels are catalog values already; prose that names
 *  them must read the same keys instead of re-spelling the words, or a relabel
 *  silently turns the sentence into a wrong destination again — the drift
 *  class the settings-redirect audit found. Both halves resolve in the active
 *  locale, so every catalog carries only the placeholders.
 */
import { i18nT } from '../i18n/t'

export type CapabilitiesTab = 'knowledge' | 'skills'

const CAPABILITIES_TAB_LABEL_KEY: Record<CapabilitiesTab, string> = {
  knowledge: 'pages.capabilitiesPage.knowledge_label',
  skills: 'pages.capabilitiesPage.skills_label',
}

/** `{{capabilities}} → {{tab}}`: the Agent Capabilities rail entry and one of its tabs. */
export function capabilitiesVars(tab: CapabilitiesTab): { capabilities: string; tab: string } {
  return {
    capabilities: i18nT('nav.agent_capabilities'),
    tab: i18nT(CAPABILITIES_TAB_LABEL_KEY[tab]),
  }
}

/** The same two placeholders for the MCP Servers rail entry and its Servers
 *  view, where an MCP server is added, enabled and checked. */
export function mcpServersVars(): { capabilities: string; tab: string } {
  return {
    capabilities: i18nT('nav.mcp_servers'),
    tab: i18nT('pages.mcpPage.view_servers'),
  }
}
