/**
 * A card refusal reads in the person's language by its gateway code, falls
 * back to the gateway's own message for a code this build does not know, and
 * only the refusals a fresh read can resolve ask for one.
 */
import { describe, it, expect } from 'vitest'
import { i18nT } from '../i18n/t'
import { problemFrom, problemText, wantsRefresh } from './refusals'

const CODES = [
  'owner_only',
  'plan_mismatch',
  'changed_since_preview',
  'changed_since_apply',
  'stale_revision',
  'card_busy',
  'step_out_of_order',
  'undo_unavailable',
]

/** An HTTP error the way the api client throws it: a message plus the raw body. */
function httpError(body: unknown, message = 'gateway said no'): Error {
  return Object.assign(new Error(message), { body: typeof body === 'string' ? body : JSON.stringify(body) })
}

describe('card refusals', () => {
  it('localizes every known refusal code instead of showing the gateway message', () => {
    for (const code of CODES) {
      const text = problemText({ code, message: 'gateway said no' })
      expect(text).toBe(i18nT(`components.changeCards.refusal_${code}`))
      expect(text).not.toBe('gateway said no')
    }
  })

  it('falls back to the gateway message for an unknown or missing code', () => {
    expect(problemText({ code: 'something_new', message: 'raw reason' })).toBe('raw reason')
    expect(problemText({ code: null, message: 'raw reason' })).toBe('raw reason')
  })

  it('reads the code from an error body, and the message from any thrown value', () => {
    expect(problemFrom(httpError({ code: 'card_busy' }))).toEqual({ code: 'card_busy', message: 'gateway said no' })
    expect(problemFrom(httpError('not json'))).toEqual({ code: null, message: 'gateway said no' })
    expect(problemFrom('plain string')).toEqual({ code: null, message: 'plain string' })
  })

  it('asks for a fresh read only where one can resolve the refusal', () => {
    expect(wantsRefresh('changed_since_preview')).toBe(true)
    expect(wantsRefresh('stale_revision')).toBe(true)
    expect(wantsRefresh('changed_since_apply')).toBe(false)
    expect(wantsRefresh(null)).toBe(false)
  })
})
