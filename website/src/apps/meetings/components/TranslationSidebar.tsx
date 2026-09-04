// Live translation of the transcript, line by line, beside the meeting.
//
// Both halves of each line are shown: the source above, the translation below.
// That is not redundancy — the panel exists for someone following a meeting held
// in a language they only partly understand, and seeing the two together is what
// lets them check a translation they doubt against what was actually said. It also
// makes a FAILED translation legible: an empty `text` renders as a marked gap
// rather than a line that silently went missing.
//
// Newest last and auto-scrolled, like a transcript rather than a feed: reading
// order matches speaking order.

import { useEffect, useRef } from 'react'
import { Languages, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Badge, Btn, EmptyState } from '../../../components/ui'
import type { TranslationLine } from '../api'

interface Props {
  lines: TranslationLine[]
  /** Endonym for the target language, e.g. `日本語`. Not translated. */
  languageLabel: string
  /** Lines waiting on the model. Shown so a lagging panel does not look broken. */
  pending: number
  /** Lines dropped because the backlog filled. Shown because it is data loss. */
  dropped: number
  loading: boolean
  onClose: () => void
}

export default function TranslationSidebar({
  lines,
  languageLabel,
  pending,
  dropped,
  loading,
  onClose,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  // Follow the tail as lines arrive. Keyed on the LAST line number rather than the
  // array length: trimming old lines server-side changes the length without adding
  // anything new, and scrolling then would yank the view while the user is reading
  // back.
  const lastN = lines.length > 0 ? lines[lines.length - 1].n : -1
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [lastN])

  return (
    // Stacked below `lg` with a bounded height, side-by-side at 340px from `lg`
    // up — the same responsive shape TaskSidebar uses, because a fixed 340px
    // column beside the meeting clips the panel inside a 320px viewport.
    <aside
      className="flex-none w-full h-[42%] min-h-[260px] border-t border-border lg:h-full lg:w-[340px] lg:border-t-0 lg:border-l bg-bg flex flex-col overflow-hidden"
      aria-label={i18nT('apps.meetings.translation.title')}
    >
      <div className="flex-none px-3 py-2.5 border-b border-border flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Languages className="lucide-inline text-muted" />
          <span className="text-[13px] font-semibold text-text-strong truncate">
            {i18nT('apps.meetings.translation.title')}
          </span>
          {/* The language's own endonym, so it is readable to whoever wants it. */}
          <Badge variant="muted">{languageLabel}</Badge>
        </div>
        <Btn onClick={onClose} aria-label={i18nT('apps.meetings.translation.close')}>
          <X className="lucide-inline" />
        </Btn>
      </div>

      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-3">
        {lines.length === 0 ? (
          <EmptyState
            icon={<Languages className="lucide-inline" />}
            title={i18nT('apps.meetings.translation.empty')}
            subtitle={
              loading
                ? i18nT('apps.meetings.translation.loading')
                : i18nT('apps.meetings.translation.emptyHint')
            }
          />
        ) : (
          lines.map(line => (
            <div key={line.n} className="flex flex-col gap-1">
              <p className="text-[12px] text-muted leading-snug">{line.source}</p>
              {line.text ? (
                <p className="text-[13px] text-text leading-snug">{line.text}</p>
              ) : (
                <p className="text-[13px] text-muted italic leading-snug">
                  {i18nT('apps.meetings.translation.lineFailed')}
                </p>
              )}
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>

      {(pending > 0 || dropped > 0) && (
        <div className="flex-none px-3 py-2 border-t border-border text-[12px] text-muted flex items-center justify-between gap-2">
          {/* Translation runs one line at a time behind live speech, so a backlog is
              normal rather than a fault — saying so stops the panel looking stuck. */}
          <span>{pending > 0 ? i18nT('apps.meetings.translation.pending') : ''}</span>
          {dropped > 0 && (
            <span className="text-danger">{i18nT('apps.meetings.translation.dropped')}</span>
          )}
        </div>
      )}
    </aside>
  )
}
