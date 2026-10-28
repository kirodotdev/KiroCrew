/**
 * "Open in kiro-cli": the short reason a chat cannot open in kiro-cli, for each
 * refusal code the gateway answers (`handlers/kiro_cli_launch.py`). One map for
 * the chat menu's disabled row and the terminal tab's refusal banner, so the
 * same refusal reads the same way in both.
 */

import { i18nT } from '../i18n/t'

/** Codes after which the menu leaves the action out instead of disabling it:
 *  the chat can never open in kiro-cli, so a greyed row would only be noise. */
export const KIRO_CLI_HIDDEN_CODES: ReadonlySet<string> = new Set([
  'kiro_cli_not_kiro',
  'kiro_cli_unsupported',
  'kiro_cli_remote',
  'kiro_cli_terminal_disabled',
])

// Literal keys, indexed: a key assembled at runtime is invisible to the i18n tooling.
const BLOCKED_KEYS = {
  kiro_cli_no_session: 'components.sessionActionsMenu.kiro_cli_blocked_no_session',
  kiro_cli_no_log: 'components.sessionActionsMenu.kiro_cli_blocked_no_log',
  kiro_cli_no_project: 'components.sessionActionsMenu.kiro_cli_blocked_no_project',
  kiro_cli_not_installed: 'components.sessionActionsMenu.kiro_cli_blocked_not_installed',
  kiro_cli_busy: 'components.sessionActionsMenu.reload_blocked_running',
  kiro_cli_subagents: 'components.sessionActionsMenu.reload_blocked_subagents',
  kiro_cli_already_open: 'components.sessionActionsMenu.kiro_cli_blocked_already_open',
  kiro_cli_project_agent: 'components.sessionActionsMenu.kiro_cli_blocked_project_agent',
  kiro_cli_ended: 'components.sessionActionsMenu.kiro_cli_blocked_ended',
} as const

/** The short reason for refusal `code`; an unknown code reads as unavailable. */
export function kiroCliBlockedLabel(code: string): string {
  const key = (BLOCKED_KEYS as Record<string, string>)[code]
    ?? 'components.sessionActionsMenu.kiro_cli_blocked_unavailable'
  return i18nT(key)
}
