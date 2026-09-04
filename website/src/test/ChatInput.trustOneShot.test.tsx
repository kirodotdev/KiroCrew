/**
 * Source contract for #5486: ChatInput's own `toApiDecision` must not map a
 * trust verb onto the one-shot approval endpoint.
 *
 * `POST /api/approvals/{id}/{action}` honors exactly `approve`, `reject` and
 * `reject_once` (dashboard/handlers/sessions.py) — there is no trust verb, and
 * the next identical call prompts again. Mapping `trust`/`trust_reads` to
 * `approve` there ran the tool once while `finish()` dispatched
 * `decision: 'trust'`, so the composer reported a standing grant the backend
 * never recorded. Same defect as #5400 (spawn-approval card) and #5434
 * (collapsed tool row); ChatPage's `toApiDecision` carries the same rule.
 *
 * Why a source contract and not a render test: the trust arm is now
 * unreachable from the DOM. `approvalTrustGrantable` withholds both Trust
 * affordances unless a slot is present, and `handleApprovalAction` closes over
 * the `activeSlot` of the render that drew the button — so a rendered Trust
 * click always reaches the slot-scoped `api.approveChatSlot` branch, never this
 * mapping. The narrowing is kept as a defensive invariant for the same reason
 * the sibling keeps it (ChatPage.collapsedGroupTrust.test.tsx): the render gate
 * is one edit away from being widened, and this arm decides what a widened gate
 * would authorize. The gate itself is pinned behaviorally in
 * ChatInput.approval.test.tsx.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const source = readFileSync(resolve(__dirname, '../components/ChatInput.tsx'), 'utf-8')

/** The body of `function toApiDecision(...)`, brace-matched from its signature. */
function toApiDecisionBody(src: string): string {
  const at = src.indexOf('function toApiDecision')
  expect(at).toBeGreaterThan(-1)
  const open = src.indexOf('{', src.indexOf(')', at))
  let depth = 0
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}') {
      depth--
      if (depth === 0) return src.slice(open + 1, i)
    }
  }
  throw new Error('unbalanced braces in toApiDecision')
}

const body = toApiDecisionBody(source)
// Executed rather than string-matched so the pin is about the MAPPING, not its
// spelling: any re-ordering, added branch or changed literal is caught. The body
// carries no type annotations, so it is valid JS as extracted. `new Function` is
// the subject under test being the source TEXT: the helper is module-private so
// it cannot be imported, and re-typing it here would pin a copy rather than the
// code that ships.
const toApiDecision = new Function('d', body) as (d: string) => string

describe("ChatInput toApiDecision (#5486 contract)", () => {
  it('maps every trust verb to reject — the one-shot endpoint cannot record a grant', () => {
    // The full verb set `handleApprovalAction` routes to the slot endpoint. If a
    // trust verb ever reaches this mapping, fail closed: a rejection tells the
    // truth, an `approve` claims a standing grant that was a single execution.
    for (const verb of ['trust', 'trust_reads', 'trust_command', 'trust_base']) {
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

  it('has exactly one call site, on the one-shot endpoint', () => {
    // A second call site would be a second resolve path with its own grant
    // semantics — re-verify this contract against it before adding one.
    const calls = source.match(/(?<!function\s)toApiDecision\(/g) ?? []
    expect(calls).toHaveLength(1)
    expect(source).toContain('api.resolveApproval(approvalId, toApiDecision(decision))')
  })
})
