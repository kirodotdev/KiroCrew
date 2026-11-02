import { describe, it, expect } from 'vitest'
import { EXPECTED_END_STATES, isExpectedEndState } from '../components/expectedEndStates'
import { approvalRefusalEndState } from '../api/apiError'
import { ApiError } from '../api/client'

describe('expectedEndStates', () => {
  it('is the closed, frozen list the errors-use-error-notice rule names', () => {
    expect([...EXPECTED_END_STATES]).toEqual(['approval_gone', 'interrupted_cycle'])
    expect(Object.isFrozen(EXPECTED_END_STATES)).toBe(true)
  })

  it('accepts only listed states', () => {
    expect(isExpectedEndState('approval_gone')).toBe(true)
    expect(isExpectedEndState('interrupted_cycle')).toBe(true)
    // Real failures and clean stops are not on the list.
    for (const s of ['consecutive_failures', 'model_refused', 'manual', '', null, undefined, 404]) {
      expect(isExpectedEndState(s)).toBe(false)
    }
  })
})

describe('approvalRefusalEndState', () => {
  it('maps a terminal approval refusal to approval_gone', () => {
    expect(approvalRefusalEndState(new ApiError(404, 'not found'))).toBe('approval_gone')
    expect(approvalRefusalEndState(new ApiError(400, 'no pending approval'))).toBe('approval_gone')
  })

  it('leaves every other failure unlisted (an error)', () => {
    expect(approvalRefusalEndState(new ApiError(500, 'internal error'))).toBeNull()
    expect(approvalRefusalEndState(new ApiError(400, 'invalid action'))).toBeNull()
    expect(approvalRefusalEndState(new TypeError('Failed to fetch'))).toBeNull()
  })
})
