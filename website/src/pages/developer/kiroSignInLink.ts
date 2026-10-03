/**
 * Where the Kiro sign-in card lives and how to link to it. Constants only, so the
 * chat error row and Settings > Overview can point at the card without pulling
 * the card's component graph (the OIDC chooser) into their chunks.
 */
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { settingsPath } from '../../components/settingsPath'

/** The one backend that runs agents as this identity (the gateway's
 *  `ACP_BACKENDS_HOST_AUTH_CALLBACK`). The Agent Harness tab renders the card
 *  only while this id is on offer, and Settings > Overview signposts the card
 *  only while it is the selected backend. */
export const KIRO_SIGN_IN_BACKEND = 'kas'
/** The Settings tab the coding-agent switch and this card live on (`buildTabs()`
 *  in SettingsPage.tsx): the card sits under the switch because the identity it
 *  signs in is used by exactly one backend, KAS, and that switch is where KAS is
 *  chosen. It was the Developer page's `agent-backend` tab, whose old links
 *  DeveloperPage forwards here. */
export const AGENT_HARNESS_SETTINGS_TAB = 'agent'
/** Route of the card: Settings opened on that tab, ringing the card through
 *  `useSettingHighlight` via the `data-setting-key` anchor the card carries --
 *  it sits below a long switch card, so a reader sent here mid-error must land
 *  ON it rather than hunt. Exported so the chat error row that links here and
 *  the page cannot drift apart on the spelling. */
export const KIRO_SIGN_IN_PATH = settingsPath({
  tab: AGENT_HARNESS_SETTINGS_TAB,
  highlight: `key:${KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}`,
})
