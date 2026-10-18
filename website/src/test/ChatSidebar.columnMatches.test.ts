/** `columnMatches` is the board's per-column filter predicate. These tests pin the
 *  tag-filter branches, and in particular the "Untagged" column shape — an empty
 *  tag list with `include_untagged` on — which the header labels "Untagged" and so
 *  must show untagged sessions ONLY, not every session (#21048). */
import { describe, expect, it } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useColumnMatches } from '../pages/chat-sidebar/board'
import type { TagColumn } from '../types'
import type { Slot } from '../pages/chat-sidebar/types'

/** An empty set of runtime lane signals: these tests exercise only the tag-filter
 *  branches, never a state lane. */
const NO_SIGNALS = {
  subagentStartedCounts: {},
  subagentApprovalCounts: {},
  workflowActiveSet: new Set<string>(),
  automationRunningSet: new Set<string>(),
}

/** A minimal slot carrying just the key and tags the predicate reads. */
function slot(key: string, tags: string[]): Slot {
  return { key, running: false, tags } as Slot
}

/** A minimal tag column; callers override the fields each case is about. */
function column(over: Partial<TagColumn>): TagColumn {
  return { id: 'c1', name: '', tag_ids: [], mode: 'any', order: 0, ...over } as TagColumn
}

/** Render the hook and return its `columnMatches` predicate. Named without the
 *  `use` prefix: the hook is called inside `renderHook`'s render callback (a valid
 *  hook site), not by this helper directly, so this is an ordinary function. */
function matcherFor() {
  return renderHook(() => useColumnMatches(NO_SIGNALS)).result.current.columnMatches
}

describe('columnMatches tag filtering', () => {
  it('empty tags + include_untagged ("Untagged" column) matches ONLY untagged sessions', () => {
    const columnMatches = matcherFor()
    const col = column({ tag_ids: [], include_untagged: true })
    expect(columnMatches(col, slot('untagged', []))).toBe(true)
    // The regression: a tagged session must NOT appear in the "Untagged" column.
    expect(columnMatches(col, slot('tagged', ['planned']))).toBe(false)
  })

  it('an empty-tag column narrows to untagged regardless of a custom name', () => {
    // The filter is keyed on the fields, not the display name, so renaming an
    // "Untagged" column never changes what it shows.
    const columnMatches = matcherFor()
    const col = column({ name: 'Everything', tag_ids: [], include_untagged: true })
    expect(columnMatches(col, slot('untagged', []))).toBe(true)
    expect(columnMatches(col, slot('tagged', ['planned']))).toBe(false)
  })

  it('empty tags without the flag ("All sessions" column) matches every session', () => {
    const columnMatches = matcherFor()
    const col = column({ tag_ids: [], include_untagged: false })
    expect(columnMatches(col, slot('untagged', []))).toBe(true)
    expect(columnMatches(col, slot('tagged', ['planned']))).toBe(true)
  })

  it('include_untagged is additive on top of a non-empty tag filter', () => {
    const columnMatches = matcherFor()
    const col = column({ tag_ids: ['planned'], mode: 'any', include_untagged: true })
    expect(columnMatches(col, slot('planned', ['planned']))).toBe(true)
    expect(columnMatches(col, slot('untagged', []))).toBe(true)
    expect(columnMatches(col, slot('done', ['done']))).toBe(false)
  })

  it('a non-empty tag filter without the flag excludes untagged sessions', () => {
    const columnMatches = matcherFor()
    const col = column({ tag_ids: ['planned'], mode: 'any', include_untagged: false })
    expect(columnMatches(col, slot('planned', ['planned']))).toBe(true)
    expect(columnMatches(col, slot('untagged', []))).toBe(false)
  })

  it("mode 'all' requires every selected tag; mode 'none' excludes them", () => {
    const columnMatches = matcherFor()
    const all = column({ tag_ids: ['a', 'b'], mode: 'all' })
    expect(columnMatches(all, slot('both', ['a', 'b']))).toBe(true)
    expect(columnMatches(all, slot('one', ['a']))).toBe(false)
    const none = column({ tag_ids: ['a'], mode: 'none' })
    expect(columnMatches(none, slot('has-a', ['a']))).toBe(false)
    expect(columnMatches(none, slot('no-a', ['b']))).toBe(true)
  })
})
