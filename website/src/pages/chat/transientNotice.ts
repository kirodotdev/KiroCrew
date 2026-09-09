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

export type TransientNotice =
  | {
      /**
       * The gateway has ALREADY queued the retry: routine status, drawn as a soft
       * NoticeCard in the given tone (warn while pending, info once an answer has
       * arrived below it).
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

/**
 * True once a later row proves the model answered again after this notice —
 * within THIS recovery only. The scan stops at the first row that closes the
 * recovery window without an answer: a `user` row (a new turn; whatever answers
 * it says nothing about this one) or an `error` row that is not itself a pending
 * link of the same ladder (the terminal give-up, or any other ❌ — the recovery
 * failed). A later pending link (`retrying…` after `retrying…`) is the same
 * ladder still driving, so the scan continues through it and every link settles
 * together when the answer finally lands. Other roles (tool, notice…) are
 * transparent.
 */
function answeredAfter(messages: readonly ChatMessage[], index: number): boolean {
  for (let j = index + 1; j < messages.length; j++) {
    const row = messages[j]
    const r = row.role
    if (r === 'assistant' || r === 'streaming') return true
    if (r === 'user') return false
    if (r === 'error') {
      const later = shapeOf(row)
      if (later === 'retrying' || later === 'resuming') continue
      return false
    }
  }
  return false
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
 * ERROR row does not settle it — the ladder may have given up, and claiming a
 * restore then would be false — and it also CLOSES the window: an answer to a
 * later, unrelated turn never flips a failed recovery to "restored" above its
 * own red error. The terminal shape never settles.
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
  if (answeredAfter(messages, index)) return { card: 'notice', text: i18nT(RESTORED_KEY), tone: 'info' }
  return { card: 'notice', text: i18nT(shape === 'resuming' ? RESUMING_KEY : RETRYING_KEY), tone: 'warn' }
}
