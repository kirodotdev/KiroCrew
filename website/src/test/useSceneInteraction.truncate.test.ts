import { describe, expect, it } from 'vitest'
import { messagePreview } from '../hooks/useSceneInteraction'

// #24727: the 64-unit scene tooltip must not keep half of an emoji. A lone
// high surrogate before the ellipsis renders as U+FFFD.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

describe('messagePreview truncation', () => {
  it('drops an emoji the 64-unit cap would cut in half', () => {
    const preview = messagePreview(`${'a'.repeat(62)}🎉 tail`)
    expect(preview).toBe(`${'a'.repeat(62)}…`)
    expect(preview).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji that ends exactly at the cut', () => {
    expect(messagePreview(`${'a'.repeat(61)}🎉 tail`)).toBe(`${'a'.repeat(61)}🎉…`)
  })
})
