import { describe, it, expect } from 'vitest'
import {
  findPinnedPromptIdx,
  findNextPromptIdx,
  computePinPush,
  promptPreview,
} from '../utils/pinnedPrompt'
import type { DisplayItem } from '../pages/chat/types'

const user = (content: string, idx: number): DisplayItem =>
  ({ kind: 'single', msg: { role: 'user', content }, idx } as unknown as DisplayItem)
const assistant = (idx: number): DisplayItem =>
  ({ kind: 'single', msg: { role: 'assistant', content: 'a' }, idx } as unknown as DisplayItem)
const turn = (): DisplayItem =>
  ({ kind: 'turn', items: [], complete: true } as unknown as DisplayItem)

describe('findPinnedPromptIdx', () => {
  it('returns -1 with no prompts', () => {
    expect(findPinnedPromptIdx([], 0, false)).toBe(-1)
    expect(findPinnedPromptIdx([assistant(0), turn()], 1, true)).toBe(-1)
  })

  it('pins the fold row itself once its top has crossed the fold', () => {
    const items = [user('p1', 0), turn(), user('p2', 2), turn()]
    expect(findPinnedPromptIdx(items, 2, true)).toBe(2)
  })

  it('pins the previous prompt while the fold row starts below the fold', () => {
    const items = [user('p1', 0), turn(), user('p2', 2), turn()]
    expect(findPinnedPromptIdx(items, 2, false)).toBe(0)
  })

  it('skips non-prompt rows walking upward', () => {
    const items = [user('p1', 0), turn(), assistant(2), turn()]
    expect(findPinnedPromptIdx(items, 3, true)).toBe(0)
  })

  it('pins nothing above the first prompt', () => {
    expect(findPinnedPromptIdx([user('p1', 0), turn()], 0, false)).toBe(-1)
  })
})

describe('findNextPromptIdx', () => {
  it('finds the next prompt after the pinned one', () => {
    const items = [user('p1', 0), turn(), user('p2', 2), turn()]
    expect(findNextPromptIdx(items, 0)).toBe(2)
  })

  it('returns -1 when the pinned prompt is the last one', () => {
    const items = [user('p1', 0), turn(), user('p2', 2), turn()]
    expect(findNextPromptIdx(items, 2)).toBe(-1)
  })
})

describe('computePinPush', () => {
  const bannerH = 52
  const foldY = 100

  it('does not push while the incoming prompt is below the banner', () => {
    expect(computePinPush(bannerH, foldY, foldY + bannerH)).toBe(0)
    expect(computePinPush(bannerH, foldY, foldY + 400)).toBe(0)
  })

  it('pushes so the banner bottom tracks the incoming prompt top', () => {
    // gap 30 → banner bottom sits 30px below the fold → pushed 22px
    expect(computePinPush(bannerH, foldY, foldY + 30)).toBe(22)
  })

  it('is fully pushed out exactly when the incoming prompt reaches the fold', () => {
    expect(computePinPush(bannerH, foldY, foldY)).toBe(bannerH)
    expect(computePinPush(bannerH, foldY, foldY - 200)).toBe(bannerH)
  })

  it('no push when the incoming row is unmounted or the banner unmeasured', () => {
    expect(computePinPush(bannerH, foldY, null)).toBe(0)
    expect(computePinPush(0, foldY, foldY)).toBe(0)
  })
})

describe('promptPreview', () => {
  it('collapses newlines to a single line', () => {
    expect(promptPreview('line one\n\nline two')).toBe('line one line two')
  })

  it('drops inline images and folds fenced code', () => {
    expect(promptPreview('look ![img](/a/b.png) here')).toBe('look here')
    expect(promptPreview('run ```js\nconst a = 1\n``` please')).toBe('run … please')
  })

  it('reduces attachment tokens to a basename', () => {
    expect(promptPreview('review [attached_file 1] /Users/me/proj/main.py now'))
      .toBe('review main.py now')
  })
})
