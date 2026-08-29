// Detail pane: verdict header + list of findings grouped by severity.
import { AlertTriangle, MessageSquare } from 'lucide-react'

import { useChatLauncher } from '../../../app-sdk'
import { i18nT } from '../../../i18n/t'
import { useWritingReview } from '../context'
import { writingReviewApi } from '../api'
import FindingCard from './FindingCard'
import VerdictBadge from './VerdictBadge'
import { resolveScannerName } from '../lib/scannerNames'
import {
  buildReviewChatFallbackMessage,
  buildReviewChatMessage,
  reasonLabel,
} from '../lib/reviewChatHandoff.prompt'
import type { Finding, Severity } from '../lib/types'

const SEVERITY_ORDER: Severity[] = ['high', 'medium', 'low', 'advisory']

/**
 * ``Severity`` bucket -> full literal i18n key. Written out per bucket rather
 * than assembled with a template so ``dynamicKeys.test.ts`` can see the keys
 * and ``deadKeys.test.ts`` doesn't flag them as unreferenced. Same pattern as
 * ``AboutPanel``'s ``UPDATE_ERROR_KEYS`` and ``McpToolsPanel``'s
 * ``STATUS_LABEL_KEY`` (see comments there for why assembly kills tooling).
 */
const SEVERITY_HEADING_KEYS: Record<Severity, string> = {
  high: 'apps.writingReview.findingCard.severity.high',
  medium: 'apps.writingReview.findingCard.severity.medium',
  low: 'apps.writingReview.findingCard.severity.low',
  advisory: 'apps.writingReview.findingCard.severity.advisory',
}

export default function ReviewDetail() {
  const { reviewDetailQuery } = useWritingReview()
  const { openChat } = useChatLauncher()

  if (reviewDetailQuery.isLoading) {
    return (
      <div className="p-6 text-[12px] text-muted">
        {i18nT('apps.writingReview.reviewDetail.loading')}
      </div>
    )
  }
  if (reviewDetailQuery.isError || !reviewDetailQuery.data) {
    return (
      <div className="p-6 text-[12px] text-danger">
        {i18nT('apps.writingReview.reviewDetail.error')}
      </div>
    )
  }

  const reviewDetail = reviewDetailQuery.data
  const findingsBySeverity: Record<Severity, Finding[]> = {
    high: [],
    medium: [],
    low: [],
    advisory: [],
  }
  for (const finding of reviewDetail.findings) {
    findingsBySeverity[finding.severity]?.push(finding)
  }

  const openChatAboutThisReview = async () => {
    // Frontend pre-fetch handoff: the writing-review-reviewer agent has no HTTP or
    // file-read tools, so we pack the entire context (findings + failed
    // scanners + full doc content) into the first chat message. Falls back
    // to a review_id-only marker if the context fetch fails, so the chat
    // still opens and the agent can at least acknowledge the id.
    let firstMessage = buildReviewChatFallbackMessage(reviewDetail.id)
    try {
      const contextBundle = await writingReviewApi.getReviewContext(reviewDetail.id)
      firstMessage = buildReviewChatMessage(contextBundle)
    } catch {
      // Non-fatal: use the compact marker.
    }
    openChat({
      agent: 'writing-review-reviewer',
      message: firstMessage,
    })
  }

  return (
    <div className="flex flex-col min-h-0 flex-1">
      <header className="p-4 border-b border-border flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <h2 className="text-[15px] font-medium text-text truncate">
            {reviewDetail.doc_name}
          </h2>
          <div className="text-[11.5px] text-muted">
            {i18nT('apps.writingReview.reviewDetail.subheader', {
              count: reviewDetail.findings.length,
              scanners: reviewDetail.scanners_run.length,
            })}
          </div>
          {reviewDetail.context.ask && (
            <div
              className="mt-1 text-[11.5px] text-muted italic truncate"
              title={reviewDetail.context.ask}
            >
              {i18nT('apps.writingReview.reviewDetail.askLabel', {
                ask: reviewDetail.context.ask,
              })}
            </div>
          )}
        </div>
        <VerdictBadge verdict={reviewDetail.verdict} />
        <button
          type="button"
          onClick={openChatAboutThisReview}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border text-[12px] text-text hover:bg-bg-hover"
        >
          <MessageSquare className="lucide-inline" aria-hidden="true" />
          {i18nT('apps.writingReview.reviewDetail.chatAbout')}
        </button>
      </header>
      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
        {reviewDetail.partial_failure && reviewDetail.failed_scanners.length > 0 && (
          <section className="rounded-md border border-warn bg-warn-subtle p-3 space-y-2">
            <div className="flex items-center gap-2 text-[12.5px] text-warn font-medium">
              <AlertTriangle className="lucide-inline" aria-hidden="true" />
              {i18nT('apps.writingReview.reviewDetail.partialFailure')}
            </div>
            <ul className="space-y-1.5 text-[12px] text-text">
              {reviewDetail.failed_scanners.map(failedScanner => (
                <li key={failedScanner.name} className="flex items-baseline gap-2">
                  <span className="font-medium">{resolveScannerName(failedScanner.name)}</span>
                  <span className="text-muted">-</span>
                  <span>{reasonLabel(failedScanner)}</span>
                  {failedScanner.duration_ms > 0 && (
                    <span className="text-[11px] text-muted">
                      {i18nT('apps.writingReview.reviewDetail.failedScanner.duration', {
                        ms: failedScanner.duration_ms,
                      })}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
        {SEVERITY_ORDER.map(severityBucket => {
          const findingsInBucket = findingsBySeverity[severityBucket]
          if (findingsInBucket.length === 0) return null
          return (
            <section key={severityBucket} className="space-y-2">
              <h3 className="text-[11.5px] text-muted uppercase tracking-wide">
                {i18nT(SEVERITY_HEADING_KEYS[severityBucket])}
              </h3>
              <div className="flex flex-col gap-2">
                {findingsInBucket.map(finding => (
                  <FindingCard key={finding.id} finding={finding} />
                ))}
              </div>
            </section>
          )
        })}
        {reviewDetail.findings.length === 0 && (
          <div className="text-[12.5px] text-muted">
            {i18nT('apps.writingReview.reviewDetail.noFindings')}
          </div>
        )}
      </div>
    </div>
  )
}
