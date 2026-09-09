/**
 * A proxy challenge withdraws retry affordances gated on `isAuthExpiredError`.
 *
 * This is a downstream consequence of the change, not a new behaviour: an
 * `ApiError` from a lapsed proxy now carries `authRequired`, and every surface
 * that reads it stops offering an action that would replay the same refusal —
 * `RemoteCrewPanel`'s failed-load Refresh button among them.
 *
 * The behaviour is desirable and the panel's own comment already gives the reason;
 * what it lacked was anything asserting it, so a later change to the flag could
 * quietly restore a button that only reproduces the error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ApiError } from '../api/apiError'
import { isAuthExpiredError } from '../api/client'

vi.mock('../i18n/t', () => ({ i18nT: (k: string) => k }))

/** The three signals a challenge needs, in the shape the wire delivers them. */
const CHALLENGE = {
  status: 403,
  type: 'text/html; charset=UTF-8',
  body: '<!DOCTYPE html><html><body><h1>Access Required</h1>'
    + '<p><a href="https://dash.example/gate-auth?redirect=%2Fapi">Sign in</a></p>'
    + '</body></html>',
}

describe('a lapsed proxy withdraws a retry that would replay it', () => {
  beforeEach(() => { vi.unstubAllGlobals() })

  it('marks the error as auth-expired, which is what gates the affordance', async () => {
    const { noteEdgeAuthChallenge } = await import('../api/edgeAuthChallenge')
    vi.stubGlobal('window', {
      location: { href: 'https://dash.example/' }, self: {}, top: {},
    })
    // `self === top` would be the un-framed case; this stub is a pane, so the
    // outcome is `framed` — one of the two the client marks auth-expired.
    expect(noteEdgeAuthChallenge(CHALLENGE.status, CHALLENGE.type, CHALLENGE.body))
      .toBe('framed')

    const err = new ApiError(403, 'proxy session expired', CHALLENGE.body, true)
    expect(isAuthExpiredError(err)).toBe(true)
  })

  it('leaves an ordinary failure retryable', () => {
    // The negative control: without the flag the affordance must stay, or every
    // transient error would lose its retry.
    expect(isAuthExpiredError(new ApiError(500, 'boom', 'boom', false))).toBe(false)
    expect(isAuthExpiredError(new Error('not an ApiError'))).toBe(false)
  })
})
