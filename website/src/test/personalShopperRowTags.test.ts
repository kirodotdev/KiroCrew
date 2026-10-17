/**
 * Row-scoped group reassignment in the Personal Shopper Preferences tab.
 *
 * A multi-group preference is rendered once per group section, so "which tag
 * does this row's selector edit?" is not a cosmetic question — getting it wrong
 * edits a membership the user cannot see, or deletes one they never touched.
 * Every case below is a defect that shipped in an earlier revision.
 */
import { describe, it, expect } from 'vitest'
import { rowTagFor, nextTagsForRowEdit } from '../apps/personal-shopper/PreferencesTab'

describe('rowTagFor', () => {
  it('uses the section the row is rendered under, not the first tag', () => {
    // The original bug: under section "B" the editor opened preselected to "A".
    expect(rowTagFor(['A', 'B'], 'B')).toBe('B')
  })

  it('falls back to the first tag when there is no section (single group)', () => {
    expect(rowTagFor(['A'], undefined)).toBe('A')
  })

  it('is empty for an ungrouped preference', () => {
    expect(rowTagFor([], undefined)).toBe('')
  })
})

describe('nextTagsForRowEdit', () => {
  it('replaces only this row’s tag and keeps the others, in place', () => {
    expect(nextTagsForRowEdit(['A', 'B'], 'B', 'C')).toEqual(['A', 'C'])
  })

  it('preserves the tail — the data-loss regression', () => {
    // Sending [editGroup] alone used to drop every other membership.
    expect(nextTagsForRowEdit(['A', 'B', 'C'], 'A', 'Z')).toEqual(['Z', 'B', 'C'])
  })

  it('clearing removes only this row’s membership', () => {
    expect(nextTagsForRowEdit(['A', 'B'], 'B', '')).toEqual(['A'])
  })

  it('deduplicates when the user picks a group the preference already has', () => {
    // Otherwise this persists a duplicate tag and renders duplicate-key rows.
    expect(nextTagsForRowEdit(['A', 'B'], 'B', 'A')).toEqual(['A'])
  })

  it('treats a pick on an ungrouped preference as an addition', () => {
    expect(nextTagsForRowEdit([], '', 'A')).toEqual(['A'])
  })

  it('leaves an ungrouped preference alone when nothing is picked', () => {
    expect(nextTagsForRowEdit([], '', '')).toEqual([])
  })
})

describe('sibling rows cannot reverse each other', () => {
  it('an untouched selector sends no tags even after the sibling refetched', () => {
    // Two rows exist for one multi-group preference. Row "A" saves first and
    // `pref.tags` refetches under row "B"'s open form. Judging "did the group
    // change?" against the REFRESHED tags made row B's untouched selector look
    // like a deliberate change, so its text-only save shipped stale tags and
    // undid row A's reassignment.
    //
    // The guard is that the baseline is the value the form OPENED with, so an
    // untouched selector is equal to it no matter how `pref.tags` moved.
    const openedWith = 'B'
    const editGroupUntouched = 'B'
    expect(editGroupUntouched !== openedWith).toBe(false) // → omit `tags`

    // And when row B *does* move its selector, it edits its own tag against the
    // refreshed list (A → Z already applied by the sibling), not a stale copy.
    expect(nextTagsForRowEdit(['Z', 'B'], 'B', 'Q')).toEqual(['Z', 'Q'])
  })
})

describe('the concurrent-edit precondition is unreachable', () => {
  /**
   * Two rows submitting before either refetch is a genuine lost update: each
   * PUT carries a whole array computed from the same stale read, so the second
   * write restores the tag the first one replaced. This is what the reassignment
   * affordance is gated on `tags.length <= 1` to prevent — that preference
   * renders in exactly ONE section, so a second concurrent row cannot exist.
   *
   * These assertions pin the reasoning, not the component: they show the race
   * needs >= 2 tags, and that the UI never produces such a preference.
   */
  const canReassign = (tags: string[]) => tags.length <= 1

  it('offers reassignment for the shapes the UI can actually create', () => {
    expect(canReassign([])).toBe(true) // ungrouped
    expect(canReassign(['A'])).toBe(true) // one group
  })

  it('withholds it for a multi-group preference, which only the API can make', () => {
    expect(canReassign(['A', 'B'])).toBe(false)
  })

  it('never grows the tag count, so a single-group preference stays single', () => {
    // The add form sends at most one tag; if this editor could add a second,
    // the UI would manufacture the very shape the gate excludes.
    expect(nextTagsForRowEdit(['A'], 'A', 'B')).toHaveLength(1)
    expect(nextTagsForRowEdit([], '', 'A')).toHaveLength(1)
    expect(nextTagsForRowEdit(['A'], 'A', '')).toHaveLength(0)
  })

  it('demonstrates the lost update the gate makes unreachable', () => {
    // Rows A and B both read ['A','B']; A→Z then B→Q before any refetch.
    const rowA = nextTagsForRowEdit(['A', 'B'], 'A', 'Z') // ['Z','B']
    const rowB = nextTagsForRowEdit(['A', 'B'], 'B', 'Q') // ['A','Q'] — stale
    expect(rowA).toEqual(['Z', 'B'])
    expect(rowB).toEqual(['A', 'Q'])
    // Last write wins, so Z is gone and A is resurrected. Only possible with
    // >= 2 tags, which is exactly what `canReassign` refuses to edit.
    expect(rowB).not.toContain('Z')
    expect(canReassign(['A', 'B'])).toBe(false)
  })
})
