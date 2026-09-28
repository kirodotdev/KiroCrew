/**
 * Where the default crewmate is changed and how to link to it. Constants only,
 * so the Crewmates roster can point at the control without pulling the
 * Developer page's config tab into its chunk.
 */
import { DEFAULT_CREWMATE_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'

/** The Developer page tab the row lives on (`buildTabs()` in DeveloperPage.tsx):
 *  Config, the Crewmates table that already badges the default. */
const DEFAULT_CREWMATE_DEVELOPER_TAB = 'config'

/** Route of the Default crewmate select: the Developer page opened on its
 *  Config tab, ringing the row through `useSettingHighlight` (which
 *  DeveloperPage mounts) via the `data-setting-key` anchor the row carries —
 *  it sits below the agents table, so a reader sent here from a roster badge
 *  lands ON it rather than hunting. The `/developer` route is always mounted;
 *  only the sidebar entry is behind Developer Mode. */
export const DEFAULT_CREWMATE_PATH = `/developer?tab=${DEFAULT_CREWMATE_DEVELOPER_TAB}&highlight=${encodeURIComponent(
  `key:${DEFAULT_CREWMATE_HIGHLIGHT_ANCHOR}`,
)}`
