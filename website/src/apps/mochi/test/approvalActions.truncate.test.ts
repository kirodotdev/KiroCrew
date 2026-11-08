import { describe, expect, it } from 'vitest'
import { approvalLabel } from '../panel/approvalActions'

// #24727: the 72-unit approval line must not keep half of an emoji. A lone
// high surrogate before the ellipsis renders as U+FFFD in the dialog.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

describe('approvalLabel truncation', () => {
  it('drops an emoji the 72-unit cap would cut in half', () => {
    const label = approvalLabel(`${'a'.repeat(71)}🎉tail`)
    expect(label).toBe(`${'a'.repeat(71)}…`)
    expect(label).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji that ends exactly at the cap', () => {
    expect(approvalLabel(`${'a'.repeat(70)}🎉tail`)).toBe(`${'a'.repeat(70)}🎉…`)
  })
})
