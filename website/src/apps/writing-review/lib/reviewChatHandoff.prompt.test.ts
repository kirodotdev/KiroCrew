/**
 * Additional contract tests for ``reviewChatHandoff.prompt.ts`` —
 * covering branches the existing ``ReviewDetail.test.tsx`` file does
 * not exercise (failed_scanners rendering, ask-line optionality,
 * cross_validation + conflict formatting, ``reasonLabel`` map, and
 * the ``buildReviewChatFallbackMessage`` helper).
 */
import { describe, it, expect } from 'vitest'

import {
  buildReviewChatMessage,
  buildReviewChatFallbackMessage,
  reasonLabel,
} from './reviewChatHandoff.prompt'
import type { FailedScanner, ReviewContextBundle, ReviewDetail } from './types'

function makeReviewBundle(overrides: {
  docPath?: string
  ask?: string
  findings?: ReviewDetail['findings']
  failed?: FailedScanner[]
  content?: string
} = {}): ReviewContextBundle {
  const review: ReviewDetail = {
    id: 'r1',
    doc_name: 'sample.md',
    doc_path: overrides.docPath ?? '/tmp/sample.md',
    verdict: 'yellow',
    finding_count: (overrides.findings ?? []).length,
    scanners_run: ['clarity', 'evidence'],
    created_at: 1_700_000_000,
    context: {
      audience: 'team',
      doc_type: 'update',
      tone: 'neutral',
      additional_context: [],
      ask: overrides.ask ?? '',
    },
    findings: overrides.findings ?? [],
    partial_failure: (overrides.failed ?? []).length > 0,
    failed_scanners: overrides.failed ?? [],
    log_reference: null,
  }
  return {
    review,
    document_content: overrides.content ?? '',
    scanner_brief_dir: '/tmp/briefs',
  }
}

describe('buildReviewChatMessage — branch coverage top-up', () => {
  it('emits the failed_scanners block with wire-style duration_ms=<n>', () => {
    const message = buildReviewChatMessage(
      makeReviewBundle({
        failed: [
          {
            name: 'clarity',
            reason_class: 'provider_timeout',
            duration_ms: 1234,
            message: 'model timed out after 30s',
          } as FailedScanner,
        ],
      }),
    )
    // The block header + a wire-style ``duration_ms=1234`` MUST both
    // appear so the LLM parser can enumerate failed scanners without
    // misreading the number+unit as user-visible copy.
    expect(message).toContain('failed_scanners:')
    expect(message).toContain('duration_ms=1234')
    expect(message).toContain('provider_timeout')
    expect(message).toContain('clarity')
  })

  it('omits the failed_scanners block when the failed list is empty', () => {
    const message = buildReviewChatMessage(makeReviewBundle())
    expect(message).not.toContain('failed_scanners:')
  })

  it('adds an ask: line when context.ask is non-empty', () => {
    const message = buildReviewChatMessage(
      makeReviewBundle({ ask: 'is my structure clear?' }),
    )
    expect(message).toContain('ask: is my structure clear?')
  })

  it('omits the ask: line when context.ask is empty', () => {
    // Silent-when-absent keeps the handoff clean rather than emitting
    // an "ask: (none)" line the agent parser would have to special-case.
    const message = buildReviewChatMessage(makeReviewBundle({ ask: '' }))
    expect(message).not.toMatch(/^ask:/m)
  })

  it('renders findings with severity, scanner, rule, section, and paragraph', () => {
    const message = buildReviewChatMessage(
      makeReviewBundle({
        findings: [
          {
            id: 'f1',
            severity: 'high',
            confidence: 'high',
            scanner: 'clarity',
            rule: 'R1-vague-pronoun',
            issue: 'the pronoun has no antecedent',
            section: 'Introduction',
            paragraph: 2,
            proposed_fix: 'replace "it" with "the design"',
          } as never,
        ],
      }),
    )
    expect(message).toContain('[high]')
    expect(message).toContain('clarity Rule R1-vague-pronoun')
    expect(message).toContain('Introduction, paragraph 2')
    expect(message).toContain('proposed_fix:')
  })

  it('substitutes "(no section)" when a finding has no section string', () => {
    const message = buildReviewChatMessage(
      makeReviewBundle({
        findings: [
          {
            id: 'f1',
            severity: 'low',
            scanner: 'clarity',
            rule: 'R1',
            issue: 'issue',
            paragraph: 1,
          } as never,
        ],
      }),
    )
    expect(message).toContain('(no section)')
  })

  it('emits cross_validation and conflict lines when set to a non-clean tag', () => {
    const message = buildReviewChatMessage(
      makeReviewBundle({
        findings: [
          {
            id: 'f1',
            severity: 'medium',
            scanner: 'clarity',
            rule: 'R1',
            issue: 'issue',
            paragraph: 1,
            section: 'Intro',
            cross_validation: 'conflicts',
            conflicts: ['naturalness reads it differently', 'evidence has no data'],
          } as never,
        ],
      }),
    )
    expect(message).toContain('cross_validation: conflicts')
    expect(message).toContain('conflict: naturalness reads it differently')
    expect(message).toContain('conflict: evidence has no data')
  })

  it('defaults confidence to "medium" when a finding omits the field', () => {
    // Old records without a confidence field MUST render the medium
    // default rather than "undefined" — this is the guard for
    // backward-compat with pre-V2 persisted findings.
    const message = buildReviewChatMessage(
      makeReviewBundle({
        findings: [
          {
            id: 'f1',
            severity: 'advisory',
            scanner: 'evidence',
            rule: 'R2',
            issue: 'issue',
            paragraph: 1,
          } as never,
        ],
      }),
    )
    expect(message).toContain('confidence: medium')
  })
})

describe('reasonLabel', () => {
  it('returns a non-empty localised label for every known reason_class', () => {
    for (const reasonClass of [
      'provider_timeout',
      'invalid_json',
      'truncated_response',
      'missing_brief',
      'rate_limited',
      'worker_died',
      'other',
    ]) {
      const failed = { name: 'x', reason_class: reasonClass, duration_ms: 0, message: '' } as FailedScanner
      const label = reasonLabel(failed)
      expect(label).toBeTruthy()
      // The dotted i18n key path is a stable non-empty fallback in
      // test environments where i18next isn't initialised.
      expect(label).not.toBe(reasonClass)
    }
  })

  it('falls back to the "other" label for an unknown reason_class', () => {
    // Backend may ship a new reason category before the frontend has
    // an i18n key for it; the fallback keeps the handoff legible
    // rather than emitting the raw reason string.
    const unknownFailure = {
      name: 'x',
      reason_class: '__reason_that_does_not_exist_yet__',
      duration_ms: 0,
      message: '',
    } as FailedScanner
    const label = reasonLabel(unknownFailure)
    expect(label).toBeTruthy()
    expect(label).not.toBe('__reason_that_does_not_exist_yet__')
  })
})

describe('buildReviewChatFallbackMessage', () => {
  it('emits a compact [REVIEW CONTEXT] review_id=<id> marker', () => {
    // The fallback path fires when the ``/reviews/{id}/context`` fetch
    // fails and we can't build the full handoff. The agent parser
    // matches on the ``[REVIEW CONTEXT] review_id=`` prefix, so the
    // exact literal bytes here are the wire contract.
    expect(buildReviewChatFallbackMessage('r1')).toBe('[REVIEW CONTEXT] review_id=r1')
  })
})
