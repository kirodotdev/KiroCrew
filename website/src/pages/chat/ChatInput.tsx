import { openActivityToTab, setAgentSwitchNotice } from '../../store/chatSlice'
import { api } from '../../api/client'
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
 *  caller only keeps the composer text. */
export type SlashInterceptResult =
  | { intercepted: true; failed?: boolean; error?: string; stage?: 'open' | 'turn' | 'agent' }
  | { intercepted: false }

/** Caller hooks for commands whose no-slot behaviour lives in the caller. */
export interface SlashInterceptOptions {
  /** `/agent <name>` before the chat has a slot: hold the pick for the slot
   *  the next send creates, as the agent picker does. */
  onPendingAgent?: (agent: string) => void
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
  try {
    await api.sideOpen(slot)
  } catch (e: unknown) {
    // Diagnostic breadcrumb; the user-facing report is the caller's notice,
    // fed by `error`.
    // eslint-disable-next-line no-console
    console.warn('[/side] sideOpen failed:', e)
    return { intercepted: true, failed: true, error: failureMessage(e), stage: 'open' }
  }
  dispatch(openActivityToTab('side'))
  if (message) {
    let failed = false
    let error = ''
    await api.sideTurn(slot, message).catch((e: unknown) => {
      // Failure surfaces through `failed` so the caller can restore the
      // composer (e.g. 409: a side turn is already in flight, or 400: the
      // expanded question exceeds the byte limit), and through `error` so it
      // can say why. The warn stays as the diagnostic detail channel.
      // eslint-disable-next-line no-console
      console.warn('[/side] sideTurn failed:', e)
      failed = true
      error = failureMessage(e)
    })
    if (failed) return { intercepted: true, failed: true, error, stage: 'turn' }
  }
  return { intercepted: true }
}
