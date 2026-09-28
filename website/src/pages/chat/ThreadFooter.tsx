/**
 * The Slack-style footer under a bubble that has a reply thread: the faces of
 * who took part, "N replies" in accent, "Last reply 2h ago" muted. One click
 * opens the thread. Drawn only for a message whose `mid` has replies; a bubble
 * with none shows nothing here (its "Reply in thread" action lives in the hover
 * row, next to Copy).
 */
import { useTranslation } from 'react-i18next'
import { UserRound } from 'lucide-react'
import CrewAvatar from '../../components/CrewAvatar'
import { fmtRelative } from '../../i18n/format'
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
  /** `end` under the user's right-aligned bubble. */
  align?: 'start' | 'end'
}) {
  const { t } = useTranslation()
  const count = summary.count
  const label = t('pages.chat.thread.replies_count', { count })
  return (
    <button
      type="button"
      data-testid="thread-footer"
      onClick={onOpen}
      className={`mt-1 inline-flex items-center gap-2 px-1.5 py-1 rounded-md text-[12px] leading-5 hover:bg-bg-hover cursor-pointer ${align === 'end' ? 'self-end -mr-1.5' : 'self-start -ml-1.5'}`}
      aria-label={t('pages.chat.thread.open_thread')}
    >
      <span className="inline-flex items-center gap-0.5">
        {summary.participants.map((role) =>
          role === 'assistant'
            ? <CrewAvatar key={role} seed={crewmateName} size={FACE_PX} className="rounded-full" />
            : <UserFace key={role} />,
        )}
      </span>
      <span className="text-accent font-medium">{label}</span>
      {summary.last_reply_ts && (
        <span className="text-muted">{t('pages.chat.thread.last_reply', { when: fmtRelative(summary.last_reply_ts) })}</span>
      )}
    </button>
  )
}
