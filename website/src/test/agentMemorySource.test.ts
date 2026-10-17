/**
 * Which memory a pick binds, for a same-name crewmate and custom agent (#19472).
 *
 * The helpers in `lib/agentMemorySource` decide when a row, the composer chip or
 * a session-list label says "own memory" / "shared memory". The rule: only when
 * the list holds BOTH a crewmate and a custom agent of the name, and then the
 * words say what the pick binds, read from the row's store id.
 */
import { describe, it, expect } from 'vitest'
import {
  hasSameNamePair, rowMemorySource, rowMemoryTag, sessionMemoryTag, type MemorySourceRow,
} from '../lib/agentMemorySource'
import { withMemorySource } from '../utils/agentLabel'
import '../i18n/all'

const OWN_MEMBER: MemorySourceRow = { name: 'reviewer', selection_kind: 'member', memory_store: 'member-reviewer' }
const SHARED_MEMBER: MemorySourceRow = { name: 'reviewer', selection_kind: 'member', memory_store: 'default' }
const TEMPLATE: MemorySourceRow = { name: 'reviewer', selection_kind: 'template' }
const OTHER_TEMPLATE: MemorySourceRow = { name: 'writer', selection_kind: 'template' }

describe('rowMemorySource', () => {
  it('reads a crewmate with its own store as own memory', () => {
    expect(rowMemorySource(OWN_MEMBER)).toBe('own')
  })

  it('reads the default store, empty or named, as shared memory', () => {
    expect(rowMemorySource(SHARED_MEMBER)).toBe('shared')
    // The backend resolves an empty store id to `default`.
    expect(rowMemorySource({ ...SHARED_MEMBER, memory_store: '' })).toBe('shared')
  })

  it('reads every template as shared memory', () => {
    expect(rowMemorySource(TEMPLATE)).toBe('shared')
  })
})

describe('rowMemoryTag', () => {
  it('tags both rows of a same-name pair', () => {
    const rows = [OWN_MEMBER, TEMPLATE]
    expect(rowMemoryTag(OWN_MEMBER, rows)).toBe('own')
    expect(rowMemoryTag(TEMPLATE, rows)).toBe('shared')
  })

  it('tags nothing when the name has one row', () => {
    const rows = [OWN_MEMBER, OTHER_TEMPLATE]
    expect(rowMemoryTag(OWN_MEMBER, rows)).toBeNull()
    expect(rowMemoryTag(OTHER_TEMPLATE, rows)).toBeNull()
  })

  it('says what the pick binds, even when both rows share memory', () => {
    const rows = [SHARED_MEMBER, TEMPLATE]
    expect(rowMemoryTag(SHARED_MEMBER, rows)).toBe('shared')
    expect(rowMemoryTag(TEMPLATE, rows)).toBe('shared')
  })

  it('tags no row of a name-only roster', () => {
    const rows: MemorySourceRow[] = [{ name: 'reviewer' }, { name: 'reviewer' }]
    expect(hasSameNamePair('reviewer', rows)).toBe(false)
    expect(rowMemoryTag(rows[0], rows)).toBeNull()
  })
})

describe('sessionMemoryTag', () => {
  const rows = [OWN_MEMBER, TEMPLATE, OTHER_TEMPLATE]

  it('follows the kind the slot recorded', () => {
    expect(sessionMemoryTag('reviewer', 'member', '', rows)).toBe('own')
    expect(sessionMemoryTag('reviewer', 'template', '', rows)).toBe('shared')
  })

  it('reads a kind-less slot as the crewmate, like name-first resolution', () => {
    expect(sessionMemoryTag('reviewer', undefined, '', rows)).toBe('own')
    expect(sessionMemoryTag('reviewer', '', '', rows)).toBe('own')
  })

  it('reads an agent-less session as the default custom agent', () => {
    expect(sessionMemoryTag('', 'member', 'reviewer', rows)).toBe('shared')
    expect(sessionMemoryTag(undefined, undefined, 'reviewer', rows)).toBe('shared')
  })

  it('marks nothing for a name without a pair, or rows not loaded yet', () => {
    expect(sessionMemoryTag('writer', 'template', '', rows)).toBeNull()
    expect(sessionMemoryTag('reviewer', 'member', '', [])).toBeNull()
    expect(sessionMemoryTag('', '', '', rows)).toBeNull()
  })
})

describe('withMemorySource', () => {
  it('leaves a label alone without a tag', () => {
    expect(withMemorySource('reviewer', null)).toBe('reviewer')
  })

  it('names the memory after the label', () => {
    expect(withMemorySource('reviewer', 'own')).toBe('reviewer · own memory')
    expect(withMemorySource('reviewer', 'shared')).toBe('reviewer · shared memory')
  })

  it('keeps the inherited-default marker it is given', () => {
    expect(withMemorySource('reviewer · default', 'shared')).toBe('reviewer · default · shared memory')
  })
})
