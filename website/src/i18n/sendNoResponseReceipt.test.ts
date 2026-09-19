/**
 * The unanswered-send transcript row must state a RECEIPT fact, not a server claim.
 *
 * The row is appended with `role: 'error'` and stays in the transcript permanently, so a
 * reply that streams in directly beneath it sits under the row. A claim ABOUT THE SERVER
 * ("the server did not respond") is then plainly false to the reader; a claim about what
 * this client RECEIVED ("no delivery confirmation was received") stays true either way.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

const KEY = 'pages.chatPage.send_no_response'

/** Resolve a dotted key against a nested catalog object. */
function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

describe('the unanswered-send row states a receipt fact', () => {
  it('is present in every catalog that carries it and never claims the server was silent', () => {
    let seen = 0
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      const value = resolve(catalog.translation, KEY)
      if (typeof value !== 'string') continue
      seen++
      expect(value, `${lang} still claims the server did not respond`)
        .not.toMatch(/did not respond|n'a pas répondu|no respondió|не ответил|não respondeu/i)
      // Only the INSTANT transport arm appends this row -- the abort arm returns without it
      // -- so naming an elapsed window would date a failure that happened immediately.
      expect(value, `${lang} names an elapsed window, but this row renders on the instant arm`)
        .not.toMatch(/\d/)
    }
    // Positive control: a zero here would make the assertions above vacuous.
    expect(seen, 'no catalog carried the key -- the guard measured nothing').toBeGreaterThan(0)
  })
})
