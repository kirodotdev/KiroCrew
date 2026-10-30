import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { isRetryNotice } from '../../lib/retryNotice'
import type { NoticeTone } from './NoticeCard'

/**
 * Localized copy for the `error` rows the gateway's transient-5xx ladder appends.
 *
 * Each such row carries a structured wire token in `meta.notice`
 * (`TRANSIENT_NOTICE_*` in `src/kiro_crew/dashboard/chat_utils.py`); the row's
 * English content is the fallback for channel mirrors and SSE readers and is
 * never shown here. The dashboard keys its `i18nT()` copy on the token, so the
 * wording can change on either side without the other falling back to raw
 * English. The tokens below are WIRE VALUES, not copy — never translate them,
 * and keep them byte-identical to the Python constants
 * (`test/test_transient_notice_parity.py` pins both sides).
 */
const RETRYING_KEY = 'pages.chat.transientNotice.retrying'
const RESUMING_KEY = 'pages.chat.transientNotice.resuming'
const RESTORED_KEY = 'pages.chat.transientNotice.restored'
const INTERRUPTED_KEY = 'pages.chat.transientNotice.interrupted'
const GIVE_UP_KEY = 'pages.chat.transientNotice.give_up'

type Shape = 'retrying' | 'resuming' | 'give_up'

/** `meta.notice` wire tokens → row shape. Mirrors chat_utils.TRANSIENT_NOTICE_*. */
const NOTICE_TOKENS: Readonly<Record<string, Shape>> = {
  transient_retrying: 'retrying',
  transient_resuming: 'resuming',
  transient_give_up: 'give_up',
}

/**
 * Rows persisted by a gateway that predates the token carry only their English
 * content, and are re-read from disk on every reload with no migration. They are
 * recognised by these closed patterns (the gateway no longer emits this wording,
 * so nothing new can match) and classified the way that gateway classified them:
 *
 * - "retrying…" was only ever written with a recovery already queued → pending.
 * - "recovering…" was written in both cases, distinguished only by
 *   `meta.kind = transient_retry` on the row that DID re-queue → pending when the
 *   kind is present, terminal otherwise.
 * - "please retry." was always terminal.
 */
const LEGACY_RETRYING_RE = /^⟳ Backend hiccup — retrying…$/u
const LEGACY_RECOVERING_RE = /^⟳ Backend hiccup — recovering…$/u
const LEGACY_GIVE_UP_RE = /^⟳ Backend hiccup — please retry\.$/u

function shapeOf(m: ChatMessage): Shape | null {
  const token = (m.meta as { notice?: unknown } | undefined)?.notice
  if (typeof token === 'string' && token in NOTICE_TOKENS) return NOTICE_TOKENS[token]
  const content = (m.content ?? '').trim()
  if (LEGACY_RETRYING_RE.test(content)) return 'retrying'
  if (LEGACY_RECOVERING_RE.test(content)) return isRetryNotice(m) ? 'resuming' : 'give_up'
  if (LEGACY_GIVE_UP_RE.test(content)) return 'give_up'
  return null
}

/**
 * True for a PENDING link of the transient ladder — the gateway has already
 * re-queued the turn and nothing is asked of the person. A crewmate's chat
 * drops these rows (`components/chat/crewmateBubbles`): routine recovery is the
 * runtime's own machinery, and a patrolling crewmate otherwise stacks one
 * "Connection restored" card per retried wake with no message of its own
 * between them. The terminal give-up is NOT pending: it is the one shape where
 * the person must act, and it keeps drawing everywhere.
 */
export function isPendingTransientNotice(m: ChatMessage): boolean {
  if (m.role !== 'error') return false
  const shape = shapeOf(m)
  return shape === 'retrying' || shape === 'resuming'
}

export type TransientNotice =
  | {
      /**
       * The gateway has ALREADY queued the retry: routine status, drawn as a soft
       * NoticeCard in the given tone (warn while pending; info once the recovery
       * window has closed, reading "restored" when an answer arrived below it and
       * the past-tense "interrupted" when it closed without one).
       */
      card: 'notice'
      text: string
      tone: NoticeTone
    }
  | {
      /**
       * The ladder gave up, or nothing could be re-queued (Stop active, nested
       * turn): the person has to act, so the row keeps the red ErrorCard — and
       * with it the Continue affordance the host may attach.
       */
      card: 'error'
      text: string
    }

