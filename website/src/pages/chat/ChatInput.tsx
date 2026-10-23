import { openActivityToTab, awaitSideRestore, setAgentSwitchNotice, sideSendStatus } from '../../store/chatSlice'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { sendTurn, SEND_ABORT_MS } from '../../chat-core/transport/sendTurn'
import { deliveryUnconfirmedCopy } from '../../chat-core/transport/receiptCopy'
import { sideTurnWire } from './sideTurnWire'
import type { AppDispatch } from '../../store'
import { performAgentSlotSwitch } from '../../lib/agentSwitch'
import { agentSwitchFailureMessage } from '../../utils/agentSwitchFeedback'

/** `failed` marks a command that was recognized but could not run (no slot,
 *  side-open rejected, side-turn rejected — e.g. 409 while a side turn is in
 *  flight). Callers use it to keep or restore the composer text so the
 *  question is recoverable instead of silently lost, and render `error` (the
 *  backend's own message, when it gave one) so the refusal is not silent.
 *  `stage` says WHAT failed: `open` (no panel) vs `turn` (the panel opened,
 *  only the message was refused) — the caller's title must match the state
 *  the user can see. `agent` is a refused `/agent <name>` switch: its reason
 *  is already on the shared agent-switch notice (the picker's surface), so the
 *  caller only keeps the composer text.
 *  `unconfirmed` is the one `failed` that is NOT a refusal: the turn answered
 *  2xx but its receipt could not be read. The composer is kept (the same
 *  reason as a failure: the question must stay recoverable), but the report is
 *  not an error — the side panel's own standing "delivery not confirmed"
 *  notice is set here, the SideChat transport policy for the same receipt. */
export type SlashInterceptResult =
  | { intercepted: true; failed?: boolean; error?: string; stage?: 'open' | 'turn' | 'agent'; unconfirmed?: boolean }
  | { intercepted: false }

/** Caller hooks for commands whose no-slot behaviour lives in the caller. */
export interface SlashInterceptOptions {
  /** `/agent <name>` before the chat has a slot: hold the pick for the slot
   *  the next send creates, as the agent picker does. */
  onPendingAgent?: (agent: string) => void
  /** Brings this slot's Side Chat on screen once the side session is open.
   *  Absent: the chat page's activity panel (`openActivityToTab('side')`). A
   *  host whose Side Chat lives elsewhere (the Members page drawer, a split
   *  pane re-binding the panel) passes its own; `false` means it did not open,
   *  so the question is not sent into a Side Chat nobody can see. */
  openSide?: () => boolean | void | Promise<boolean | void>
}

/** The message of a rejected side-chat request, for the caller's notice. The
 *  api client's ApiError carries the backend `error` body verbatim. */
function failureMessage(e: unknown): string {
  return e instanceof Error && e.message ? e.message : ''
}

// `/btw` is a pure alias for `/side` — same capture group, same handling —
// so a quick "by the way" question reads naturally at the composer.
const SIDE_RE = /^\/(?:side|btw)(?:\s+([\s\S]+))?$/

// `/agent <name>`: one token in the registered-agent grammar (dotted template
// names included). Passed to kiro-cli, the switch is refused while Crew's
// native skill projection is on, and with it off it would move the process
// without Crew's skill scope following -- so the composer sends it to the
// same endpoint the agent picker uses, which restarts the session on the new
// agent with its skills prepared. Mirrors `src/kiro_crew/agent_switch_command.py`,
// which catches the command on every other path (split pane, linked channel
// threads, Slack) once it has been sent.
const AGENT_RE = /^\/agent\s+([A-Za-z0-9][A-Za-z0-9_.-]*)$/
// kiro-cli's own `/agent` subcommands. They are not agent names, so they keep
// going to kiro-cli unchanged (`list` and `schema` answer there).
const AGENT_SUBCOMMANDS = new Set([
  'list', 'schema', 'create', 'edit', 'generate', 'validate', 'migrate',
  'set-default', 'swap', 'delete', 'help',
])

