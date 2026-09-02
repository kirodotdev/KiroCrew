/**
 * Mirror of `test/test_tool_purpose_key.py` — the reserved tool-purpose
 * argument is read by SHAPE, not by an allowlist of literals, because the key
 * is a synthetic parameter the model fills in and models paraphrase its name
 * (`__purpose`, `__thinking_purpose`, `__woohoo_purpose` all occur in real
 * transcripts). Matching two literals left the purpose unread for whole
 * sessions at a time.
 */
import { describe, it, expect } from 'vitest'
import { isToolPurposeKey, purposeFromToolArgs, TOOL_PURPOSE_KEYS } from './toolPurpose'

describe('isToolPurposeKey', () => {
  it.each([...TOOL_PURPOSE_KEYS, '__purpose', '__thinking_purpose', '__woohoo_purpose', '__toolPurpose', '__tool-use-purpose'])(
    'claims the reserved spelling %s',
    key => { expect(isToolPurposeKey(key)).toBe(true) },
  )

  it.each(['purpose', 'tool_use_purpose', '_purpose', '__purpose_of_the_call', '__purposefully', '__command', ''])(
    'leaves %s alone',
    key => { expect(isToolPurposeKey(key)).toBe(false) },
  )
})

describe('purposeFromToolArgs', () => {
  it('reads the declared spelling', () => {
    expect(purposeFromToolArgs({ __tool_use_purpose: 'Read the failing job log' }))
      .toBe('Read the failing job log')
  })

  it('reads a paraphrased spelling alongside real arguments', () => {
    expect(purposeFromToolArgs({ __purpose: 'Read the failing job log', command: 'gh run view' }))
      .toBe('Read the failing job log')
  })

  it('prefers the declared spelling over a paraphrase', () => {
    expect(purposeFromToolArgs({ __purpose: 'paraphrased', __tool_use_purpose: 'canonical' }))
      .toBe('canonical')
  })

  it('resolves multiple paraphrases deterministically regardless of key order', () => {
    expect(purposeFromToolArgs({ __woohoo_purpose: 'second', __alpha_purpose: 'first' })).toBe('first')
    expect(purposeFromToolArgs({ __alpha_purpose: 'first', __woohoo_purpose: 'second' })).toBe('first')
  })

  it('does not misread a tool\u2019s own functional purpose argument', () => {
    expect(purposeFromToolArgs({ purpose: 'billing', amount: 12 })).toBe('')
  })

  it('trims surrounding whitespace', () => {
    expect(purposeFromToolArgs({ __purpose: '  Read the log  ' })).toBe('Read the log')
  })

  it.each([[''], ['   '], [null], [42], [['a']], [{ a: 1 }]])(
    'treats a blank or non-string value (%s) as no purpose',
    value => { expect(purposeFromToolArgs({ __purpose: value })).toBe('') },
  )

  it('falls through a blank declared key to a populated paraphrase', () => {
    expect(purposeFromToolArgs({ __tool_use_purpose: '   ', __purpose: 'the real reason' }))
      .toBe('the real reason')
  })

  it.each([[null], [undefined], ['a string'], [42], [['__purpose']]])(
    'returns nothing for a non-object payload (%s)',
    args => { expect(purposeFromToolArgs(args)).toBe('') },
  )
})
