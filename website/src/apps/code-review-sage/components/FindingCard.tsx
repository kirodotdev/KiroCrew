// One finding inside a Focus Report row's expanded detail.
//
// Mirrors sage_lib/report.py's `_finding_html`: a severity-tinted left border,
// a header of the dimension + file:line location, then the model's observation,
// its consequence, an optional code snippet, and a suggestion. All model-authored
// PROSE (observation / consequence / suggestion) renders through MarkdownRenderer;
// the `snippet` is code from a private diff and renders in a <pre> — NEVER through
// the markdown renderer, so it is shown verbatim and can't be reinterpreted as
// markup.
import {
  AlertOctagon, AlertTriangle, Check, CornerDownRight, Loader2, MessageSquarePlus,
} from 'lucide-react'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import type { Finding } from '../lib/types'

import { i18nT } from '../../../i18n/t'
/** Severity → visual treatment. Red is a blocking finding, yellow a should-fix.
 * Anything unset defaults to yellow (the softer of the two). */
function severityVisual(severity?: Finding['severity']) {
  if (severity === 'red') {
    return { Icon: AlertOctagon, text: 'text-danger', border: 'border-l-danger', dot: 'text-danger' }
  }
  return { Icon: AlertTriangle, text: 'text-warn', border: 'border-l-warn', dot: 'text-warn' }
}

export default function FindingCard({
  finding, onPost, posted = false, posting = false,
  selectable = false, selected = false, onToggle, label = '',
}: {
  finding: Finding
  /** Post THIS finding as a single inline comment. Omitted when the run cannot
   *  post (no records left, or a review still running). */
  onPost?: () => void
  posted?: boolean
  posting?: boolean
  /** Batch selection: ticking several and posting them together puts ONE pending
   *  review on the pull request instead of one per comment. */
  selectable?: boolean
  selected?: boolean
  onToggle?: () => void
  /** Accessible name for the checkbox — the card's own text is long. */
  label?: string
}) {
  const { Icon, text, border, dot } = severityVisual(finding.severity)
  const location = [finding.file, finding.line != null && finding.line !== '' ? String(finding.line) : null]
    .filter(Boolean)
    .join(':')

  return (
    <div className={`rounded-lg border border-border bg-bg-elevated overflow-hidden border-l-[3px] ${border} my-2`}>
      <div className="flex items-center gap-1.5 px-3.5 py-2 border-b border-border text-[12.5px]">
        {selectable && !posted && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            aria-label={i18nT('apps.codeReviewSage.components.findingCard.select_to_post',
        { label: label || i18nT('apps.codeReviewSage.components.findingCard.this_finding') })}
            className="mr-0.5 flex-shrink-0 accent-accent cursor-pointer"
          />
        )}
        <Icon size={13} className={`${dot} flex-shrink-0`} aria-hidden="true" />
        {finding.dimension && (
          <span className={`font-semibold ${text}`}>{finding.dimension}</span>
        )}
        {location && (
          <span className="font-mono text-[11.5px] text-muted truncate" title={location}>
            {finding.dimension ? '· ' : ''}{location}
          </span>
        )}
      </div>
      <div className="px-3.5 py-3 text-[13px] leading-relaxed text-text space-y-2">
        {finding.observation && <MarkdownRenderer content={finding.observation} />}
        {finding.consequence && (
          <div className="flex items-start gap-1.5 text-[12.5px] text-muted">
            <CornerDownRight size={13} className="flex-shrink-0 mt-0.5" aria-hidden="true" />
            <span className="min-w-0"><MarkdownRenderer content={finding.consequence} /></span>
          </div>
        )}
        {finding.snippet && (
          // Code from a private diff — rendered verbatim in a monospace <pre>,
          // deliberately NOT through MarkdownRenderer (see file header).
          <pre className="mt-1 overflow-auto rounded-md border border-border bg-bg px-3 py-2 text-[11.5px] leading-relaxed font-mono whitespace-pre-wrap">
            {finding.snippet}
          </pre>
        )}
        {finding.suggestion && (
          <div className="pt-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-accent">
              {i18nT('apps.codeReviewSage.components.findingCard.suggestion')}
            </span>
            <div className="mt-1"><MarkdownRenderer content={finding.suggestion} /></div>
          </div>
        )}
        {/* Per-finding posting: you rarely agree with every finding, so sending
            them one at a time is the normal case rather than an escape hatch.
            Each post is its own pending review on the pull request. */}
        {(posted || onPost) && (
          <div className="pt-1.5">
            {posted ? (
              <span className="inline-flex items-center gap-1.5 text-[11.5px] text-ok">
                <Check size={11} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.findingCard.posted_to_the_pull_request')}
              </span>
            ) : posting ? (
              <span className="inline-flex items-center gap-1.5 text-[11.5px] text-muted">
                <Loader2
                  size={11}
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
                {i18nT('apps.codeReviewSage.components.findingCard.posting')}
              </span>
            ) : (
              <button
                type="button"
                onClick={onPost}
                aria-label={finding.file
              ? i18nT('apps.codeReviewSage.components.findingCard.post_finding_on',
                { target: finding.file })
              : i18nT('apps.codeReviewSage.components.findingCard.post_finding')}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11.5px] text-text hover:text-accent hover:border-accent cursor-pointer"
              >
                <MessageSquarePlus size={11} aria-hidden="true" />
                {i18nT('apps.codeReviewSage.components.findingCard.post_this_comment')}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
