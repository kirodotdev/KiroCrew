import { memo } from 'react'
import { Users } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

/** `meta.kind` of the gateway's Crew Mode dispatch hint
 *  (`session_control_parts/crew_dispatch_hint.py`): one `notice` row in a Crew
 *  Mode conductor's chat per person turn in which it started a worker. */
export const CREW_DISPATCH_HINT_KIND = 'crew_dispatch_hint'

export function isCrewDispatchHint(m: ChatMessage): boolean {
  return m.role === 'notice' && m.meta?.kind === CREW_DISPATCH_HINT_KIND
}

/** The dispatch hint: where the conductor's workers are listed, in the person's
 *  language, with a button that opens the side panel on the Crew tab. A host
 *  that cannot open the panel for this transcript passes no `onOpen` and gets
 *  the text alone. Same frame as `NoticeCard`. */
export default memo(function CrewDispatchHintCard({ onOpen }: { onOpen?: () => void }) {
  // This memo() boundary renders i18nT() strings, so a language switch must
  // invalidate it.
  useLanguageGeneration()
  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="crew-dispatch-hint"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 min-w-0 text-[13px] leading-5">
        <span className="flex items-start gap-2 min-w-0 flex-1">
          <Users size={13} className="lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)]" aria-hidden="true" />
          <span className="min-w-0 break-words">{i18nT('commandCenter.dispatch_hint')}</span>
        </span>
        {onOpen && (
          <button
            type="button"
            onClick={onOpen}
            className="shrink-0 flex items-center gap-1 text-[12px] leading-4 text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors"
            data-testid="crew-dispatch-hint-open"
          >
            {i18nT('commandCenter.dispatch_hint_open')}
          </button>
        )}
      </div>
    </div>
  )
})
