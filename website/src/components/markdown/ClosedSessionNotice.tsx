import { useContext } from 'react'
import { createPortal } from 'react-dom'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { ParagraphNoticeSlotCtx } from './contexts'
import type { ClosedSessionFailure } from './linkTargets'

/**
 * The notice a closed-session chip or link shows when the reader clicked it and
 * the session could not be checked. Raised only by that click, never by the
 * background probe, so reading past a link raises nothing. Retry asks again and
 * opens the session when it is there, and the notice goes with it. Two actions
 * only (the hand-off and Try again), so no dismiss control.
 *
 * WHERE it renders: below the paragraph, in the slot `MdParagraph` provides
 * (`ParagraphNoticeSlotCtx`), so the sentence that names the session stays
 * whole whether the reference ends it or sits in its middle. Without a slot (a
 * tight list item, a heading, a table cell) it renders in place, as a line of
 * its own after the reference's trailing punctuation, which the component
 * draws before this notice. Block-level either way (`flex`, as a `span` so it
 * is legal inside a `<p>`).
 */
/** `name` is the session the chip or link names, as the reader sees it in the notice. */
export function ClosedSessionNotice({ failure, name }: { failure?: ClosedSessionFailure; name: string }) {
  const slot = useContext(ParagraphNoticeSlotCtx)
  if (!failure?.shown) return null
  const notice = (
    // Narrow first: inside the message width it wraps, so the actions are never
    // clipped. From `sm` up it holds one line.
    <span className="my-1 flex max-w-full flex-wrap items-center gap-x-1.5 gap-y-0.5 sm:flex-nowrap sm:whitespace-nowrap" data-closed-session-notice="">
      {/* askAgent on: a transcript chip or link holds no draft; the host
          composer's draft is persisted per slot. */}
      <ErrorNotice
        variant="inline"
        message={i18nT('components.markdownRenderer.closed_session_open_failed', { name })}
        askAgent
        className="min-w-0 max-w-full flex-wrap sm:flex-nowrap"
      />
      <button
        type="button"
        className="bg-transparent border-none p-0 cursor-pointer text-[12px] font-medium text-accent hover:underline disabled:opacity-50 disabled:cursor-default"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); failure.retry() }}
        disabled={failure.busy}
        aria-busy={failure.busy || undefined}
      >
        {i18nT('components.markdownRenderer.closed_session_try_again')}
      </button>
    </span>
  )
  return slot ? createPortal(notice, slot) : notice
}
