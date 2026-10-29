import { memo } from 'react'
import { Info, Users } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { openPanelView } from '../../hooks/usePanelTabs'

/**
 * The gateway's one-per-turn Crew Mode dispatch notice
 * (`meta.kind === 'crew_dispatch_notice'`,
 * `src/kiro_crew/dashboard/crew_dispatch_notice.py`).
 *
 * In Crew Mode a dispatched crew member is listed in the Crew (Workers) tab of
 * the side panel, not in the session list, so after the conductor starts one the
 * person otherwise sees only a folded tool line. This row names that tab and
 * carries a button that opens it. The gateway writes exactly one per person turn
 * and never on a plain chat, a failed dispatch or a crew member's own
 * sub-dispatch, so this component just renders — it decides nothing about when to
 * appear.
 *
 * Shares `NoticeCard`'s visual grammar (the same ring, card background, radius,
 * padding, 13px/leading-5 type and a 13px lucide icon in a gap-2 leading slot) so
 * it reads as the same family of routine gateway notices, with the info glyph and
 * a trailing action button. The row's English `content` is only a fallback for
 * channel mirrors and SSE readers; here the copy comes from the catalog.
 */
export default memo(function CrewDispatchNoticeCard({ slot }: { slot?: string | null }) {
  // This memo() boundary renders i18nT() strings, so a language switch must
  // invalidate it (the same subscription NoticeCard uses).
  useLanguageGeneration()
  const openCrewTab = i18nT('pages.chat.crewDispatchNotice.openCrewTab')
  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="crew-dispatch-notice-card"
    >
      <div className="flex items-start gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">
        <Info
          size={13}
          className="lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)]"
          aria-hidden="true"
        />
        <div className="min-w-0 flex flex-col gap-1.5">
          <span className="min-w-0 break-words" data-testid="crew-dispatch-notice-text">
            {i18nT('pages.chat.crewDispatchNotice.text')}
          </span>
          <div>
            <button
              type="button"
              onClick={() => openPanelView(slot ?? null, 'workers')}
              title={openCrewTab}
              aria-label={openCrewTab}
              className="pi-morph inline-flex items-center gap-1 text-[11px] leading-4 text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors"
              data-testid="crew-dispatch-notice-open"
            >
              <Users size={13} />
              <span>{openCrewTab}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  )
})
