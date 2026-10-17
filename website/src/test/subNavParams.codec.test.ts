/**
 * Pins the shared path-segment codec every path-mode navigation site uses
 * (SidePanelLayout, SettingsSubNav, SettingsPage's legacy translation,
 * settingsRoute). The two contracts:
 *
 *  - toPathSegment: one key = exactly one segment. `/`, `?`, `#`, `%` are
 *    percent-encoded; the dot-only keys the WHATWG URL parser resolves as
 *    dot-segments even in percent form ('%2E', '%2E%2E') are REJECTED (null),
 *    because no encoding makes them safe.
 *  - parsePathSegments: POSITIONAL. An empty segment (double slash, trailing
 *    slash) stays in place as '' rather than letting deeper segments shift
 *    forward — /settings/channels//slack must never read as sub=slack.
 *    Segments are decoded so encoded keys round-trip.
 */
import { describe, it, expect } from 'vitest'
import { toPathSegment, parsePathSegments } from '../components/subNavParams'

describe('toPathSegment', () => {
  it('passes plain keys through unchanged', () => {
    expect(toPathSegment('channels')).toBe('channels')
    expect(toPathSegment('aws-transcribe')).toBe('aws-transcribe')
  })

  it('encodes depth- and boundary-minting characters into one segment', () => {
    expect(toPathSegment('a/b')).toBe('a%2Fb')
    expect(toPathSegment('a?b')).toBe('a%3Fb')
    expect(toPathSegment('a#b')).toBe('a%23b')
    expect(toPathSegment('a%b')).toBe('a%25b')
  })

  it('rejects the dot-only segments URL normalization resolves against the tree', () => {
    expect(toPathSegment('.')).toBeNull()
    expect(toPathSegment('..')).toBeNull()
    // Dotted keys that are NOT pure dot-segments are ordinary keys.
    expect(toPathSegment('a.b')).toBe('a.b')
  })
})

describe('parsePathSegments', () => {
  it('returns [] at the bare base and outside the base', () => {
    expect(parsePathSegments('/settings', '/settings')).toEqual([])
    expect(parsePathSegments('/settings', '/developer/x')).toEqual([])
    // Prefix, not startsWith: /settingsfoo is a different route.
    expect(parsePathSegments('/settings', '/settingsfoo')).toEqual([])
  })

  it('splits positional segments under the base', () => {
    expect(parsePathSegments('/settings', '/settings/channels')).toEqual(['channels'])
    expect(parsePathSegments('/settings', '/settings/channels/slack')).toEqual(['channels', 'slack'])
  })

  it('keeps empty segments in position instead of shifting deeper ones forward', () => {
    expect(parsePathSegments('/settings', '/settings/channels//slack')).toEqual(['channels', '', 'slack'])
    expect(parsePathSegments('/settings', '/settings//security')).toEqual(['', 'security'])
    expect(parsePathSegments('/settings', '/settings/channels/')).toEqual(['channels', ''])
  })

  it('decodes segments so encoded keys round-trip through toPathSegment', () => {
    expect(parsePathSegments('/settings', '/settings/channels/a%2Fb')).toEqual(['channels', 'a/b'])
  })

  it('leaves a malformed escape as-is rather than throwing', () => {
    expect(parsePathSegments('/settings', '/settings/channels/%zz')).toEqual(['channels', '%zz'])
  })
})
