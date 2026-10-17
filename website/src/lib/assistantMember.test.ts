import { describe, expect, it } from 'vitest'
import { ASSISTANT_MEMBER_NAME, hideMateWithoutPreview, isAssistantMember } from './assistantMember'

describe('the first crewmate', () => {
  it('is identified by its key alone, whatever template it runs', () => {
    expect(isAssistantMember({ name: ASSISTANT_MEMBER_NAME, kiro_agent: 'kirocrew' })).toBe(true)
    expect(isAssistantMember({ name: ASSISTANT_MEMBER_NAME, kiro_agent: 'my-template' })).toBe(true)
    expect(isAssistantMember({ name: 'helper', kiro_agent: 'kirocrew' })).toBe(false)
    expect(isAssistantMember({ name: 'kirocrew-mate', kiro_agent: 'kirocrew-mate' })).toBe(false)
    expect(isAssistantMember(null)).toBe(false)
  })
})

describe('hideMateWithoutPreview', () => {
  const created = { name: 'mate', source: 'builtin' }
  const theirs = { name: 'mate', source: 'kirocrew' }
  const other = { name: 'scout', source: 'kirocrew' }
  it('hides only the Mate Kiro Crew created while the preview is off', () => {
    expect(hideMateWithoutPreview([created, other], false)).toEqual([other])
    expect(hideMateWithoutPreview([theirs, other], false)).toEqual([theirs, other])
  })
  it('shows every row with the preview on', () => {
    expect(hideMateWithoutPreview([created, other], true)).toEqual([created, other])
  })
})
