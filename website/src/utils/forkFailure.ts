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
  surface: 'transcript' | 'offsite' = 'transcript',
): string {
  if (code === 'fork_corpus_too_large') {
    // Surface decides whether "pick a message" is reachable at all: off the
    // transcript the reader has no message list in front of them.
    // Advising "earlier" on a tail deployment copies MORE rows, so a guessed direction
    // makes the retry fail identically. Only the offsite surface can reach unknown.
    const key = surface === 'offsite'
      ? (direction === 'unknown'
          ? 'pages.chatPage.fork_too_large_offsite_unknown'
          : direction === 'tail'
            ? 'pages.chatPage.fork_too_large_offsite_tail'
            : 'pages.chatPage.fork_too_large_offsite_head')
      : (direction === 'tail'
          ? 'pages.chatPage.fork_too_large_error_tail'
          : 'pages.chatPage.fork_too_large_error_head')
    // The control's own label, so the sentence names what the reader must click and
    // stays localized with the button rather than hard-coding its English name.
    return i18nT(key, {
      control: i18nT('pages.chat.assistantMessage.fork_conversation_from_here'),
    })
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
 * The same line, for a caller that must first learn the direction from config.
 *
 * The lookup is allowed to fail and must not take the message down with it: the fork
 * error is the thing the user needs, the direction only refines its advice. `head` is
 * the fallback because it matches the server's own when tail-fork is disabled, so a
 * throw degrades the advice by one word instead of leaving the pane silent -- which
 * would read as a success that opened no tab.
 */
export async function forkFailureMessageForConfig(
  body: string | undefined,
  raw: string,
  cached: { tail_fork_enabled?: boolean } | undefined,
  fetchConfig: () => Promise<{ tail_fork_enabled?: boolean } | undefined>,
): Promise<ForkFailureNotice> {
  let direction: 'head' | 'tail' | 'unknown' = cached?.tail_fork_enabled ? 'tail' : 'head'
  if (!cached) {
    try {
      const fresh = await fetchConfig()
      direction = fresh?.tail_fork_enabled ? 'tail' : 'head'
    } catch {
      // A resolved-but-empty config still SETTLES the direction (the flag defaults off).
      // Only a thrown fetch leaves it unknown, and a guess there inverts the advice.
      direction = 'unknown'
    }
  }
  return {
    message: forkFailureMessageForCode(parseErrorCode(body), raw, direction, 'offsite'),
    report: findReport(raw),
  }
}
