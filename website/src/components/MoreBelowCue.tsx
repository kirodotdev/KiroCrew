import { ChevronDown } from 'lucide-react'
import { i18nT } from '../i18n/t'

interface MoreBelowCueProps {
  /** Rows sit below the fold. When false the strip keeps its height and only
   *  its text hides, so reaching the last row does not resize the list. */
  visible: boolean
  testId: string
}

/** A "More below" label in its own strip under a list that scrolls inside a
 *  fixed-height box, so the list reads as clipped rather than complete. A label,
 *  not a control: decorative, since every row it points at is already in the tab
 *  order and the accessibility tree. Accent text and a hairline because a muted
 *  caption was easy to miss. */
export function MoreBelowCue({ visible, testId }: MoreBelowCueProps) {
  return (
    <div
      aria-hidden="true"
      data-testid={testId}
      className={`shrink-0 flex items-center justify-center gap-1 pt-1.5 pb-1 border-0 border-t border-solid border-border text-[12px] font-medium text-accent select-none ${visible ? '' : 'invisible'}`}
    >
      {i18nT('components.sidePanelLayout.more_below')}
      <ChevronDown size={14} />
    </div>
  )
}
