import { describe, it, expect } from 'vitest'
import {
  findPinnedPromptIdx,
  findNextPromptIdx,
  computePinPush,
  promptPreview,
  pinHandoffY,
  pinPushTravel,
  ROW_PAD_Y,
  DEFAULT_PINNED_CARD_H,
} from '../utils/pinnedPrompt'
import type { DisplayItem } from '../pages/chat/types'

const user = (content: string, idx: number): DisplayItem =>
  ({ kind: 'single', msg: { role: 'user', content }, idx } as unknown as DisplayItem)
const assistant = (idx: number): DisplayItem =>
  ({ kind: 'single', msg: { role: 'assistant', content: 'a' }, idx } as unknown as DisplayItem)
const turn = (): DisplayItem =>
  ({ kind: 'turn', items: [], complete: true } as unknown as DisplayItem)

describe('pinHandoffY', () => {
  it('is the bottom edge of the band, not the fold line', () => {
    expect(pinHandoffY(100, 46.75)).toBe(100 + ROW_PAD_Y * 2 + 46.75)
  })

  it('falls back to the computed one-line card height before any measurement', () => {
    expect(DEFAULT_PINNED_CARD_H).toBeCloseTo(46.75, 2)
  })

  it('a one-line prompt hands over exactly as its bubble top reaches the card top', () => {
    // Row = ROW_PAD_Y + bubble + ROW_PAD_Y, and a one-line bubble is cardH tall.
    // Pinning at rowBottom <= handoffY therefore fires at bubbleTop === foldY +
    // ROW_PAD_Y — the card's own top — i.e. the old top-edge rule, unchanged.
    const foldY = 100, cardH = 46.75
    const handoffY = pinHandoffY(foldY, cardH)
    const rowBottomAtHandoff = handoffY
    const bubbleTop = rowBottomAtHandoff - ROW_PAD_Y - cardH
    expect(bubbleTop).toBe(foldY + ROW_PAD_Y)
  })
})

describe('findPinnedPromptIdx', () => {
  it('returns -1 with no prompts', () => {
    expect(findPinnedPromptIdx([], 0)).toBe(-1)
    expect(findPinnedPromptIdx([assistant(0), turn()], 2)).toBe(-1)
  })

  it('pins the previous prompt while the straddling row is itself a prompt', () => {
    // p2 straddles the hand-off line — still readable in the transcript, so the
    // banner keeps showing p1 (this is the tall-prompt fix).
    const items = [user('p1', 0), turn(), user('p2', 2), turn()]
    expect(findPinnedPromptIdx(items, 2)).toBe(0)
  })

  it('pins a prompt once the row after it is the straddling one', () => {
    const items = [user('p1', 0), turn(), user('p2', 2), turn()]
    expect(findPinnedPromptIdx(items, 3)).toBe(2)
  })

  it('skips non-prompt rows walking upward', () => {
    const items = [user('p1', 0), turn(), assistant(2), turn()]
    expect(findPinnedPromptIdx(items, 3)).toBe(0)
  })

  it('pins nothing above the first prompt', () => {
    expect(findPinnedPromptIdx([user('p1', 0), turn()], 0)).toBe(-1)
  })

  it('pins nothing when no row is below the hand-off line', () => {
    expect(findPinnedPromptIdx([user('p1', 0), turn()], -1)).toBe(-1)
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
  // The card sits ROW_PAD_Y below the fold, so it must travel that much further
  // than its own height to clear the band completely.
  const travel = ROW_PAD_Y + bannerH

  it('does not push while the incoming prompt is below the banner', () => {
    expect(computePinPush(bannerH, foldY, foldY + travel)).toBe(0)
    expect(computePinPush(bannerH, foldY, foldY + 400)).toBe(0)
  })

  it('pushes so the banner bottom tracks the incoming prompt top', () => {
    // gap 30 → banner bottom sits 30px below the fold → pushed travel-30
    expect(computePinPush(bannerH, foldY, foldY + 30)).toBe(travel - 30)
  })

  it('clears the band completely when the incoming prompt top reaches the fold', () => {
    // Card top = foldY + ROW_PAD_Y, so a push of exactly `travel` puts its BOTTOM
    // on the fold line: nothing of it is left inside the band. A push of only
    // `bannerH` would strand a ROW_PAD_Y-tall strip of the card's bottom edge
    // visible over the incoming prompt for the whole no-banner stretch.
    expect(computePinPush(bannerH, foldY, foldY)).toBe(travel)
    expect(computePinPush(bannerH, foldY, foldY - 200)).toBe(travel)
    expect(travel).toBeGreaterThan(bannerH)
  })

  it('leaves a no-banner stretch for a prompt taller than the band', () => {
    // A tall prompt's top is already above the fold (card fully pushed out) while
    // its own bottom is still below the hand-off line, so it has not taken the
    // pin yet — that stretch shows no banner, by design. The push reaching
    // `pinPushTravel` is ChatPage's signal to DROP the banner for its duration,
    // so nothing of the outgoing card can survive it.
    const handoffY = pinHandoffY(foldY, bannerH)
    const tallTop = foldY - 300
    const push = computePinPush(bannerH, foldY, tallTop)
    expect(push).toBe(travel)
    expect(push).toBeGreaterThanOrEqual(pinPushTravel(bannerH))
    expect(tallTop + 600).toBeGreaterThan(handoffY) // 600px-tall row: bottom still below
  })

  it('does not report a completed push while any of the card is still in the band', () => {
    // The drop threshold must not fire early: one pixel short of the fold, a
    // pixel of card is still legitimately visible and the banner stays mounted.
    const push = computePinPush(bannerH, foldY, foldY + 1)
    expect(push).toBeLessThan(pinPushTravel(bannerH))
  })

  it('hands off in one frame for a one-line incoming prompt', () => {
    // Its row is ROW_PAD_Y + bannerH + ROW_PAD_Y tall, so top-reaches-fold and
    // bottom-reaches-hand-off-line are the same instant — no gap for short prompts.
    expect(computePinPush(bannerH, foldY, foldY)).toBe(travel)
    expect(foldY + ROW_PAD_Y * 2 + bannerH).toBe(pinHandoffY(foldY, bannerH))
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
