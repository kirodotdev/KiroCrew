import { i18nT } from '../i18n/t'
import { findReport, parseErrorCode } from './errorReport'
import type { ErrorReport } from './errorReport'

/**
 * The localized line for a fork that failed, shared by every surface that offers one.
 *
 * The over-capacity refusal reaches three entry points -- the transcript's own fork
 * button, the session grid's, and "Duplicate" in the session list -- and a whole-session
 * fork has no chosen message, so all three advise the recovery the backend names: fork AT
 * a message instead. Keyed on the machine-readable code, never on the prose, and reusing
 * the transcript path's own strings so the three cannot drift into three answers.
 *
 * `direction` mirrors what the transcript path resolves from `tail_fork_enabled`: a head
 * deployment copies the slice UP TO the chosen message, a tail one copies FROM it onwards,
 * so the smaller slice lies at opposite ends and the advice inverts with it.
 */
export function forkFailureMessageForCode(
  code: string | undefined,
  raw: string,
  direction: 'head' | 'tail' | 'unknown' = 'head',
  surface: 'transcript' | 'offsite' | 'offsite-compact' = 'transcript',
  title?: string,
  control?: string,
): string {
  if (code === 'fork_corpus_too_large') {
    // Surface decides whether "pick a message" is reachable at all, and both offsite
    // spellings name the session because neither renders beside the control pressed.
    const key = surface === 'offsite-compact'
      ? 'pages.chatPage.fork_too_large_offsite_compact'
      : surface === 'offsite'
        ? 'pages.chatPage.fork_too_large_offsite_unknown'
        : (direction === 'tail'
            ? 'pages.chatPage.fork_too_large_error_tail'
            : 'pages.chatPage.fork_too_large_error_head')
    // The control's own label, so the sentence names what the reader must click and
    // stays localized with the button rather than hard-coding its English name.
    return i18nT(key, {
      control: control ?? i18nT('pages.chat.assistantMessage.fork_conversation_from_here'),
      title,
    })
  }
  if (code === 'no_messages_after_fork_point') {
    return i18nT('pages.chatPage.fork_failed_error', { error: i18nT('pages.chat.assistantMessage.fork_no_later_messages') })
  }
  return i18nT('pages.chatPage.fork_failed_error', { error: raw || i18nT('pages.chatPage.unknown_error') })
}

/**
 * A localized line PLUS the report it can no longer be matched to.
 *
 * The journal is keyed on the RAW wire message, so replacing that text with a
 * localized one severs the lookup `ErrorNotice` would otherwise do for itself --
 * and the endpoint, HTTP status and backend `code` are the whole reason the shared
 * surface is mandatory. Resolve the report against the raw text while it is still
 * in hand, and hand both to the caller.
 */
export interface ForkFailureNotice {
  message: string
  report: ErrorReport | undefined
}

/**
 * Report a transcript fork failure on the page's shared action-error notice.
 *
 * The journal is keyed on the RAW wire message, so the localized replacement has to
 * carry the report it can no longer be matched to (endpoint, status, backend code).
 * `direction` is what `handleFork` resolved, so the advice points at the smaller slice.
 */
export function showTranscriptForkFailure(
  show: (message: string, title?: string, report?: ErrorReport) => void,
  code: string | undefined,
  raw: string | undefined,
  direction: 'head' | 'tail',
  control?: string,
): void {
  show(forkFailureMessageForCode(code, raw || '', direction, 'transcript', undefined, control), undefined, findReport(raw))
}

/**
 * The same line for a caller off the transcript, plus the report it can no longer be
 * matched to. No config read: the offsite copy names the recovery without a direction.
 */
export function forkFailureNoticeOffsite(
  body: string | undefined,
  raw: string,
  compact = false,
  title?: string,
): ForkFailureNotice {
  return {
    message: forkFailureMessageForCode(
      parseErrorCode(body),
      raw,
      'unknown',
      compact ? 'offsite-compact' : 'offsite',
      title,
    ),
    report: findReport(raw),
  }
}
