/**
 * Where the Kiro sign-in card lives and how to link to it. Constants only, so the
 * chat error row and Settings > Overview can point at the card without pulling
 * the card's component graph (the OIDC chooser) into their chunks.
 */
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { settingsPath } from '../../components/settingsPath'
import { KIRO_SIGN_IN_SETTING_ID, LEGACY_KIRO_SIGN_IN_SETTING_ID } from '../../components/commandPalette/kiroSignInSettingId'

/** The backend whose presence in the build's `agent.acp_backend` choices says the
 *  build offers Kiro sign-in (the gateway's `ACP_BACKENDS_HOST_AUTH_CALLBACK`). */
export const KIRO_SIGN_IN_BACKEND = 'kas'
/** The Settings tab the coding-agent switch lives on (`buildTabs()` in
 *  SettingsPage.tsx). It was the Developer page's `agent-backend` tab, whose old
 *  links DeveloperPage forwards here. */
export const AGENT_HARNESS_SETTINGS_TAB = 'agent'
/** The Settings tab the Kiro sign-in card lives on. Its own tab, not a part of
 *  the harness switch: the sign-in is used by more than the agent harness
 *  (Team Crews, Shared Crews, the credits pill), so it is reachable whichever
 *  harness runs. */
export const KIRO_ACCOUNT_SETTINGS_TAB = 'kiro-account'
/** Route of the card: Settings opened on its tab, ringing the card through
 *  `useSettingHighlight` via the `data-setting-key` anchor the card carries.
 *  Exported so the chat error row that links here and the page cannot drift
 *  apart on the spelling. */
export { KIRO_SIGN_IN_SETTING_ID }
export const KIRO_SIGN_IN_TARGET = {
  tab: KIRO_ACCOUNT_SETTINGS_TAB,
  highlight: `key:${KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}`,
} as const
export const KIRO_SIGN_IN_PATH = settingsPath(KIRO_SIGN_IN_TARGET)

/**
 * Whether a `highlight` value asks for the sign-in card: the chat's deep link
 * (`key:<anchor>`), a Settings search hit (the registry id), or the registry id
 * the card had while it lived on the Agent Harness tab. Old links that carry one
 * on that tab are forwarded to `KIRO_SIGN_IN_PATH`.
 */
export function isKiroSignInHighlight(highlight: string | null | undefined): boolean {
  return highlight === `key:${KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}`
    || highlight === KIRO_SIGN_IN_SETTING_ID
    || highlight === LEGACY_KIRO_SIGN_IN_SETTING_ID
}
