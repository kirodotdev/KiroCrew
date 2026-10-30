import { describe, expect, it } from 'vitest'
import { mateAbilitiesLabel, templateDisplayName } from './builtFromName'

describe('templateDisplayName', () => {
  it('labels the default template and the source-defined agents a crewmate runs on as the default', () => {
    for (const id of ['kirocrew', 'kirocrew-mate-conductor', 'kirocrew-mate-worker', '']) {
      expect(templateDisplayName(id)).toBe('kirocrew (default)')
    }
  })

  it('keeps a template the user picked under its own name', () => {
    expect(templateDisplayName('release-captain')).toBe('release-captain')
  })
})

describe('mateAbilitiesLabel', () => {
  it('is a generic mate label, not a template provenance', () => {
    // A mate's abilities are the sum of its base plus additions, not one
    // template, so it is named generically rather than by a "built from".
    expect(mateAbilitiesLabel()).toBe('Mate')
  })
})
