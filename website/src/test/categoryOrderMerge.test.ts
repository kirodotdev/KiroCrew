/**
 * Tests for merging the published rail order into the client's canonical order.
 *
 * The rule under test is deliberately not "published wins, rest to the back":
 * a category the document omits keeps its LOCAL position, so a document that
 * simply forgot one does not silently demote it.
 */

import { describe, it, expect } from 'vitest'
import {
  CATEGORY_ORDER,
  PUBLISHED_CATEGORY_ID,
  mergeCategoryOrder,
  categoryCounts,
  type Category,
} from '../components/appstore/categories'

describe('mergeCategoryOrder', () => {
  it('returns the canonical order when nothing is published', () => {
    expect(mergeCategoryOrder([])).toEqual([...CATEGORY_ORDER])
  })

  it('returns the canonical order when no published id is recognised', () => {
    expect(mergeCategoryOrder(['nope', 'also-nope'])).toEqual([...CATEGORY_ORDER])
  })

  it('maps each published slug to the category it names', () => {
    // The table's values are CATEGORY_ORDER indexes, so a reorder of that array
    // would silently rewire every slug. This pins the pairs by value.
    expect(PUBLISHED_CATEGORY_ID).toEqual({
      'developer-tools': 'Developer Tools',
      'designer-tools': 'Designer Tools',
      'oncall-ops': 'On-call & Ops',
      'productivity': 'Productivity',
      'agents-automation': 'Agents & Automation',
      'research-writing': 'Research & Writing',
      'other': 'Other',
    })
  })

  it('covers every canonical category, so none can become unreachable', () => {
    expect(new Set(Object.values(PUBLISHED_CATEGORY_ID))).toEqual(new Set(CATEGORY_ORDER))
  })

  it('maps the published slug that no slugify would produce', () => {
    // The catalog publishes `oncall-ops`; a mechanical transform of the client
    // id `On-call & Ops` yields `on-call-ops`, which would silently miss.
    expect(PUBLISHED_CATEGORY_ID['oncall-ops']).toBe('On-call & Ops')
    // Naming ONE category does not promote it to the front: the two categories
    // that canonically precede it keep their places, which is the whole point of
    // not flushing omitted categories to the back.
    const got = mergeCategoryOrder(['oncall-ops'])
    expect(got).toContain('On-call & Ops')
    expect(got.indexOf('On-call & Ops')).toBe(CATEGORY_ORDER.indexOf('On-call & Ops'))
    expect(got).toEqual([...CATEGORY_ORDER])
  })

  it('honours the published sequence for the categories it names', () => {
    const got = mergeCategoryOrder(['other', 'developer-tools'])
    expect(got.indexOf('Other')).toBeLessThan(got.indexOf('Developer Tools'))
  })

  it('keeps an omitted category next to its canonical neighbour, not at the end', () => {
    // `designer-tools` is absent from the live document. It must stay adjacent to
    // Developer Tools (its canonical predecessor), NOT be flushed to the back.
    const live = [
      'developer-tools',
      'oncall-ops',
      'productivity',
      'agents-automation',
      'research-writing',
      'other',
    ]
    const got = mergeCategoryOrder(live)
    expect(got).toContain('Designer Tools')
    expect(got.indexOf('Designer Tools')).toBe(got.indexOf('Developer Tools') + 1)
    expect(got.indexOf('Designer Tools')).toBeLessThan(got.length - 1)
  })

  it('reproduces the canonical order when the live document is published as authored', () => {
    // Guards the actual deployed data: today's editorial.json must not reorder
    // the rail, so shipping this consumer is a no-op until a curator changes it.
    const live = [
      'developer-tools',
      'oncall-ops',
      'productivity',
      'agents-automation',
      'research-writing',
      'other',
    ]
    expect(mergeCategoryOrder(live)).toEqual([...CATEGORY_ORDER])
  })

  it('keeps every category exactly once', () => {
    const got = mergeCategoryOrder(['other', 'other', 'developer-tools'])
    expect(new Set(got).size).toBe(got.length)
    expect(got.length).toBe(CATEGORY_ORDER.length)
  })

  it('never loses a category, whatever subset is published', () => {
    for (const subset of [
      ['other'],
      ['productivity', 'developer-tools'],
      ['research-writing', 'oncall-ops', 'other'],
    ]) {
      const got = mergeCategoryOrder(subset)
      expect([...got].sort()).toEqual([...CATEGORY_ORDER].sort())
    }
  })

  it('ignores an inherited Object.prototype key published as an id', () => {
    // A published id of `toString` must not resolve to Object.prototype.toString
    // and push a function into the rail.
    const got = mergeCategoryOrder(['toString', 'constructor'])
    expect(got).toEqual([...CATEGORY_ORDER])
    for (const c of got) expect(typeof c).toBe('string')
  })
})

describe('categoryCounts with an explicit order', () => {
  const apps = [
    { tags: ['git'] }, // Developer Tools
    { tags: ['oncall'] }, // On-call & Ops
    { tags: ['research'] }, // Research & Writing
  ]

  it('defaults to the canonical order when no order is passed', () => {
    const got = categoryCounts(apps).map(c => c.category)
    expect(got).toEqual(['Developer Tools', 'On-call & Ops', 'Research & Writing'])
  })

  it('follows a supplied order', () => {
    const order: Category[] = ['Research & Writing', 'On-call & Ops', 'Developer Tools']
    const got = categoryCounts(apps, order).map(c => c.category)
    expect(got).toEqual(['Research & Writing', 'On-call & Ops', 'Developer Tools'])
  })

  it('still omits categories with no apps', () => {
    const got = categoryCounts(apps, [...CATEGORY_ORDER]).map(c => c.category)
    expect(got).not.toContain('Productivity')
    expect(got).toHaveLength(3)
  })
})
