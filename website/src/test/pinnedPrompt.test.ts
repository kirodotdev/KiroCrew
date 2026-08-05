import { describe, it, expect } from 'vitest'
import {
  findPinnedPromptIdx,
  findNextPromptIdx,
  computePinPush,
  promptPreview,
  promptImages,
  promptBody,
  pinnedImageUrl,
  pinHandoffY,
  pinPushTravel,
  ROW_PAD_Y,
  DEFAULT_PINNED_CARD_H,
  PINNED_PREVIEW_LINES,
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
    // ROW_PAD_Y — the card's own top — i.e. the top-edge rule.
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
    // banner keeps showing p1.
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

  it('leaves an image-only prompt with no text at all', () => {
    // Which is exactly why promptImages exists: the card would otherwise be blank.
    expect(promptPreview('![shot](/tmp/a.png)')).toBe('')
  })
})

describe('promptImages', () => {
  it('finds nothing in a prompt without images', () => {
    expect(promptImages('just text')).toEqual([])
  })

  it('collects sources in document order', () => {
    expect(promptImages('a ![one](/x/1.png) b ![two](/y/2.jpg)'))
      .toEqual(['/x/1.png', '/y/2.jpg'])
  })

  it('recovers the image of a prompt whose text is empty after preview', () => {
    const content = '![shot](/tmp/a.png)'
    expect(promptPreview(content)).toBe('')
    expect(promptImages(content)).toEqual(['/tmp/a.png'])
  })

  it('deduplicates a repeated source', () => {
    expect(promptImages('![a](/x/1.png) ![b](/x/1.png)')).toEqual(['/x/1.png'])
  })

  it('drops empty sources rather than yielding a broken thumbnail', () => {
    expect(promptImages('![alt]()')).toEqual([])
    expect(promptImages('![alt](   )')).toEqual([])
  })

  it('agrees with promptPreview about what an image is', () => {
    // The two share IMAGE_MD_RE precisely so a form can never be stripped from the
    // text while being missed by the thumbnail pass (i.e. silently lost).
    const content = 'before ![x](/p/q.png) after'
    expect(promptPreview(content)).toBe('before after')
    expect(promptImages(content)).toEqual(['/p/q.png'])
  })
})

describe('promptBody', () => {
  it('keeps line structure, unlike the collapsed preview', () => {
    expect(promptBody('one\n\ntwo')).toBe('one\n\ntwo')
    expect(promptPreview('one\n\ntwo')).toBe('one two')
  })

  it('removes the image markdown the card renders as a thumbnail', () => {
    // Leaving it in printed the source of an image the user is already looking at.
    expect(promptBody('before ![x](/p/q.png) after')).toBe('before  after')
  })

  it('drops a line that held nothing but an image', () => {
    expect(promptBody('intro\n![x](/p/q.png)\noutro')).toBe('intro\noutro')
  })

  it('yields empty for an image-only prompt, leaving the strip as the whole card', () => {
    expect(promptBody('![x](/p/q.png)')).toBe('')
    expect(promptImages('![x](/p/q.png)')).toEqual(['/p/q.png'])
  })

  it('preserves authored blank lines rather than reflowing them', () => {
    // Expanded is the read-it-properly view: the user's own spacing is content,
    // and only the lines an image vacated are removed.
    expect(promptBody('a\n\n\n\nb')).toBe('a\n\n\n\nb')
  })
})

describe('fenced code is treated identically by all three passes', () => {
  // The three passes agreeing on IMAGE_MD_RE was not enough: promptPreview folded
  // fences BEFORE the image pass while the other two ran on raw content, so a
  // prompt that merely QUOTED image markdown in a code block produced a thumbnail
  // for an image it never attached, and had that line rewritten inside the quoted
  // code. They now share splitFences, so the agreement is structural.
  const quoted = 'see this snippet:\n```md\n![alt](/x/1.png)\n```\nthat is all'

  it('does not invent a thumbnail for an image quoted in a code fence', () => {
    expect(promptImages(quoted)).toEqual([])
  })

  it('leaves quoted image syntax intact in the expanded body', () => {
    expect(promptBody(quoted)).toContain('![alt](/x/1.png)')
    expect(promptBody(quoted)).toContain('```md')
  })

  it('still folds the fence away in the collapsed preview', () => {
    expect(promptPreview(quoted)).toBe('see this snippet: … that is all')
  })

  it('still collects a real attachment that sits outside the fence', () => {
    const mixed = '![real](/r.png) and ```\n![quoted](/q.png)\n```'
    expect(promptImages(mixed)).toEqual(['/r.png'])
    expect(promptBody(mixed)).toContain('![quoted](/q.png)')
    expect(promptBody(mixed)).not.toContain('![real]')
  })

  it('handles an unterminated fence without swallowing a later attachment', () => {
    // An unclosed fence is not a fence as far as FENCE_RE is concerned, so the
    // image after it is still a real attachment rather than silently dropped.
    expect(promptImages('```\nnot closed ![a](/a.png)')).toEqual(['/a.png'])
  })
})

describe('pinnedImageUrl', () => {
  it('routes a local path through the gateway file endpoint', () => {
    expect(pinnedImageUrl('/tmp/a b.png')).toBe('/api/file-raw?path=%2Ftmp%2Fa%20b.png')
  })

  it('passes absolute and inline sources through untouched', () => {
    expect(pinnedImageUrl('https://e.com/a.png')).toBe('https://e.com/a.png')
    expect(pinnedImageUrl('data:image/png;base64,AAA')).toBe('data:image/png;base64,AAA')
    expect(pinnedImageUrl('blob:abc')).toBe('blob:abc')
  })
})

describe('collapsed preview line count', () => {
  it('shows more than one line', () => {
    expect(PINNED_PREVIEW_LINES).toBeGreaterThan(1)
  })

  it('couples to the hand-off line in the SAFE direction', () => {
    // The clamp makes the collapsed card taller, and its measured height feeds
    // pinHandoffY. A taller card must only ever move the line DOWN, making the pin
    // condition (rowBottom <= handoffY) easier — otherwise a card growing after it
    // mounts could invalidate the very pin that mounted it and oscillate.
    const foldY = 100
    const oneLine = pinHandoffY(foldY, DEFAULT_PINNED_CARD_H)
    const threeLine = pinHandoffY(foldY, DEFAULT_PINNED_CARD_H * PINNED_PREVIEW_LINES)
    expect(threeLine).toBeGreaterThan(oneLine)
    // A row that qualified against the shorter line still qualifies against the
    // taller one.
    const rowBottom = oneLine
    expect(rowBottom <= oneLine).toBe(true)
    expect(rowBottom <= threeLine).toBe(true)
  })
})
