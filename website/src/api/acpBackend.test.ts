/**
 * `isKiroBackend` decides whether the pill's Kiro-only surfaces (the no-reading
 * dash) render. It must be POSITIVE — true only for the kiro id, and never for a
 * config that has not loaded — because a guess here puts a "could not read your
 * balance; open to refresh" control in front of a user whose harness has no
 * balance to read.
 */
import { describe, expect, it } from 'vitest'

import { ACP_BACKEND_KIRO, isKiroBackend } from './acpBackend'

describe('isKiroBackend', () => {
  it('is false while the config has not loaded: nothing Kiro-only renders on a guess', () => {
    expect(isKiroBackend(undefined)).toBe(false)
  })

  it('names kiro by its own id, which is also what an unset key means on the gateway', () => {
    expect(ACP_BACKEND_KIRO).toBe('')
    expect(isKiroBackend({ agent: { acp_backend: ACP_BACKEND_KIRO } })).toBe(true)
    expect(isKiroBackend({ agent: {} })).toBe(true)
    expect(isKiroBackend({})).toBe(true)
  })

  it('is false for every other harness, not just claude', () => {
    for (const backend of ['claude', 'kas', 'codex', 'opencode', 'pi', 'goose', 'deepseek', 'some-future-harness']) {
      expect(isKiroBackend({ agent: { acp_backend: backend } }), backend).toBe(false)
    }
  })
})
