/**
 * Where the default crewmate is changed and how to link to it. Constants only,
 * so the Crewmates roster can point at the control without pulling the
 * Developer panel's config page into its chunk.
 */
import { settingsPath } from '../../components/settingsPath'
import { DEFAULT_CREWMATE_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'

/** The Settings > Developer page the row lives on (`DeveloperPanel.tsx`):
 *  Config, the Crewmates table that already badges the default. */
const DEFAULT_CREWMATE_DEVELOPER_SUB = 'config'

/** Route of the Default crewmate select: Settings > Developer > Config,
 *  ringing the row through `useSettingHighlight` (which SettingsPage mounts)
 *  via the `data-setting-key` anchor the row carries. It sits below the agents
 *  table, so a reader sent here from a roster badge lands ON it rather than
 *  hunting. */
export const DEFAULT_CREWMATE_PATH = settingsPath({
  tab: 'developer',
  sub: DEFAULT_CREWMATE_DEVELOPER_SUB,
  highlight: `key:${DEFAULT_CREWMATE_HIGHLIGHT_ANCHOR}`,
})
