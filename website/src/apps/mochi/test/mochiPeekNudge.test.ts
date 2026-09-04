/**
 * The peek nudge: position stands in for a missing peek POSE.
 *
 * Only the built-in cat ships `peeking` / `peekThinking` art. The Kiro Ghost
 * omits it deliberately (a peek is a specific half-hidden drawing and none of its
 * four float clips is one), so AnimationResolver falls back to `idle` and a
 * "peeking" ghost sat upright at the edge — indistinguishable from not peeking.
 *
 * The nudge must apply ONLY when the art cannot say it itself, or a pack that
 * ships a peek pose would have that pose shoved out of frame.
 */
import { describe, expect, it } from 'vitest'

import { PEEK_NUDGE_PX, peekNudgeFor } from '../src/renderer/peekNudge'
import type { AnimationResolver } from '../src/renderer/animationResolver'

/** A resolver stub that only has to answer `hasState`. */
function packWithPeek(has: boolean): AnimationResolver {
  return { hasState: (slot: string) => (slot === 'peeking' ? has : true) } as AnimationResolver
}

describe('peekNudgeFor', () => {
  it('slides toward the edge for a pack with no peek art', () => {
    const noPeek = packWithPeek(false)
    expect(peekNudgeFor({ isPeeking: true, hideEdge: 'left', resolver: noPeek })).toBe(
      -PEEK_NUDGE_PX,
    )
    expect(peekNudgeFor({ isPeeking: true, hideEdge: 'right', resolver: noPeek })).toBe(
      PEEK_NUDGE_PX,
    )
  })

  it('leaves a pack that ships a peek pose alone', () => {
    // Nudging it too would push the half-hidden drawing out of frame.
    expect(peekNudgeFor({ isPeeking: true, hideEdge: 'right', resolver: packWithPeek(true) })).toBe(
      0,
    )
  })

  it('leaves the built-in cat alone — its peek art is compiled in', () => {
    // resolver === null IS the cat: PetWidget renders it from fallbackUriCache,
    // which holds real peek SVGs.
    expect(peekNudgeFor({ isPeeking: true, hideEdge: 'left', resolver: null })).toBe(0)
  })

  it('is zero when not peeking, or peeking at no edge', () => {
    const noPeek = packWithPeek(false)
    expect(peekNudgeFor({ isPeeking: false, hideEdge: 'left', resolver: noPeek })).toBe(0)
    expect(peekNudgeFor({ isPeeking: true, hideEdge: null, resolver: noPeek })).toBe(0)
  })
})
