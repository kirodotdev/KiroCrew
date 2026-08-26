import { openActivityToTab } from '../../store/chatSlice'
import { api } from '../../api/client'
import type { AppDispatch } from '../../store'

/** `failed` marks a command that was recognized but could not run (no slot,
 *  side-open rejected, side-turn rejected — e.g. 409 while a side turn is in
 *  flight). Callers use it to keep or restore the composer text so the
 *  question is recoverable instead of silently lost. */
export type SlashInterceptResult =
  | { intercepted: true; failed?: boolean }
  | { intercepted: false }

const SIDE_RE = /^\/side(?:\s+([\s\S]+))?$/

/** Sync predicate for the commands interceptSlashCommand handles. The steer
 *  path needs a cheap synchronous check before deciding not to steer — see
 *  ChatPage's steer() — so this stays in lockstep with the matches below. */
export function isInterceptedSlashCommand(raw: string): boolean {
  const trimmed = raw.trim()
  return trimmed === '/onboarding' || SIDE_RE.test(trimmed)
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
  const match = trimmed.match(SIDE_RE)
  if (!match) {
    return { intercepted: false }
  }
  if (!slot) {
    // Intentional diagnostic: the command was recognized but can't run
    // without an active slot, which is otherwise silent to the user.
    // eslint-disable-next-line no-console
    console.warn('[/side] no active slot — intercepted but not dispatched')
    return { intercepted: true, failed: true }
  }
  const message = match[1]?.trim() ?? ''
  try {
    await api.sideOpen(slot)
  } catch (e: unknown) {
    // Intentional diagnostic breadcrumb for a silent side-open failure.
    // eslint-disable-next-line no-console
    console.warn('[/side] sideOpen failed:', e)
    return { intercepted: true, failed: true }
  }
  dispatch(openActivityToTab('side'))
  if (message) {
    let failed = false
    await api.sideTurn(slot, message).catch((e: unknown) => {
      // Failure surfaces through `failed` so the caller can restore the
      // composer (e.g. 409: a side turn is already in flight, or 400: the
      // expanded question exceeds the byte limit). The warn stays as the
      // diagnostic detail channel.
      // eslint-disable-next-line no-console
      console.warn('[/side] sideTurn failed:', e)
      failed = true
    })
    if (failed) return { intercepted: true, failed: true }
  }
  return { intercepted: true }
}
