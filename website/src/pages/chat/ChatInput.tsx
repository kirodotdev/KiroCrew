import { openActivityToTab, sideSendStatus } from '../../store/chatSlice'
import { api } from '../../api/client'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { deliveryUnconfirmedCopy } from '../../chat-core/transport/receiptCopy'
import { sideTurnWire } from './sideTurnWire'
import type { AppDispatch } from '../../store'

/** `failed` marks a command that was recognized but could not run (no slot,
 *  side-open rejected, side-turn rejected — e.g. 409 while a side turn is in
 *  flight). Callers use it to keep or restore the composer text so the
 *  question is recoverable instead of silently lost, and render `error` (the
 *  backend's own message, when it gave one) so the refusal is not silent.
 *  `stage` says WHAT failed: `open` (no panel) vs `turn` (the panel opened,
 *  only the message was refused) — the caller's title must match the state
 *  the user can see.
 *  `unconfirmed` is the one `failed` that is NOT a refusal: the turn answered
 *  2xx but its receipt could not be read. The composer is kept (the same
 *  reason as a failure: the question must stay recoverable), but the report is
 *  not an error — the side panel's own standing "delivery not confirmed"
 *  notice is set here, the SideChat transport policy for the same receipt. */
export type SlashInterceptResult =
  | { intercepted: true; failed?: boolean; error?: string; stage?: 'open' | 'turn'; unconfirmed?: boolean }
  | { intercepted: false }

/** The message of a rejected side-chat request, for the caller's notice. The
 *  api client's ApiError carries the backend `error` body verbatim. */
function failureMessage(e: unknown): string {
  return e instanceof Error && e.message ? e.message : ''
}

// `/btw` is a pure alias for `/side` — same capture group, same handling —
// so a quick "by the way" question reads naturally at the composer.
const SIDE_RE = /^\/(?:side|btw)(?:\s+([\s\S]+))?$/

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
