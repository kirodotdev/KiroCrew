import { describe, expect, it } from 'vitest'
import { annotationLabel } from '../utils/browserAnnotations'

// #24727: the 40-unit label cap must not keep half of an emoji. A lone high
// surrogate before the ellipsis renders as U+FFFD in the annotation list.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

describe('annotationLabel truncation', () => {
  it('drops an emoji the 40-unit label cap would cut in half', () => {
    const label = annotationLabel({ name: `${'a'.repeat(38)}🎉 tail`, text: '' })
    expect(label).toBe(`${'a'.repeat(38)}…`)
    expect(label).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji that ends exactly at the cut', () => {
    expect(annotationLabel({ name: `${'a'.repeat(37)}🎉 tail`, text: '' })).toBe(`${'a'.repeat(37)}🎉…`)
  })
})
