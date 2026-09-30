/**
 * The row a thread leaves behind in its parent when it ends.
 *
 * The gateway writes it as an ordinary `assistant` row carrying
 * `meta.thread_summary: {thread_slot, title}` -- so it persists, rewinds and
 * exports like every other row -- and `thread_slot` is the back-link. Drawn as a
 * card rather than as the row's own text for two reasons: the text reads as the
 * crewmate saying "Thread ended.", which the crewmate never said, and the
 * back-link is the whole point of recording the slot, so it has to be pressable.
 *
 * It carries no written summary because nothing composes one. The card states
 * what happened, names the thread, and opens it.
 */
import { memo } from 'react'
import { MessageSquare } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { threadTitleBeside } from './threadTitle'

export default memo(function ThreadClosedCard({ title, onOpen }: {
  title: string
  /** Absent when the anchor that named this slot is gone: the card still states
   *  the close, and nothing is offered that would open a thread it cannot find. */
  onOpen?: () => void
}) {
  const { t } = useTranslation()
  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="thread-closed-card"
    >
      <div className="flex items-start gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">
        <MessageSquare
          size={13}
          className="lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)]"
          aria-hidden="true"
        />
        <span className="min-w-0 break-words flex flex-wrap items-baseline gap-x-1.5">
          <span>{t('pages.chat.thread.card_ended')}</span>
          {/* Stripped of a "Thread:" prefix the card's own heading already says,
              so an untitled thread's card does not read "Thread ended. Thread:
              Ready." -- the same strip the footer chip and the drawer header do. */}
          {threadTitleBeside(title) && (
            <span className="text-text truncate max-w-[260px]">{threadTitleBeside(title)}</span>
          )}
          {onOpen && (
            <button
              type="button"
              data-testid="thread-closed-card-open"
              onClick={onOpen}
              className="text-accent font-medium hover:underline cursor-pointer"
            >
              {t('pages.chat.thread.card_read')}
            </button>
          )}
        </span>
      </div>
    </div>
  )
})
