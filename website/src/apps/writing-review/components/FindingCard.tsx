// One finding rendered as a card in the detail pane. Includes severity
// label, scanner+rule, the issue text, and the proposed fix.
import type { Confidence, Finding, Severity } from '../lib/types'
import { i18nT } from '../../../i18n/t'
import { resolveScannerName } from '../lib/scannerNames'

const SEVERITY_CLASS: Record<Severity, string> = {
  high: 'bg-danger-subtle text-danger border-danger',
  medium: 'bg-warn-subtle text-warn border-warn',
  low: 'bg-ok-subtle text-ok border-ok',
  advisory: 'bg-bg-elevated text-muted border-muted',
}

const SEVERITY_LABEL_KEY: Record<Severity, string> = {
  high: 'apps.writingReview.findingCard.severity.high',
  medium: 'apps.writingReview.findingCard.severity.medium',
  low: 'apps.writingReview.findingCard.severity.low',
  advisory: 'apps.writingReview.findingCard.severity.advisory',
}

const CONFIDENCE_CLASS: Record<Confidence, string> = {
  high: 'bg-ok-subtle text-ok border-ok',
  medium: 'bg-bg-elevated text-muted border-muted',
  low: 'bg-warn-subtle text-warn border-warn',
}

const CONFIDENCE_LABEL_KEY: Record<Confidence, string> = {
  high: 'apps.writingReview.findingCard.confidence.high',
  medium: 'apps.writingReview.findingCard.confidence.medium',
  low: 'apps.writingReview.findingCard.confidence.low',
}

export default function FindingCard({ finding }: { finding: Finding }) {
  // Old records may deserialise without a confidence field; render a neutral
  // pill using the "medium" default so the layout stays consistent.
  const findingConfidence: Confidence = finding.confidence || 'medium'
  return (
    <div className="flex flex-col gap-2 p-3 border border-border rounded-md bg-card">
      <div className="flex items-center gap-2">
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded-full border text-[11px] font-medium uppercase tracking-wide ${SEVERITY_CLASS[finding.severity]}`}
        >
          {i18nT(SEVERITY_LABEL_KEY[finding.severity])}
        </span>
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded-full border text-[11px] font-medium ${CONFIDENCE_CLASS[findingConfidence]}`}
          title={i18nT(CONFIDENCE_LABEL_KEY[findingConfidence])}
        >
          {i18nT(CONFIDENCE_LABEL_KEY[findingConfidence])}
        </span>
        {finding.cross_validation === 'conflicts' && (
          <span
            className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium bg-warn-subtle text-warn border border-warn"
            title={i18nT('apps.writingReview.findingCard.scannersDisagreeTitle')}
          >
            {i18nT('apps.writingReview.findingCard.scannersDisagree')}
          </span>
        )}
        <span className="text-[12px] text-muted">
          {i18nT('apps.writingReview.findingCard.scannerRule', {
            scanner: resolveScannerName(finding.scanner),
            rule: finding.rule,
          })}
        </span>
        {finding.section && (
          <span className="text-[11.5px] text-muted opacity-70">
            {i18nT('apps.writingReview.findingCard.location', {
              section: finding.section,
              paragraph: finding.paragraph,
            })}
          </span>
        )}
      </div>
      <div className="text-[13px] text-text">{finding.issue}</div>
      {finding.proposed_fix && (
        <div className="text-[12.5px] text-text bg-bg border-l-2 border-accent pl-2 py-1">
          <div className="text-[11px] text-muted mb-0.5 uppercase tracking-wide">
            {i18nT('apps.writingReview.findingCard.proposedFix')}
          </div>
          {finding.proposed_fix}
        </div>
      )}
      {finding.conflicts && finding.conflicts.length > 0 && (
        <div className="text-[11.5px] text-warn">
          {finding.conflicts.map((conflictNote, conflictIndex) => (
            <div key={conflictIndex}>- {conflictNote}</div>
          ))}
        </div>
      )}
      {finding.related_locations && finding.related_locations.length > 0 && (
        <div className="mt-1 pt-2 border-t border-border">
          <div className="text-[11px] text-muted font-medium mb-1">
            {i18nT('apps.writingReview.findingCard.alsoAppearsIn')}
          </div>
          {finding.related_locations.map((relatedLocation, relatedIndex) => (
            <div key={relatedIndex} className="text-[11.5px] text-muted pl-2">
              {'• '}
              {i18nT('apps.writingReview.findingCard.relatedLocation', {
                section: relatedLocation.section,
                paragraph: relatedLocation.paragraph,
                scanner: resolveScannerName(relatedLocation.scanner),
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