/** How THIS recovery ended, as far as the transcript below the notice shows. */
type Outcome = 'answered' | 'failed' | 'pending'

/**
 * Scan the rows after this notice, within THIS recovery only.
 *
 * - `answered`: an assistant (or live streaming) row proves the model answered
 *   again.
 * - `failed`: the window closed without an answer: an `error` row that is not
 *   itself a pending link of the same ladder (the terminal give-up, or any
 *   other ❌ — the recovery failed). Every gateway path that drops the
 *   re-queued retry appends such a row, so this is the one proof of failure.
 * - `pending`: no row has decided the outcome yet, so the ladder may still be
 *   driving. That includes a `user` row with no answer or error before it
 *   (the abandoned case), which stops the scan without settling. A message
 *   typed during the backoff is written at send time, while the pre-token
 *   replay is re-queued at the head and still runs, so a user row does not
 *   prove the retry ended. Nor can the answer below it be read as "restored":
 *   it may belong to the new turn.
 *
 * A later pending link (`retrying…` after `retrying…`) is the same ladder still
 * driving, so the scan continues through it and every link settles together.
 * So does any other re-queued recovery row (`kind: transient_retry`, such as
 * "Connection lost — retrying…" after a dead process): recovery is still
 * running. Other roles (tool, notice…) are transparent.
 */
function answeredAfter(messages: readonly ChatMessage[], index: number): Outcome {
  for (let j = index + 1; j < messages.length; j++) {
    const row = messages[j]
    const r = row.role
    if (r === 'assistant' || r === 'streaming') return 'answered'
    if (r === 'user') return 'pending'
    if (r === 'error') {
      const later = shapeOf(row)
      if (later === 'retrying' || later === 'resuming') continue
      // Any other re-queued recovery row (a dead process, a lost session, a
      // model fallback) is still driving toward an answer; the give-up row
      // never carries this kind, so it still closes the window.
      if (isRetryNotice(row)) continue
      return 'failed'
    }
  }
  return 'pending'
}

/**
 * Resolve an `error` row into localized transient-notice copy, or null when the
 * row is not one of the gateway's transient-5xx notices (any other error keeps
 * its verbatim prose).
 *
 * A pending row is tense-aware. The gateway appends nothing when the re-queued
 * turn succeeds, so on its own the row would read "retrying…" in a transcript
 * whose answer already sits below it; when a later assistant row exists WITHIN
 * the same recovery the same card reads "Connection restored" instead. A later
 * ERROR row never settles it to "restored" — the ladder may have given up, and
 * claiming a restore then would be false — and it CLOSES the window: the card
 * settles to a muted, past-tense "Connection interrupted", so the row above a
 * red give-up does not read as a retry still in progress, and an answer to a
 * later, unrelated turn never flips it to "restored". A new user turn stops the
 * scan without settling either way (see `answeredAfter`). The terminal shape
 * never settles.
 *
 * The two pending shapes are distinct gateway states and keep distinct copy:
 * `retrying` (nothing streamed, the whole turn is re-sent) and `resuming` (a
 * partial answer is already on screen above the row and the model is asked to
 * finish it), which is why the `resuming` copy names the answer it is finishing.
 */
export function resolveTransientNotice(
  m: ChatMessage,
  messages: readonly ChatMessage[],
  index: number,
): TransientNotice | null {
  if (m.role !== 'error') return null
  const shape = shapeOf(m)
  if (!shape) return null
  if (shape === 'give_up') return { card: 'error', text: i18nT(GIVE_UP_KEY) }
  const outcome = answeredAfter(messages, index)
  if (outcome === 'answered') return { card: 'notice', text: i18nT(RESTORED_KEY), tone: 'info' }
  if (outcome === 'failed') return { card: 'notice', text: i18nT(INTERRUPTED_KEY), tone: 'info' }
  return { card: 'notice', text: i18nT(shape === 'resuming' ? RESUMING_KEY : RETRYING_KEY), tone: 'warn' }
}
