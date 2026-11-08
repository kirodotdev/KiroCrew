/**
 * Where the agent sandbox tier (`agent.sandbox`) is changed and how to link to
 * it. Constants only, so Settings → Session Environments can point at the
 * control without pulling the Developer page's config tab into its chunk.
 */
import { AGENT_SANDBOX_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'

/** The Developer page tab the Sandbox select lives on (`buildTabs()` in
 *  DeveloperPage.tsx): Config, its Config summary card. */
const AGENT_SANDBOX_DEVELOPER_TAB = 'config'

/** Route of the Sandbox select: the Developer page opened on its Config tab,
 *  ringing the row through `useSettingHighlight` (which DeveloperPage mounts)
 *  via the `data-setting-key` anchor the row carries. The `/developer` route is
 *  always mounted; only the sidebar entry is behind Developer Mode. */
export const AGENT_SANDBOX_PATH = `/developer?tab=${AGENT_SANDBOX_DEVELOPER_TAB}&highlight=${encodeURIComponent(
  `key:${AGENT_SANDBOX_HIGHLIGHT_ANCHOR}`,
)}`
