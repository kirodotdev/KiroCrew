import { memo } from 'react'

import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

/** The synthetic row kind that marks where a slot's current conversation begins.
 *
 *  Not a gateway-authored row: the transcript holds no such message, because a
 *  reset deliberately writes nothing into it. The pane splices this row in at
 *  the boundary the member log recorded, so the one tag lives beside the row
 *  that carries it and the renderer entry cannot drift from the splice. */
export const CONVERSATION_BOUNDARY_KIND = 'conversation_boundary'

/**
 * The line between a discarded conversation and the current one.
 *
 * Drawn as a ROW rather than as a header above the transcript, because the
 * boundary moves: with the earlier messages withheld it is the top of the drawn
 * list, and with them revealed it sits in the middle. A control above the rows
 * is in the right place only in the first case, which would leave a reader who
 * expanded the history with an undivided transcript and no way to tell which
 * half the model remembers.
 *
 * It carries the reveal, so one element is both the marker and the control: a
 * separate button that disappeared on press would take the marker with it.
 * `earlierHidden` is how many rows are behind it; 0 means they are on screen and
 * the control collapses them again.
 *
 * The moment is formatted through `fmtDateTime`, which follows the APP's
 * language rather than the browser's: a bare `toLocaleString()` renders an
 * English date inside a translated line, one word away from a label this row
 * takes from the catalog.
 */
const ConversationBoundaryRow = memo(function ConversationBoundaryRow({
  at,
  earlierHidden,
  onToggle,
}: {
  /** The boundary's own instant, epoch ms, as the projection recorded it. */
  at: number
  /** Rows withheld behind this line; 0 when they are already drawn. */
  earlierHidden: number
  onToggle: () => void
}) {
  // memo() boundary rendering i18nT() strings: subscribe so a language switch repaints.
  useLanguageGeneration()
  const when = Number.isFinite(at) && at > 0 ? new Date(at) : null
  return (
    <div
      className="flex items-center gap-3 px-4 py-3 select-none"
      data-testid="conversation-boundary-row"
    >
      {/* The label group WRAPS rather than holding one line: on a 320px pane the
          label, the full timestamp and the control do not fit side by side, and a
          non-wrapping group is clipped by the transcript's own hidden horizontal
          overflow -- taking the control with it. `flex-1 min-w-0` lets the rules
          give up their width to the group first, and they are hidden below sm,
          where there is none to spend on decoration. */}
      <span className="hidden sm:block h-px flex-1 min-w-0 bg-border" aria-hidden />
      <span className="flex flex-wrap items-center justify-center gap-x-2 gap-y-0.5 min-w-0 text-[12px] text-muted">
        <span>{i18nT('components.chatPane.conversation_boundary')}</span>
        {/* `dateTime` stays the ISO instant: that attribute is machine-read,
            so it must not follow anybody's locale. The visible text does. */}
        {when && (
          <time dateTime={when.toISOString()} data-testid="conversation-boundary-time">
            {fmtDateTime(when)}
          </time>
        )}
        <button
          type="button"
          onClick={onToggle}
          className="text-accent underline bg-transparent border-none p-0 cursor-pointer hover:text-accent-hover transition-colors focus-ring"
          data-testid={earlierHidden > 0 ? 'chat-pane-show-earlier' : 'chat-pane-hide-earlier'}
        >
          {earlierHidden > 0
            ? i18nT('components.chatPane.show_earlier_messages')
            : i18nT('components.chatPane.hide_earlier_messages')}
        </button>
      </span>
      <span className="hidden sm:block h-px flex-1 min-w-0 bg-border" aria-hidden />
    </div>
  )
})

export default ConversationBoundaryRow
