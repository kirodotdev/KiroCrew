/**
 * The Slack Code switch registry for the settings page: every switch the server
 * registers (`slack_manifest.FEATURE_SCOPES`), in display order, with the
 * catalog key of its name.
 *
 * The Slack Code draft, its PUT payload, the capability lines and the "Update
 * your Slack app" panel all iterate this registry, so registering a switch is
 * its key in `SlackCodeSwitchKey` plus its entry here (a `Record` over that
 * union, so the compiler refuses a key without one); the switch's own toggle in
 * `SlackCodePanel` is the only markup it adds.
 */
import type { SlackCodeSwitchKey } from '../../api/client'

/** Catalog key of each switch's name, as its toggle and the update panel show it. */
export const SLACK_CODE_SWITCH_LABEL: Record<SlackCodeSwitchKey, string> = {
  agent_sessions: 'pages.settings.slackPanel.slackCode.agent_sessions',
}

/** Every registered switch, in display order. */
export const SLACK_CODE_SWITCHES = Object.keys(SLACK_CODE_SWITCH_LABEL) as SlackCodeSwitchKey[]

export function isSlackCodeSwitch(key: string): key is SlackCodeSwitchKey {
  return Object.prototype.hasOwnProperty.call(SLACK_CODE_SWITCH_LABEL, key)
}
