// Model-facing handoff prompt builder for the writing-review-reviewer agent.
//
// This module composes the first-turn chat message the writing-review-reviewer
// LLM receives when a user clicks "Discuss with the writing reviewer"
// on a completed review. Every literal in it is a WIRE-CONTRACT field
// name the agent parses by exact byte-for-byte match -- ``review_id:``,
// ``doc_name:``, ``scanners_run:``, ``partial_failure:``,
// ``failed_scanners:``, ``findings (N):``, ``Extracted document
// content (from source docx):``, ``[REVIEW CONTEXT]``, and the
// ``Use fs_read on ... to load the document when a user asks about a
// specific passage.`` handoff instruction. Translating any of them
// would break the agent's ability to reason about the review's
// contents -- the string is not user-visible copy, it's a machine
// contract with the model.
//
// Extracted from ``../components/ReviewDetail.tsx`` (2026-08-29) so the
// ``.prompt.ts`` suffix -- already a named boundary in
// ``eslint.i18n.config.js`` (``src/**/*.prompt.ts``) for model-facing
// prompts, with the same rationale ``AGENTS.md``'s router points at --
// covers it without a new allowlist entry. The consuming component
// still renders all its user-visible copy through ``i18nT()`` and
// stays fully gated by the i18n checks.

import { i18nT } from '../../../i18n/t'
import type { FailedScanner, ReviewContextBundle, ReviewDetail } from './types'

const REASON_LABEL_KEY: Record<string, string> = {
  provider_timeout: 'apps.writingReview.reviewDetail.failedScanner.provider_timeout',
  invalid_json: 'apps.writingReview.reviewDetail.failedScanner.invalid_json',
  truncated_response: 'apps.writingReview.reviewDetail.failedScanner.truncated_response',
  missing_brief: 'apps.writingReview.reviewDetail.failedScanner.missing_brief',
  rate_limited: 'apps.writingReview.reviewDetail.failedScanner.rate_limited',
  worker_died: 'apps.writingReview.reviewDetail.failedScanner.worker_died',
  other: 'apps.writingReview.reviewDetail.failedScanner.other',
}

// Resolve a failed-scanner's ``reason_class`` to the localized label
// used inside the ``failed_scanners:`` block. Kept co-located with
// ``buildReviewChatMessage`` because it is only called from there;
// the ``ReviewDetail`` component's failed-scanner list uses its own
// direct ``i18nT`` calls against the same key table.
export function reasonLabel(failedScanner: FailedScanner): string {
  const reasonKey = REASON_LABEL_KEY[failedScanner.reason_class] || REASON_LABEL_KEY.other
  return i18nT(reasonKey)
}

/**
 * Build the rich first-turn message the writing-review-reviewer agent sees.
 *
 * Includes the review summary, every finding with its metadata, and the
 * full document text. This is the frontend pre-fetch handoff pattern --
 * the agent has no HTTP fetch or file-read tool, so we pack everything
 * it needs to reason about the review into the chat message itself.
 */
