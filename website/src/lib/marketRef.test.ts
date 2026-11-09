// The Install from Kiro Space field checks a reference the way the gateway does
// (LISTING_REF_RE plus a positive-integer version), so a bad one is refused
// before any request leaves the page.
import { describe, it, expect } from 'vitest'
import { parseMarketRef } from './marketRef'

describe('parseMarketRef', () => {
  it('accepts <namespace>/<name> with no version', () => {
    expect(parseMarketRef('acme/notes')).toEqual({ ref: 'acme/notes' })
  })

  it('accepts kebab-case segments and trims surrounding spaces', () => {
    expect(parseMarketRef('  acme-co/daily-notes2  ')).toEqual({ ref: 'acme-co/daily-notes2' })
  })

  it('reads an optional @<version> as a positive integer', () => {
    expect(parseMarketRef('acme/notes@3')).toEqual({ ref: 'acme/notes', version: 3 })
    expect(parseMarketRef('acme/notes@123456789')).toEqual({ ref: 'acme/notes', version: 123456789 })
  })

  it.each([
    '',
    'notes',
    'acme/',
    '/notes',
    'acme/notes/extra',
    'Acme/notes',
    'acme/Notes',
    'acme/-notes',
    'acme/notes-',
    'acme/no--tes',
    'acme/no_tes',
    'acme/notes@',
    'acme/notes@0',
    'acme/notes@01',
    'acme/notes@-1',
    'acme/notes@1.5',
    'acme/notes@abc',
    'acme/notes@1234567890',
    'acme/notes@1@2',
    'acme notes',
  ])('refuses %j', (input) => {
    expect(parseMarketRef(input)).toBeNull()
  })
})