/** The agent an `/agent <name>` command switches to, or null when the text is
 *  not one (bare `/agent`, a subcommand, more than one word). */
export function agentSwitchTarget(trimmed: string): string | null {
  const name = trimmed.match(AGENT_RE)?.[1]
  return name && !AGENT_SUBCOMMANDS.has(name.toLowerCase()) ? name : null
}

/** Whether trimmed composer text is a `/side` (or `/btw`) command. A host
 *  with no Side Chat surface refuses it before anything opens server-side. */
export function isSideCommand(trimmed: string): boolean {
  return SIDE_RE.test(trimmed)
}

/** Sync predicate for the commands interceptSlashCommand handles. The steer
 *  path needs a cheap synchronous check before deciding not to steer — see
 *  ChatPage's steer() — so this stays in lockstep with the matches below. */
export function isInterceptedSlashCommand(raw: string): boolean {
  const trimmed = raw.trim()
  return trimmed === '/onboarding' || SIDE_RE.test(trimmed) || agentSwitchTarget(trimmed) !== null
}

export async function interceptSlashCommand(
  raw: string,
  slot: string | null,
  dispatch: AppDispatch,
  opts: SlashInterceptOptions = {},
): Promise<SlashInterceptResult> {
  const trimmed = raw.trim()
  const agent = agentSwitchTarget(trimmed)
  if (agent !== null) {
    if (!slot) {
      if (!opts.onPendingAgent) return { intercepted: true, failed: true, stage: 'agent' }
      opts.onPendingAgent(agent)
      return { intercepted: true }
    }
    dispatch(setAgentSwitchNotice(null))
    try {
      // `template`: kiro-cli's `/agent` names agent specs, and a stated kind
      // makes the endpoint refuse a name nothing answers (409) instead of
      // committing it and letting the default agent answer the next turn.
      // `announce`: the gateway appends the "Switched to agent" line the
      // split pane's in-turn command appends, so both surfaces read the same.
      await performAgentSlotSwitch(slot, agent, dispatch, 'template', { announce: true })
    } catch (e: unknown) {
      // The caller reports it on the composer's notice, above the kept draft.
      const message = agentSwitchFailureMessage(e)
      return { intercepted: true, failed: true, error: message, stage: 'agent' }
    }
    return { intercepted: true }
  }
  // Client-only command: replay the import gate, then the feature tour.
  // The App shell reads continueOnboarding while AgentImportFlow handles the
  // same event, so Settings can replay only the importer with a plain Event.
  if (trimmed === '/onboarding') {
    window.dispatchEvent(
      new CustomEvent('mc-start-import', { detail: { continueOnboarding: true } }),
    )
    return { intercepted: true }
  }
  const match = trimmed.match(SIDE_RE)
  if (!match) {
    return { intercepted: false }
  }
  if (!slot) {
    // Intentional diagnostic: the command was recognized but can't run
    // without an active slot, which is otherwise silent to the user.
    // eslint-disable-next-line no-console
    console.warn('[/side] no active slot — intercepted but not dispatched')
    return { intercepted: true, failed: true, stage: 'open' }
  }
  const message = match[1]?.trim() ?? ''
  // Neither request below has a deadline of its own, so the command waits for
  // each only until the send's deadline, as the panel's send does, and reports
  // instead of waiting with no outcome. The open is idempotent, so `/side` sent
  // again after an open that timed out simply asks again.
  let openDeadline: ReturnType<typeof setTimeout> | undefined
  try {
    const opened = await Promise.race([
      api.sideOpen(slot).then(() => true),
      new Promise<false>(resolve => { openDeadline = setTimeout(() => resolve(false), SEND_ABORT_MS) }),
    ])
    if (!opened) return { intercepted: true, failed: true, stage: 'open' }
  } catch (e: unknown) {
    // Diagnostic breadcrumb; the user-facing report is the caller's notice,
    // fed by `error`.
    // eslint-disable-next-line no-console
    console.warn('[/side] sideOpen failed:', e)
    return { intercepted: true, failed: true, error: failureMessage(e), stage: 'open' }
  } finally {
    clearTimeout(openDeadline)
  }
  // After a page reload the gateway can hold a side conversation this page has not
  // drawn yet, and the question joins it. Read it first, as the panel does; a read
  // that fails, or is still outstanding at the deadline, holds the question back,
  // so it is not sent into a conversation the panel cannot show. The command
  // stays in the composer.
  const restored = await dispatch(awaitSideRestore({ slot, ms: SEND_ABORT_MS })).unwrap()
  if (restored !== 'restored') {
    // eslint-disable-next-line no-console
    console.warn('[/side] side conversation not loaded:', restored)
    // Not the panel's `restore_failed`: that notice tells the reader to press
    // Retry, and there is no Retry here. Nothing opened, so this reports in the
    // composer's own terms, where the command is what stayed behind.
    return { intercepted: true, failed: true, error: i18nT('pages.chat.sideChat.restore_failed_command'), stage: 'open' }
  }
  if (opts.openSide) {
    if ((await opts.openSide()) === false) return { intercepted: true, failed: true, stage: 'open' }
  } else {
    dispatch(openActivityToTab('side'))
  }
  if (message) {
    // A new submit to the panel supersedes the last one's standing notice --
    // the same rule SideChat's own submit applies -- so an earlier `/side`
    // that went unconfirmed does not leave "not confirmed" standing over a
    // later one that the server plainly accepted. (Only a send that reaches
    // the panel: a refusal below sets its own status, a bare `/side` sends
    // nothing and changes nothing.)
    dispatch(sideSendStatus({ slot, error: null, notice: null }))
    // The SAME wire SideChat's composer sends on, so the two ways of asking a
    // side question classify a receipt by one rule: an `ApiError` from the
    // server is a refusal (fix and resend); a 2xx whose body could not be read,
    // a raw rejection AFTER `/side/turn` was dispatched (connection reset --
    // `/side/open` just succeeded on the same link, so the request probably
    // left) and the transport deadline are all INDETERMINATE. Calling
    // `api.sideTurn` directly classified every rejection but the unreadable
    // body as a retry-safe failure, so a reset after an accepted POST handed
    // the question back with "try again" -- and the retry ran the turn twice.
    // (`sideTurnWire` re-opens the panel first; `/side/open` is idempotent and
    // cheap, and the explicit open above is what tells an open failure --
    // `stage: 'open'`, no panel -- from a turn failure.)
    const receipt = await sendTurn({ message, slot, wire: sideTurnWire(slot) })
    switch (receipt.status) {
      case 'dispatched':
      case 'queued':
        return { intercepted: true }
      case 'refused':
        // The server said no (409 a side turn is already in flight, 400 the
        // expanded question exceeds the byte limit). `failed` lets the caller
        // keep or restore the composer; `error` says why.
        // eslint-disable-next-line no-console
        console.warn('[/side] sideTurn refused:', receipt.reason)
        return { intercepted: true, failed: true, error: receipt.reason ?? '', stage: 'turn' }
      case 'transport-error':
        // Nothing left the browser (the wire's own `/side/open` failed, or the
        // deadline fired before the turn was dispatched): a plain failure the
        // caller restores from, with its connection copy.
        return { intercepted: true, failed: true, error: '', stage: 'turn' }
      case 'response-late':
      case 'unknown':
        // The server PROBABLY took the turn -- but the outage that lost the
        // receipt can also swallow the WS row that would show the question in
        // the panel, and nothing replays it. Treating it as success cleared the
        // composer, so the question could vanish from the UI; treating it as a
        // failure invites a retry that runs the turn twice. UNCONFIRMED, the
        // way SideChat treats the same receipt: the composer keeps the command
        // (nothing is lost) and the side panel stands its "delivery not
        // confirmed" notice, which tells the user to look before resending.
        dispatch(sideSendStatus({ slot, notice: { text: deliveryUnconfirmedCopy(true), restoredDraft: false } }))
        return { intercepted: true, failed: true, unconfirmed: true, stage: 'turn' }
    }
  }
  return { intercepted: true }
}
