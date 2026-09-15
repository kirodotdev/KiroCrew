/**
 * Where the Kiro sign-in card lives and how to link to it. Constants only, so the
 * chat error row and Settings > Overview can point at the card without pulling
 * the card's component graph (the OIDC chooser) into their chunks.
 */
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'

/** The one backend that runs agents as this identity (the gateway's
 *  `ACP_BACKENDS_HOST_AUTH_CALLBACK`). The Agent Backend tab renders the card
 *  only while this id is on offer, and Settings > Overview signposts the card
 *  only while it is the selected backend. */
export const KIRO_SIGN_IN_BACKEND = 'kas'
/** The Developer page tab the card lives on (`buildTabs()` in DeveloperPage.tsx):
 *  the Agent Backend switch, because the identity this card signs in is used by
 *  exactly one backend, KAS, and that switch is where KAS is chosen. */
const KIRO_SIGN_IN_DEVELOPER_TAB = 'agent-backend'
/** Route of the card: the Developer page opened on that tab, ringing the card
 *  through `useSettingHighlight` (which DeveloperPage mounts) via the
 *  `data-setting-key` anchor the card carries -- it sits below a long switch
 *  card, so a reader sent here mid-error must land ON it rather than hunt.
 *  Exported so the chat error row that links here and the page cannot drift
 *  apart on the spelling. The `/developer` route is always mounted; only the
 *  sidebar entry is behind Developer Mode, and a user running KAS turned that
 *  on to pick it. */
export const KIRO_SIGN_IN_PATH = `/developer?tab=${KIRO_SIGN_IN_DEVELOPER_TAB}&highlight=${encodeURIComponent(
  `key:${KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}`,
)}`
