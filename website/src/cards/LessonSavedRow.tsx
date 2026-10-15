/**
 * One line in the chat for a lesson the agent just saved (`learn_add`).
 *
 * A lesson is injected into every later session, so the gateway records each
 * write that changed the store as its own `card` row in the conversation that
 * made it (`dashboard/chat_cards.record_lesson_saved`): one row per lesson, the
 * way each tool permission is its own prompt. The row is already applied, so it
 * has nothing to press but a link to the Memory page, where the lessons are
 * reviewed and deleted. Drawn centred and quiet like `SelfChangeRow`, its
 * sibling for a change the agent already made.
 *
 * The link is a plain route change: the composer's unsent draft is kept per
 * slot across navigation (`utils/chatDrafts`), so leaving loses nothing.
 */
import { BookOpen } from 'lucide-react'
import { Link } from 'react-router-dom'
import { i18nT } from '../i18n/t'

export interface LessonSavedRef {
  surface: 'lesson'
  id: string
  slot: string
  /** The rule as the gateway recorded it: redacted, single-line, bounded. */
  rule: string
  /** `updated` for a write that enriched a stored lesson, else `saved`. */
  status: 'saved' | 'updated'
}

/** Where lessons are reviewed: the Memory view, landed on its lessons
 *  (`focus=lessons`): the Lessons card, or the Semantic browser filtered to
 *  `lesson.` rows when vector memory keeps them (pages/overview/MemoryTab). */
export const LESSONS_ROUTE = '/settings/overview?view=memory&focus=lessons'

export default function LessonSavedRow({ lesson }: { lesson: LessonSavedRef }) {
  const text = i18nT(
    lesson.status === 'updated' ? 'components.lessonSaved.updated' : 'components.lessonSaved.saved',
    { rule: lesson.rule },
  )
  return (
    <div className="flex flex-col items-center" data-testid="lesson-saved-row" data-lesson-status={lesson.status}>
      <div className="inline-flex max-w-full items-center gap-2.5 rounded-full border border-border bg-bg-elevated px-3 py-1.5 text-[12px] text-muted">
        <BookOpen size={12} className="lucide-inline shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate" title={text} data-testid="lesson-saved-text">{text}</span>
        <Link
          to={LESSONS_ROUTE}
          className="shrink-0 rounded-full px-1.5 py-0.5 text-accent hover:bg-accent-subtle"
          data-testid="lesson-saved-review"
        >
          {i18nT('components.lessonSaved.review')}
        </Link>
      </div>
    </div>
  )
}
