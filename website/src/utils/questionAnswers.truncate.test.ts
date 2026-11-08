import { describe, expect, it } from 'vitest'
import { questionName } from './questionAnswers'

describe('questionName with astral characters', () => {
  it('drops an emoji the 80-unit cap would cut in half instead of keeping its high surrogate', () => {
    // 78 ASCII units, then an emoji occupying UTF-16 units 78-79.
    const question = `${'a'.repeat(78)}🎉 and the rest of the question`
    expect(questionName([{ question }])).toBe(`${'a'.repeat(78)}…`)
  })

  it('keeps an emoji that ends exactly at the cap', () => {
    const question = `${'a'.repeat(77)}🎉 and the rest of the question`
    expect(questionName([{ question }])).toBe(`${'a'.repeat(77)}🎉…`)
  })

  it('leaves long ASCII at 80 units including the ellipsis', () => {
    expect(questionName([{ question: 'x'.repeat(200) }])).toBe(`${'x'.repeat(79)}…`)
  })
})
