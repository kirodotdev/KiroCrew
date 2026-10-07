import { forkSlot, openActivityToTab } from '../../store/chatSlice'
import { api } from '../../api/client'
import type { AppDispatch } from '../../store'
import { errMessage } from '../../utils/thunkError'
import { queryClient } from '../../api/queryClient'

/** `failed` marks a command that was recognized but could not run (no slot,
 *  side-open rejected, side-turn rejected — e.g. 409 while a side turn is in
 *  flight). Callers use it to keep or restore the composer text so the
 *  question is recoverable instead of silently lost, and render `error` (the
 *  backend's own message, when it gave one) so the refusal is not silent.
 *  `stage` says WHAT failed: `open` (no panel) vs `turn` (the panel opened,
 *  only the message was refused) — the caller's title must match the state
 *  the user can see. */
export type SlashInterceptResult =
  | { intercepted: true; failed?: boolean; error?: string; stage?: 'open' | 'turn' | 'rewind'; switchTo?: string }
  | { intercepted: false }

/** The message of a rejected side-chat request, for the caller's notice. The
 *  api client's ApiError carries the backend `error` body verbatim. */
function failureMessage(e: unknown): string {
  return e instanceof Error && e.message ? e.message : ''
}

// `/btw` is a pure alias for `/side` — same capture group, same handling —
// so a quick "by the way" question reads naturally at the composer.
const SIDE_RE = /^\/(?:side|btw)(?:\s+([\s\S]+))?$/

// `/rewind [N]` -- kiro-cli's own command: fork the conversation at an earlier
// turn, leaving the original intact. The dashboard runs it as its own fork
// rather than adding it to the backend's `_SLASH_COMMANDS` passthrough: kiro-cli's
// /rewind forks into a NEW kiro-cli session the dashboard has no slot for, so
// the fork would be invisible in the sidebar and the tab would keep pointing at
// the old conversation. Any argument is captured, not just digits,
// so `/rewind two` is refused by the server's range check instead of being sent
// to the agent as a chat message.
const REWIND_RE = /^\/rewind(?:\s+(\S+))?$/

/** Whether the agent harness reports a `/rewind` of its own. On the claude
 *  provider every leading slash is forwarded to the harness, whose `/rewind` also
 *  restores code; the dashboard's fork must not silently replace that. Read from
 *  the slash-menu's cached `GET /api/slash-commands` answer (the menu opens, and
 *  fetches, as soon as "/" is typed), synchronously, so the sync predicate below
 *  and the handler always agree. No cached answer means no harness command. */
function harnessOwnsRewind(): boolean {
  const cmds = queryClient.getQueryData<{ name: string }[]>(['slash-commands'])
  return Array.isArray(cmds) && cmds.some(c => c?.name === '/rewind')
}

function isRewindCommand(trimmed: string): RegExpMatchArray | null {
  return harnessOwnsRewind() ? null : trimmed.match(REWIND_RE)
}

/** Sync predicate for the commands interceptSlashCommand handles. The steer
 *  path needs a cheap synchronous check before deciding not to steer — see
 *  ChatPage's steer() — so this stays in lockstep with the matches below. */
export function isInterceptedSlashCommand(raw: string): boolean {
  const trimmed = raw.trim()
  return trimmed === '/onboarding' || SIDE_RE.test(trimmed) || isRewindCommand(trimmed) !== null
}

/** Fork the slot before its Nth-last turn and name the fork in `switchTo`. Files,
 *  commands and anything else the agent did after that point are NOT undone:
 *  the fork only copies the conversation. */
async function rewindSlot(
  arg: string | undefined,
  slot: string | null,
  dispatch: AppDispatch,
): Promise<SlashInterceptResult> {
  if (!slot) return { intercepted: true, failed: true, stage: 'rewind' }
  // A non-integer argument is sent as 0, which the server refuses with the range
  // it accepts, so the reason is the server's own and one rule decides it. Not
  // NaN: JSON serialises NaN as null, and a null turns_back is a plain fork of
  // the whole conversation.
  const turnsBack = arg === undefined ? 1 : /^\d+$/.test(arg) ? Number(arg) : 0
  try {
    const result = await dispatch(forkSlot({ slot, turnsBack })).unwrap()
    if (!result?.ok || !result.key) {
      return { intercepted: true, failed: true, error: result?.error || '', stage: 'rewind' }
    }
    // The caller switches, not this function: it must clear the composer
    // FIRST, or the slot switch parks "/rewind" as the parent's draft.
    return { intercepted: true, switchTo: result.key }
  } catch (e: unknown) {
    // `unwrap()` rejects with a SerializedError, a plain object rather than an
    // Error, so failureMessage would drop the server's reason.
    return { intercepted: true, failed: true, error: errMessage(e), stage: 'rewind' }
  }
}

export async function interceptSlashCommand(
  raw: string,
  slot: string | null,
  dispatch: AppDispatch,
): Promise<SlashInterceptResult> {
  const trimmed = raw.trim()
  // Client-only command: replay the import gate, then the feature tour.
  // The App shell reads continueOnboarding while AgentImportFlow handles the
  // same event, so Settings can replay only the importer with a plain Event.
  if (trimmed === '/onboarding') {
    window.dispatchEvent(
      new CustomEvent('mc-start-import', { detail: { continueOnboarding: true } }),
    )
    return { intercepted: true }
  }
  const rewind = isRewindCommand(trimmed)
  if (rewind) return rewindSlot(rewind[1], slot, dispatch)
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
