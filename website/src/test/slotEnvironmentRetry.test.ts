/**
 * The slot-environment query's retry policy: a transient failure is retried
 * twice, the `owner_only` refusal (a Shared Crew guest) never is.
 */
import { describe, it, expect } from 'vitest'
import { ApiError } from '../api/apiError'
import { SLOT_ENVIRONMENT_RETRY, isOwnerOnlyRefusal } from '../components/sessionEnvironmentHooks'

const ownerOnly = () => new ApiError(403, 'owner authorization required', '{"code":"owner_only"}')

describe('SLOT_ENVIRONMENT_RETRY', () => {
  it('does not retry the owner_only refusal', () => {
    expect(SLOT_ENVIRONMENT_RETRY.retry(0, ownerOnly())).toBe(false)
    expect(isOwnerOnlyRefusal(ownerOnly())).toBe(true)
  })

  it('retries every other failure twice', () => {
    for (const error of [new Error('blip'), new ApiError(500, 'boom', '{"code":"internal"}'), new ApiError(403, 'sign in', '{"code":"auth_required"}'), new ApiError(403, 'forbidden', '')]) {
      expect(SLOT_ENVIRONMENT_RETRY.retry(0, error)).toBe(true)
      expect(SLOT_ENVIRONMENT_RETRY.retry(1, error)).toBe(true)
      expect(SLOT_ENVIRONMENT_RETRY.retry(2, error)).toBe(false)
    }
  })

  it('reads only a 403 with that code as the refusal', () => {
    expect(isOwnerOnlyRefusal(new ApiError(500, 'x', '{"code":"owner_only"}'))).toBe(false)
    expect(isOwnerOnlyRefusal(new Error('owner_only'))).toBe(false)
    expect(isOwnerOnlyRefusal(null)).toBe(false)
  })
})
