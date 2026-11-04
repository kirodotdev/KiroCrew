import { createElement } from 'react'
import type { RootState } from '../store'
import type { ConfirmOptions } from '../components/ConfirmDialog'
import { i18nT } from '../i18n/t'
import { closeBusyReasons, type CloseBusyReason } from './closeBusyGate'

// One literal `i18nT` call per reason, so the key checks can see every key.
function reasonLine(reason: CloseBusyReason): string {
  switch (reason) {
    case 'approval': return i18nT('hooks.useSessionActions.close_confirm.reason_approval')
    case 'turn': return i18nT('hooks.useSessionActions.close_confirm.reason_turn')
    case 'subagents': return i18nT('hooks.useSessionActions.close_confirm.reason_subagents')
    case 'queue': return i18nT('hooks.useSessionActions.close_confirm.reason_queue')
    case 'workflow': return i18nT('hooks.useSessionActions.close_confirm.reason_workflow')
    case 'goal_loop': return i18nT('hooks.useSessionActions.close_confirm.reason_goal_loop')
    case 'monitor': return i18nT('hooks.useSessionActions.close_confirm.reason_monitor')
    case 'work': return i18nT('hooks.useSessionActions.close_confirm.reason_work')
  }
}

/**
 * The confirmation a session close raises, or `null` when the close should go
 * through without asking. Every close path (sidebar ✕, session menus,
 * ⌘W / Ctrl+W, ⌥⇧W) shows it through the in-app dialog (`askAppConfirm`).
 *
 * The body names the trigger (each kind of live work, or the "Confirm Before
 * Closing Session" setting when nothing is running, with the path to that
 * setting) and its consequence, then one closing line: the conversation and
 * any unsent message are kept. The title stays a yes/no question, and the
 * confirm button restates the action.
 *
 * The "unsent message is kept" part is unconditional. Drafts are stored per
 * session and a close does not clear them, so it holds whether or not one
 * exists; reading the stored draft to decide would lag the composer by its
 * save debounce.
 *
 * `checkBusy` is false for the ⌥⇧W alias, which keeps its shipped behaviour of
 * asking only when the setting is on. It decides only WHETHER to ask; when the
 * alias does ask, the text still names any live work.
 */
export function closeConfirmPrompt(
  state: RootState,
  slotKey: string,
  { settingOn, checkBusy = true }: { settingOn: boolean; checkBusy?: boolean },
): ConfirmOptions | null {
  // Always read the live work for the TEXT: the alias that skips the busy
  // check still must not tell the user "nothing is running" over a live turn.
  const reasons = closeBusyReasons(state, slotKey)
  if (!settingOn && (!checkBusy || reasons.length === 0)) return null
  // The reasons share one paragraph however many there are.
  const why = reasons.length > 0
    ? reasons.map(reasonLine).join(' ')
    : i18nT('hooks.useSessionActions.close_confirm.reason_setting', {
      // The path to the toggle, so the user can find it and turn it off.
      setting: `${i18nT('settings.tabs.chat.label')} › ${i18nT('pages.settings.chatPanel.confirm_before_closing_session')}`,
    })
  return {
    title: i18nT('hooks.useSessionActions.close_confirm.title'),
    // Two paragraphs, not one joined string: a sentence separator differs by
    // language, and a paragraph break needs none.
    body: createElement('div', { className: 'flex flex-col gap-2' },
      createElement('p', { className: 'm-0' }, why),
      createElement('p', { className: 'm-0' }, i18nT('hooks.useSessionActions.close_confirm.actions')),
    ),
    confirmLabel: i18nT('hooks.useSessionActions.close_confirm.confirm'),
  }
}
