import { createElement } from 'react'
import type { RootState } from '../store'
import type { ConfirmOptions } from '../components/ConfirmDialog'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { closeBusyReasons, openWorkerCount, type CloseBusyReason } from './closeBusyGate'

// One literal `i18nT` call per reason, so the key checks can see every key.
export function reasonLine(reason: CloseBusyReason): string {
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
  { settingOn, checkBusy = true }: CloseConfirmOptions,
  workers: number | null = 0,
): ConfirmOptions | null {
  // Always read the live work for the TEXT: the alias that skips the busy
  // check still must not tell the user "nothing is running" over a live turn.
  const reasons = closeBusyReasons(state, slotKey)
  // A conductor's close also closes its open workers (#30877), so it always
  // asks, on every path and whatever the setting: one click must not end live
  // work in other sessions unasked (#31629). `workers` is the server's count
  // (`closeWorkerCount`); `null` means it could not be read, and the close
  // still asks, without a number.
  if (workers === null || workers > 0) return conductorClosePrompt(reasons, workers)
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

export interface CloseConfirmOptions { settingOn: boolean; checkBusy?: boolean }

/**
 * How many open sessions closing `slotKey` would close with it, as the server's
 * own close selection answers (`GET .../close-preview`): only the server knows
 * which stamped sessions it still manages. `null` when that read fails.
 */
export async function closeWorkerCount(slotKey: string): Promise<number | null> {
  try {
    const { workers } = await api.chatSlotClosePreview(slotKey)
    return typeof workers === 'number' && workers >= 0 ? workers : null
  } catch {
    return null
  }
}

/**
 * Work out the close prompt and hand it to `decide` (`null`: close without
 * asking). Synchronous when no session in the list is stamped with `slotKey`,
 * since such a close cannot take workers (`openWorkerCount`); otherwise it asks
 * the server first (`closeWorkerCount`) and builds the prompt from the state as
 * it is when that answer arrives.
 */
export function decideClosePrompt(
  getState: () => RootState,
  slotKey: string,
  opts: CloseConfirmOptions,
  decide: (ask: ConfirmOptions | null) => void,
): void {
  if (openWorkerCount(getState(), slotKey) === 0) {
    decide(closeConfirmPrompt(getState(), slotKey, opts))
    return
  }
  void closeWorkerCount(slotKey).then(workers => decide(closeConfirmPrompt(getState(), slotKey, opts, workers)))
}

/** The folder-cleanup row line for a conductor that would take `count` open
 *  workers with it (`null`: the count could not be read); that panel's Close
 *  button is its confirm. */
export function workersLine(count: number | null): string {
  return count === null
    ? i18nT('hooks.useSessionActions.close_confirm.reason_workers_unknown')
    : i18nT('hooks.useSessionActions.close_confirm.reason_workers', { count })
}

/**
 * The one question a conductor close with open workers asks: the title counts
 * the workers (or says "its open workers" when the count could not be read), the
 * body names any live work in the conductor itself, then what happens to the
 * workers, then what is kept. Confirming runs the same close as before (#30877
 * closes the workers); Cancel closes nothing.
 */
function conductorClosePrompt(reasons: CloseBusyReason[], workers: number | null): ConfirmOptions {
  const paragraphs = [
    ...(reasons.length > 0 ? [reasons.map(reasonLine).join(' ')] : []),
    i18nT('hooks.useSessionActions.close_confirm.conductor_workers'),
    i18nT('hooks.useSessionActions.close_confirm.actions'),
  ]
  return {
    title: workers === null
      ? i18nT('hooks.useSessionActions.close_confirm.conductor_title_unknown')
      : i18nT('hooks.useSessionActions.close_confirm.conductor_title', { count: workers }),
    body: createElement('div', { className: 'flex flex-col gap-2' },
      ...paragraphs.map((text, i) => createElement('p', { key: i, className: 'm-0' }, text)),
    ),
    confirmLabel: i18nT('hooks.useSessionActions.close_confirm.conductor_confirm'),
  }
}
