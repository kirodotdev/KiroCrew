import { describe, it, expect } from 'vitest'
import { matcherCannotMatchAnyTagId } from '../pages/hookEventWireValues'

/**
 * The never-fires warning is only useful if it is silent on a matcher that WOULD fire. It shipped
 * condemning any id with a non-hex character, which is every lane the product seeds, so the
 * motivating close-out matcher `*added:done;*` warned on a fresh install. Every prior test and
 * capture fixture used a generated hex id, which is exactly why nothing caught it — so these
 * cases use the real seeded ids and the backend's real charset instead.
 */
describe('matcherCannotMatchAnyTagId', () => {
  // The ids a fresh install actually has, copied from the dashboard state module's seed list.
  const SEEDED_LANE_IDS = ['planned', 'todo', 'implementation', 'review', 'done']

  it.each(SEEDED_LANE_IDS)('stays silent on the seeded lane id %s', (id) => {
    expect(matcherCannotMatchAnyTagId(`*added:${id};*`)).toBe(false)
    expect(matcherCannotMatchAnyTagId(`*removed:${id};*`)).toBe(false)
  })

  it('stays silent on a generated hex id', () => {
    expect(matcherCannotMatchAnyTagId('*added:9f2c4ab17e03;*')).toBe(false)
  })

  it('accepts every character the backend validator admits', () => {
    // The backend validator `_TOKEN_ALLOWED` is [a-z0-9_-]+, so an underscore and a hyphen pass.
    expect(matcherCannotMatchAnyTagId('*added:in_review;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:in-review;*')).toBe(false)
  })

  it('does NOT condemn a capitalised column name, which the backend still matches', () => {
    // The backend matches with fnmatch(context.lower(), matcher.lower()), so case in the
    // matcher cannot stop a match. Warning here condemned a matcher that fires.
    expect(matcherCannotMatchAnyTagId('*added:Done;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:Implementation;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*ADDED:DONE;*')).toBe(false)
  })

  it('condemns a literal no id may hold at any case', () => {
    // Folding cannot rescue a space or punctuation, so these genuinely never fire.
    expect(matcherCannotMatchAnyTagId('*added:my lane;*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:lane!;*')).toBe(true)
  })

  it('condemns a regex metacharacter, which glob treats as literal', () => {
    // Stripped as if they were wildcards, `d.ne` collapsed to `done` and the warning went
    // silent. Glob is fnmatch: only `*?[]` stand in for characters we cannot know.
    expect(matcherCannotMatchAnyTagId('*added:d.ne;*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:do+ne;*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:(done);*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:done|todo;*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:^done$;*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:do{1}ne;*')).toBe(true)
  })

  it('still treats the four true glob wildcards as unknown characters', () => {
    expect(matcherCannotMatchAnyTagId('*added:do*ne;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:do?ne;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:do[nm]e;*')).toBe(false)
  })

  it('condemns a term whose literal part holds a character no id may hold', () => {
    expect(matcherCannotMatchAnyTagId('*added:my lane;*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*added:lane!;*')).toBe(true)
  })

  it('condemns a matcher carrying no direction-tagged term at all', () => {
    expect(matcherCannotMatchAnyTagId('done')).toBe(true)
  })

  it('condemns a one-sided wildcard, which cannot span both ends of the context', () => {
    // Matching is WHOLE-STRING against `added:<id>;`, so a no-`:` pattern must cover both
    // ends: `*done` never reaches the final `;`, and `done*` never starts at `added:`.
    expect(matcherCannotMatchAnyTagId('*done')).toBe(true)
    expect(matcherCannotMatchAnyTagId('done*')).toBe(true)
    expect(matcherCannotMatchAnyTagId('?done')).toBe(true)
  })

  it('does NOT condemn a wildcard matcher, which spans the prefix and does fire', () => {
    // Matching is whole-string fnmatch, so `*` crosses `added:` -- fnmatch("added:done;",
    // "*done*") is True and the hook fires. Warning here reassured the author wrong.
    expect(matcherCannotMatchAnyTagId('*done*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*done*;*')).toBe(false)
  })

  it('stays silent on an empty matcher, which fires on every lane change', () => {
    expect(matcherCannotMatchAnyTagId('')).toBe(false)
    expect(matcherCannotMatchAnyTagId('   ')).toBe(false)
  })

  it('stays silent when wildcards stand in for the unknown part of an id', () => {
    expect(matcherCannotMatchAnyTagId('*added:9f2c*;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:*;*')).toBe(false)
  })

  it("accepts the spec's any-movement selectors, which carry no direction word", () => {
    // The spec's selector table offers `*:<id>;*` (glob) and `:<id>;` (contains) for
    // "any movement". Requiring added:/removed: condemned the shapes it prescribes.
    expect(matcherCannotMatchAnyTagId('*:done;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId(':done;')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*:9f2c4ab17e03;*')).toBe(false)
  })

  it('still condemns an any-movement selector whose id cannot exist', () => {
    expect(matcherCannotMatchAnyTagId('*:my lane;*')).toBe(true)
  })

  it('condemns the recommended shape with its trailing bound dropped', () => {
    // Every context token closes with `;`, so a final term stopping at the id leaves that
    // `;` unconsumed under whole-string fnmatch -- dead, and previously unwarned.
    expect(matcherCannotMatchAnyTagId('*added:done')).toBe(true)
    expect(matcherCannotMatchAnyTagId('*removed:done')).toBe(true)
    expect(matcherCannotMatchAnyTagId(':done')).toBe(true)
  })

  it('stays silent when a trailing wildcard can span the closing semicolon', () => {
    expect(matcherCannotMatchAnyTagId('*added:done*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:done?')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:done;*')).toBe(false)
    expect(matcherCannotMatchAnyTagId('*added:done;')).toBe(false)
  })
})