export function buildReviewChatMessage(bundle: ReviewContextBundle): string {
  const reviewData: ReviewDetail = bundle.review
  const lines: string[] = []
  lines.push('[REVIEW CONTEXT]')
  lines.push('')
  lines.push(`review_id: ${reviewData.id}`)
  lines.push(`doc_name: ${reviewData.doc_name}`)
  lines.push(`doc_path: ${reviewData.doc_path}`)
  lines.push(`verdict: ${reviewData.verdict}`)
  lines.push(`scanners_run: ${reviewData.scanners_run.join(', ')}`)
  lines.push(`partial_failure: ${reviewData.partial_failure}`)
  if (reviewData.context.ask) {
    // Surface the author's directive so the agent's opening turn can
    // frame its response around the specific decision the user is
    // asking about. Silent when absent -- the empty-ask path leaves
    // the handoff clean rather than emitting an "ask: (none)" line.
    lines.push(`ask: ${reviewData.context.ask}`)
  }

  if (reviewData.failed_scanners && reviewData.failed_scanners.length > 0) {
    lines.push('')
    lines.push('failed_scanners:')
    for (const failedScanner of reviewData.failed_scanners) {
      // Duration is emitted as ``duration_ms=NNN`` (wire-style
      // key=value) rather than ``NNNms`` so the i18n unit-literal
      // gate does not misread this machine-facing prompt as
      // user-visible copy that concatenates a number with a
      // hardcoded unit. The trailing ``_ms`` in the key itself
      // records the unit for the agent parser.
      lines.push(
        `  - ${failedScanner.name} (reason: ${failedScanner.reason_class}, duration_ms=${failedScanner.duration_ms}): ${failedScanner.message}`,
      )
    }
  }

  lines.push('')
  lines.push(`findings (${reviewData.findings.length}):`)
  for (let findingIndex = 0; findingIndex < reviewData.findings.length; findingIndex += 1) {
    const finding = reviewData.findings[findingIndex]
    lines.push('')
    lines.push(
      `${findingIndex + 1}. [${finding.severity}] ${finding.scanner} Rule ${finding.rule} — ${finding.section || '(no section)'}, paragraph ${finding.paragraph}`,
    )
    lines.push(`   confidence: ${finding.confidence || 'medium'}`)
    lines.push(`   issue: ${finding.issue}`)
    if (finding.proposed_fix) {
      lines.push(`   proposed_fix: ${finding.proposed_fix}`)
    }
    if (finding.cross_validation && finding.cross_validation !== 'clean') {
      lines.push(`   cross_validation: ${finding.cross_validation}`)
    }
    if (finding.conflicts && finding.conflicts.length > 0) {
      for (const conflictNote of finding.conflicts) {
        lines.push(`   conflict: ${conflictNote}`)
      }
    }
  }

  // Doc handoff strategy depends on file format:
  //
  // * ``.md`` / ``.txt``: agent uses ``fs_read`` on demand (F1 pattern).
  //   Selective loading keeps the first turn small; the agent pulls
  //   only the passages a user asks about.
  // * ``.docx``: ``fs_read`` returns undecodable ZIP bytes so on-demand
  //   loading doesn't work. Inline the extracted prose (from the
  //   ``/reviews/{id}/context`` prefetch) so the agent has the doc
  //   content it needs to reason about the review. Targeted exception
  //   to F1 -- one-off token cost per conversation, but the alternative
  //   is a broken UX for every docx review.
  //
  // Future formats (pdf, rtf) would follow the same branch shape:
  // agent-readable formats keep fs_read, binary formats get inlined.
  const documentPathLowercase = reviewData.doc_path.toLowerCase()
  const documentIsBinaryFormat = documentPathLowercase.endsWith('.docx')
  if (documentIsBinaryFormat) {
    lines.push('')
    lines.push('Extracted document content (from source docx):')
    lines.push('```')
    lines.push(bundle.document_content)
    lines.push('```')
  } else {
    // Doc is at ``doc_path`` -- agent uses fs_read to load the passage a
    // finding references rather than us inlining the entire document.
    lines.push('')
    lines.push(`Use fs_read on ${reviewData.doc_path} to load the document when a user asks about a specific passage.`)
  }

  return lines.join('\n')
}

/**
 * Compact fallback marker used when the ``/reviews/{id}/context``
 * fetch fails and we cannot build the full handoff message. The chat
 * still opens with the review id so the agent can at least
 * acknowledge which review the user is asking about.
 *
 * Same wire-contract rationale as ``buildReviewChatMessage``: the
 * agent matches on the ``[REVIEW CONTEXT] review_id=`` prefix by
 * exact bytes.
 */
export function buildReviewChatFallbackMessage(reviewId: string): string {
  return `[REVIEW CONTEXT] review_id=${reviewId}`
}
