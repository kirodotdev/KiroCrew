import { describe, it, expect } from 'vitest'

import type { SharedDecision } from '../../api/sharedCrew'
import { decisionVerdict } from './sharedDecision'

const decision = (outcome: string): SharedDecision =>
  ({ decision_id: 'd-1', revision: 1, outcome, resolved_at: 0, responder: null }) as unknown as SharedDecision

describe('decisionVerdict', () => {
  // A security-rule override records its own scope as the outcome, and
  // the call it settles runs, so the strip must read it as an approval.
  it.each(['approved', 'approved_trust_reads', 'deny_override_once', 'deny_override_session'])(
    'reads %s as approved',
    outcome => expect(decisionVerdict(decision(outcome))).toBe('approved'),
  )

  it.each(['rejected', 'rejected_once', 'trust', ''])('reads %s as rejected', outcome => {
    expect(decisionVerdict(decision(outcome))).toBe('rejected')
  })
})
