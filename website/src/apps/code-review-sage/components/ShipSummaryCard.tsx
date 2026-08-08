// The ship-readiness comment, shown so you can decide whether to send it.
//
// Every review produces one top-level comment stating the ship / no-ship call and
// its reason. It rides along with "post all", but it is the one comment an author
// most often wants to hold back or send on its own — a verdict on someone's pull
// request reads differently from a line-level note. So it gets the same controls
// as a finding: select it, or post just it.
//
// The body is the EXACT text that will be posted (`row.ship_comment`, built by
// the same `pipeline.build_ship_comment` the poster uses), so what you read here
// is what the author receives.
import { Check, Loader2, MessageSquarePlus, ShipWheel } from 'lucide-react'

import MarkdownRenderer from '../../../components/MarkdownRenderer'

import { i18nT } from '../../../i18n/t'
/** The pending-comment key the backend uses for this entry. */
export const SHIP_KEY = 'design'

export default function ShipSummaryCard({
  body, onPost, posted = false, posting = false,
  selectable = false, selected = false, onToggle,
}: {
  body: string
  onPost?: () => void
  posted?: boolean
  posting?: boolean
  selectable?: boolean
  selected?: boolean
  onToggle?: () => void
}) {
  // Older reports predate `ship_comment` on the row. Rendering a post control for
  // a body we cannot show would be asking the user to send text they never saw.
  if (!body.trim()) return null

  return (
    <div className="rounded-lg border border-border bg-bg-elevated overflow-hidden border-l-[3px] border-l-accent my-2">
      <div className="flex items-center gap-1.5 px-3.5 py-2 border-b border-border text-[12.5px]">
        <ShipWheel size={13} className="flex-shrink-0 text-accent" aria-hidden="true" />
        <span className="font-semibold text-accent">{i18nT('apps.codeReviewSage.components.shipSummaryCard.ship_readiness_summary')}</span>
        <span className="ml-auto text-[11px] text-muted">{i18nT('apps.codeReviewSage.components.shipSummaryCard.posted_as_a_top_level_comment')}</span>
      </div>
      <div className="px-3.5 py-2.5 text-[12.5px] leading-[1.65]">
        <MarkdownRenderer content={body} />
      </div>
      {/* Same footer control group as FindingCard: selecting this comment and
          drafting it are one decision, so the two controls sit together. */}
      {(posted || onPost || (selectable && !posted)) && (
        <div className="flex items-center gap-2 border-t border-border-strong bg-panel-strong px-3.5 py-2">
          {selectable && !posted && (
            <label className="flex items-center gap-2 text-[11.5px] text-muted cursor-pointer">
              <input
                type="checkbox"
                checked={selected}
                onChange={onToggle}
                aria-label={i18nT('apps.codeReviewSage.components.shipSummaryCard.select_the_ship_readiness_summary_to_post')}
                className="flex-shrink-0 accent-accent cursor-pointer"
              />
              {i18nT('apps.codeReviewSage.components.findingCard.select_to_draft_together')}
            </label>
          )}
          <span className="flex-1" />
          <div>
            {posted ? (
              <span className="inline-flex items-center gap-1.5 text-[11.5px] text-ok">
                <Check size={11} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.shipSummaryCard.posted_to_the_pull_request')}
              </span>
            ) : posting ? (
              <span className="inline-flex items-center gap-1.5 text-[11.5px] text-muted">
                <Loader2
                  size={11}
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
                {i18nT('apps.codeReviewSage.components.shipSummaryCard.posting')}
              </span>
            ) : (
              <button
                type="button"
                onClick={onPost}
                aria-label={i18nT('apps.codeReviewSage.components.shipSummaryCard.post_the_ship_readiness_summary')}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11.5px] text-text hover:text-accent hover:border-accent cursor-pointer"
              >
                <MessageSquarePlus size={11} aria-hidden="true" />
                {i18nT('apps.codeReviewSage.components.shipSummaryCard.post_this_comment')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
