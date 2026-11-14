import { i18nT } from '../i18n/t'

/** The readable reason for a per-tool "Always allow" refusal code the server sent
 *  (`dashboard/always_allow_tool.py`), shared by the MCP tool list and the
 *  approval card. '' for no refusal. */
const REFUSAL_KEYS: Record<string, string> = {
  asks_every_time: 'pages.overview.mcpTab.always_allow_refused_asks_every_time',
  set_elsewhere: 'pages.overview.mcpTab.always_allow_refused_set_elsewhere',
  crew_owned: 'pages.overview.mcpTab.always_allow_refused_crew_owned',
}

export function refusalText(code: string | undefined): string {
  if (!code) return ''
  return i18nT(REFUSAL_KEYS[code] ?? 'pages.overview.mcpTab.always_allow_refused_other')
}
