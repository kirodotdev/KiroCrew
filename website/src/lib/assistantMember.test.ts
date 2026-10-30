import { describe, expect, it } from 'vitest'
import { ASSISTANT_MEMBER_NAME, hasOtherMates, isAssistantMember } from './assistantMember'

describe('the first mate', () => {
  it('is identified by its key alone, whatever template it runs', () => {
    expect(isAssistantMember({ name: ASSISTANT_MEMBER_NAME, kiro_agent: 'kirocrew' })).toBe(true)
    expect(isAssistantMember({ name: ASSISTANT_MEMBER_NAME, kiro_agent: 'my-template' })).toBe(true)
    expect(isAssistantMember({ name: 'helper', kiro_agent: 'kirocrew' })).toBe(false)
    expect(isAssistantMember({ name: 'kirocrew-mate', kiro_agent: 'kirocrew-mate' })).toBe(false)
    expect(isAssistantMember(null)).toBe(false)
  })
})

describe('hasOtherMates', () => {
  it('a fresh install is Mate alone: the reserved default agent does not count', () => {
    // Every roster carries `default`; counting it would make "your first Mate"
    // unreachable on the one install it is written for.
    expect(hasOtherMates([{ name: 'default' }, { name: ASSISTANT_MEMBER_NAME }])).toBe(false)
    expect(hasOtherMates([{ name: ASSISTANT_MEMBER_NAME }])).toBe(false)
    expect(hasOtherMates([])).toBe(false)
  })

  it('a mate the person made themselves counts', () => {
    expect(hasOtherMates([{ name: 'default' }, { name: ASSISTANT_MEMBER_NAME }, { name: 'reviewer' }])).toBe(true)
  })
})
