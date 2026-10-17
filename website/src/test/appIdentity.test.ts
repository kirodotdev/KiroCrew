/**
 * `isValidAppId` is the gate between an app id and a storage key, so the
 * refusals are the part worth pinning: an accepted id becomes a `localStorage`
 * key segment and a react-query key prefix, and every one of the cases below
 * would name something other than the app it claims to be.
 */
import { describe, it, expect } from 'vitest'
import { isValidAppId } from '../apps/appIdentity'

describe('isValidAppId', () => {
  it.each([
    'aws-control',
    'agent-worlds',
    'ops-mission-control',
    'a',
    'x1',
    '2fa',
  ])('accepts %s', (appId) => {
    expect(isValidAppId(appId)).toBe(true)
  })

  it.each([
    ['', 'empty — would collapse the namespace to the prefix itself'],
    ['.', 'current-segment, which addresses the parent namespace'],
    ['..', 'traversal out of the namespace'],
    ['../aws-control', 'traversal into another app'],
    ['a/b', 'separator — one id addressing two segments'],
    ['a\\b', 'backslash separator'],
    ['Aws-Control', 'uppercase — a second namespace for one app under byte-wise keys'],
    ['aws_control', 'underscore is outside the charset'],
    ['aws control', 'whitespace'],
    ['app.json', 'dot'],
    ['app:1', 'colon — the key separator itself'],
    ['app%2f', 'percent-encoding, which a reader may decode back to a separator'],
  ])('refuses %j (%s)', (appId) => {
    expect(isValidAppId(appId)).toBe(false)
  })

  // The runtime registration seam takes an object authored outside the module,
  // so a non-string id is reachable even where TypeScript says it is not.
  it.each([undefined, null, 123, {}, [], ['aws-control'], true])(
    'refuses the non-string %j',
    (appId) => {
      expect(isValidAppId(appId)).toBe(false)
    },
  )
})
