/**
 * The Slack-style footer under a bubble that has a thread. One click opens it.
 *
 * Two kinds, because two eras of thread hang off the same map (`summarize`):
 *
 *  - a LIVE thread is an ordinary session anchored here, so there is no reply
 *    count to show and nothing to count: "Thread" plus its title, and "Closed"
 *    once it has been wrapped up. A thread opened a second ago says exactly
 *    what a thread with forty turns says, which is the honest reading — the
 *    number of turns in someone else's session is not this row's business.
 *  - a VERSION 1 thread is a set of replies in the sidecar, so it says what it
 *    always said: the faces of who took part, "N replies", "Last reply 2h ago".
 *
 * Drawn only for a message that HAS one. A bubble with no thread shows nothing
 * here; its "Reply in thread" action lives in the hover row, next to Copy.
 */
import { useTranslation } from 'react-i18next'
import { MessageSquare, UserRound } from 'lucide-react'
import CrewAvatar from '../../components/CrewAvatar'
import { fmtRelative } from '../../i18n/format'
import { threadTitleBeside } from './threadTitle'
import type { ThreadSummary } from '../../api/threads'

const FACE_PX = 18

/** The user's face beside a reply. The product has no user avatar, so this is a
 *  quiet glyph in a disc sized like the crewmate's. */
function UserFace() {
  return (
    <span
      className="inline-flex items-center justify-center rounded-full bg-bg-hover border border-border text-muted shrink-0"
      style={{ width: FACE_PX, height: FACE_PX }}
      aria-hidden="true"
    >
      <UserRound className="lucide-inline" style={{ width: 11, height: 11 }} />
    </span>
  )
}

export default function ThreadFooter({ summary, crewmateName, onOpen, align = 'start' }: {
  summary: ThreadSummary
  crewmateName: string
  onOpen: () => void
  /** `end` under the user's right-aligned bubble. Rendered as `align-self`, so
   *  a mode that re-aligns the ROW must re-state it here too: CLI mode moves
   *  the user's bubble to the left and re-aligns this footer in
   *  `styles/cli-mode.css`. Do not drop `self-start` from the `start` branch —
   *  the assistant footer's column is `align-items: stretch`, and that class is
   *  what keeps the button shrink-wrapped instead of full-width. */
  align?: 'start' | 'end'
}) {
  const { t } = useTranslation()
  const live = summary.kind === 'session'
  return (
    <button
      type="button"
      data-testid="thread-footer"
      data-thread-kind={summary.kind}
      onClick={onOpen}
      className={`mt-1 inline-flex items-center gap-2 px-1.5 py-1 rounded-md text-[12px] leading-5 hover:bg-bg-hover cursor-pointer ${align === 'end' ? 'self-end -mr-1.5' : 'self-start -ml-1.5'}`}
      aria-label={t('pages.chat.thread.open_thread')}
    >
      {live ? (
        <>
          <MessageSquare className="lucide-inline text-accent shrink-0" style={{ width: 13, height: 13 }} aria-hidden="true" />
          <span className="text-accent font-medium">{t('pages.chat.thread.title')}</span>
          {/* Stripped of a "Thread:" prefix the chip's own label already
              supplies, so an untitled thread does not read "Thread  Thread:
              Amazon" on the one surface that shows both at once. */}
          {threadTitleBeside(summary.title) && (
            <span className="text-muted truncate max-w-[220px]">{threadTitleBeside(summary.title)}</span>
          )}
          {summary.closed_at && <span className="text-muted">{t('pages.chat.thread.closed')}</span>}
        </>
      ) : (
        <>
          <span className="inline-flex items-center gap-0.5">
            {summary.participants.map((role) =>
              role === 'assistant'
                ? <CrewAvatar key={role} seed={crewmateName} size={FACE_PX} className="rounded-full" />
                : <UserFace key={role} />,
            )}
          </span>
          <span className="text-accent font-medium">{t('pages.chat.thread.replies_count', { count: summary.count })}</span>
          {summary.last_reply_ts && (
            <span className="text-muted">{t('pages.chat.thread.last_reply', { when: fmtRelative(summary.last_reply_ts) })}</span>
          )}
        </>
      )}
    </button>
  )
}
