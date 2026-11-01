import { describe, expect, it } from 'vitest'
import { scopeOverridesCss } from '../hooks/themeCss'

// #24727: the 80-unit dropped-rule id shown in Settings must not keep half of
// an emoji from a pack's selector. A lone high surrogate renders as U+FFFD.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

describe('scopeOverridesCss dropped-rule id truncation', () => {
  it('drops an emoji the 80-unit id cap would cut in half', () => {
    const r = scopeOverridesCss(`.${'a'.repeat(77)}🎉bb{color:#fff}`)
    expect(r.droppedRules).toEqual([`.${'a'.repeat(77)}…`])
    expect(r.droppedRules[0]).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji that ends exactly at the cut', () => {
    const r = scopeOverridesCss(`.${'a'.repeat(76)}🎉bb{color:#fff}`)
    expect(r.droppedRules).toEqual([`.${'a'.repeat(76)}🎉…`])
  })
})
