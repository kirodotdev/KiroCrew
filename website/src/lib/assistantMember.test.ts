import { describe, expect, it } from 'vitest'
import { ASSISTANT_MEMBER_NAME, isAssistantMember } from './assistantMember'

describe('the first crewmate', () => {
  it('is identified by its key alone, whatever template it runs', () => {
    expect(isAssistantMember({ name: ASSISTANT_MEMBER_NAME, kiro_agent: 'kirocrew' })).toBe(true)
    expect(isAssistantMember({ name: ASSISTANT_MEMBER_NAME, kiro_agent: 'my-template' })).toBe(true)
    expect(isAssistantMember({ name: 'helper', kiro_agent: 'kirocrew' })).toBe(false)
    expect(isAssistantMember({ name: 'kirocrew-mate', kiro_agent: 'kirocrew-mate' })).toBe(false)
    expect(isAssistantMember(null)).toBe(false)
  })
})
