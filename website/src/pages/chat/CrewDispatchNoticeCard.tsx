import { memo } from 'react'
import { Info, Users } from 'lucide-react'

import { useCrewMode } from '../../hooks/usePreviewFlag'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

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
export default memo(function CrewDispatchNoticeCard({
  slot,
  onOpenCrewTab,
}: {
  slot?: string | null
  onOpenCrewTab?: (slot: string) => void
}) {
  // Crew Mode ships behind a default-off feature preview
  // (docs/decisions/2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md).
  // The gateway writes this notice whenever a session is genuinely a turned-on
  // Crew Mode root (`crew_dispatch_notice.py`), which is server state and does
  // not know a given browser's preview flag. A browser with the preview OFF
  // must not render the row at all: it would expose a Crew Mode surface, claim
  // workers are absent from the session list, and offer a button whose target
  // panel the preview withholds. Gate on the reactive flag and render nothing
  // while off; the renderer match is retained upstream so the row never falls
  // back to a generic notice.
  const crewMode = useCrewMode()
  // This memo() boundary renders i18nT() strings, so a language switch must
  // invalidate it (the same subscription NoticeCard uses).
  useLanguageGeneration()
  if (!crewMode) return null
  const openCrewTab = i18nT('pages.chat.crewDispatchNotice.openCrewTab')
  // Revealing the Workers tab is the HOST's job, not the card's: it must close
  // the find pane (which keeps the side-panel dock unmounted while open) and,
  // in a split pane, rebind this card's slot to the active one before opening
  // the panel for it — neither reachable from here. The card used to call
  // openPanelView + openActivityPanel itself, which left both cases broken
  // (GPT 6.1 / Opus 5.5). Now it calls the single host-owned reveal
  // (`handleOpenCrewTab` in useChatPageResourcesController) threaded through the
  // renderer. A host that wires none (a side/embedded chat, a crew window)
  // leaves the button inert rather than half-revealing.
  const revealCrewTab = onOpenCrewTab && slot ? () => onOpenCrewTab(slot) : undefined
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
              onClick={revealCrewTab}
              disabled={!revealCrewTab}
              className="pi-morph inline-flex items-center gap-1 text-[11px] leading-4 text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors disabled:opacity-50 disabled:cursor-default"
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
