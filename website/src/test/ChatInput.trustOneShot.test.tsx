/**
 * Source contract for #5486: a trust verb must never be mapped onto the one-shot
 * approval endpoint.
 *
 * `POST /api/approvals/{id}/{action}` honors exactly `approve`, `reject` and
 * `reject_once` (dashboard/handlers/sessions.py) — there is no trust verb, and
 * the next identical call prompts again. Mapping `trust`/`trust_reads` to
 * `approve` ran the tool once while `finish()` dispatched `decision: 'trust'`, so
 * the composer reported a standing grant the backend never recorded. Same defect
 * as #5400 (spawn-approval card) and #5434 (collapsed tool row).
 *
 * WHAT CHANGED, and why this file no longer extracts source text: the mapping
 * used to be a module-private `toApiDecision` inside ChatInput.tsx, spelled a
 * second time inside ChatPage.tsx and a third time as an inline ternary in
 * ActivityViewer's ApprovalEntry — three spellings of one rule, none importable
 * by the others. #8193 collapsed them into the single shared
 * `utils/approvalDecision.ts`, so this contract now imports the function that
 * actually ships instead of brace-matching it out of a component and rebuilding
 * it with `new Function`. That hack existed only because the helper was private;
 * it pinned a reconstruction, and this pins the real thing.
 *
 * The three call sites are held by `eslint-rules/approval-one-shot-decision.js`,
 * whose own violating-fixture tests are in `approvalOneShotDecisionRule.test.ts`:
 * a lint rule is the only layer that sees a trust verb BEFORE the mapping turns
 * it into `approve`, which is upstream of both the typed `api.resolveApproval`
 * client and the backend's 400.
 *
 * Why a source contract for the ChatInput binding: the trust arm is unreachable
 * from the DOM. `approvalTrustGrantable` withholds both Trust affordances unless
 * a slot is present, and `handleApprovalAction` closes over the `activeSlot` of
 * the render that drew the button — so a rendered Trust click always reaches the
 * slot-scoped `api.approveChatSlot` branch, never this mapping. The narrowing is
 * kept as a defensive invariant because the render gate is one edit away from
 * being widened, and this mapping decides what a widened gate would authorize.
 * The gate itself is pinned behaviorally in ChatInput.approval.test.tsx.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { toApiDecision } from '../utils/approvalDecision'

const source = readFileSync(resolve(__dirname, '../components/ChatInput.tsx'), 'utf-8')

/** The full verb set `handleApprovalAction` routes to the slot endpoint. */
const TRUST_VERBS = ['trust', 'trust_reads', 'trust_command', 'trust_base']

describe('toApiDecision (#5486 contract)', () => {
  it('maps every trust verb to reject — the one-shot endpoint cannot record a grant', () => {
    // If a trust verb ever reaches this mapping, fail closed: a rejection tells
    // the truth, an `approve` claims a standing grant that was a single
    // execution.
    for (const verb of TRUST_VERBS) {
      expect(toApiDecision(verb)).toBe('reject')
    }
  })

  it('still maps the decisions this endpoint does honor', () => {
    expect(toApiDecision('approved')).toBe('approve')
    expect(toApiDecision('rejected_once')).toBe('reject_once')
    expect(toApiDecision('rejected')).toBe('reject')
  })

  it('never answers approve for anything but an explicit one-shot approval', () => {
    // Fail-closed by default: an unknown or future verb must not be upgraded
    // into an execution. `approved` is the only input that authorizes one.
    for (const verb of ['', 'zzq-unknown', 'trusted', 'approve', 'allow']) {
      expect(toApiDecision(verb)).not.toBe('approve')
    }
  })
})

describe('ChatInput binding to the shared mapping (#5486 / #8193)', () => {
  it('has exactly one call site, on the one-shot endpoint', () => {
    // A second call site would be a second resolve path with its own grant
    // semantics — re-verify this contract against it before adding one.
    const calls = source.match(/(?<!function\s)toApiDecision\(/g) ?? []
    expect(calls).toHaveLength(1)
    expect(source).toContain('api.resolveApproval(approvalId, toApiDecision(decision))')
  })

  it('does not re-declare the mapping locally', () => {
    // The regression #8193 closed was three independent spellings. The lint rule
    // now catches a local re-declaration on its own, in every file, because it
    // resolves `toApiDecision` to its BINDING and admits only an import from the
    // shared module -- a same-named local function is reported. This stays as a
    // second, cheaper signal: it names the file and the import in the failure,
    // where the lint rule names a call site.
    expect(source).not.toMatch(/function\s+toApiDecision\b/)
    expect(source).not.toMatch(/const\s+toApiDecision\s*=/)
    expect(source).toContain("import { toApiDecision } from '../utils/approvalDecision'")
  })
})
