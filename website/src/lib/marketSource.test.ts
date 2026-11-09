import { describe, it, expect } from 'vitest'
import { parseMarketSource } from './marketSource'

describe('parseMarketSource', () => {
  it('parses a valid market source into ref and version', () => {
    expect(parseMarketSource('market:acme/todo-ledger@3')).toEqual({ ref: 'acme/todo-ledger', version: 3 })
    expect(parseMarketSource('market:a1/b2-c3@123456789')).toEqual({ ref: 'a1/b2-c3', version: 123456789 })
  })

  it('rejects a source with no @version', () => {
    expect(parseMarketSource('market:acme/todo-ledger')).toBeNull()
    expect(parseMarketSource('market:acme/todo-ledger@')).toBeNull()
  })

  it('rejects a bad version', () => {
    for (const v of ['0', '01', '-1', '1.5', 'x', '1234567890']) {
      expect(parseMarketSource(`market:acme/todo-ledger@${v}`), v).toBeNull()
    }
  })

  it('rejects a malformed ref', () => {
    for (const ref of ['acme', 'Acme/todo', 'acme/Todo', 'acme/todo/extra', '-acme/todo', 'acme/todo-', 'acme//todo', '/todo', 'acme/']) {
      expect(parseMarketSource(`market:${ref}@1`), ref).toBeNull()
    }
  })

  it('rejects a trailing newline or surrounding whitespace', () => {
    expect(parseMarketSource('market:acme/todo@1\n')).toBeNull()
    expect(parseMarketSource(' market:acme/todo@1')).toBeNull()
  })

  it('rejects a non-market source and non-string input', () => {
    expect(parseMarketSource('registry:secretary')).toBeNull()
    expect(parseMarketSource('https://github.com/acme/todo')).toBeNull()
    expect(parseMarketSource('/home/me/apps/todo')).toBeNull()
    expect(parseMarketSource('')).toBeNull()
    expect(parseMarketSource(undefined)).toBeNull()
    expect(parseMarketSource(42)).toBeNull()
  })
})
