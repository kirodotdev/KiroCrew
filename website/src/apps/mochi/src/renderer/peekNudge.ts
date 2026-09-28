/**
 * peekNudgeFor — how far to slide the pet off-screen while it peeks.
 *
 * A "peek" is normally a POSE: a drawing of the character half-hidden behind the
 * screen edge. Only the built-in cat ships one (`peeking` / `peekThinking`); the
 * Kiro Ghost deliberately omits it, because a peek is a specific drawing and none
 * of its four float clips is one. AnimationResolver therefore falls back to
 * `idle`, and the result was a ghost sitting upright at the edge, fully visible —
 * visually identical to not peeking at all.
 *
 * So for a pack with no peek art, the POSITION carries the meaning the missing
 * drawing would have: move the pet toward the edge it is hiding against until
 * part of it is off-screen. A pack that DOES ship peek art is left alone — its
 * drawing already says "I am behind the edge", and nudging it as well would push
 * that art out of frame.
 */
import type { AnimationResolver } from './animationResolver'

/**
 * Pixels to slide toward the edge. Chosen against PET_W (128): a little under
 * half the body tucks out of frame — enough to read unmistakably as hiding, while
 * the remaining silhouette is still recognisably the pet rather than a sliver.
 */
export const PEEK_NUDGE_PX = 60

export interface PeekNudgeArgs {
  isPeeking: boolean
  hideEdge: 'left' | 'right' | null
  /** null means the built-in cat, whose peek art is compiled in. */
  resolver: AnimationResolver | null
}

/**
 * Signed x offset: negative toward the left edge, positive toward the right,
 * 0 when no nudge applies.
 */
/*
 * A plain function, not a hook: this is a pure derivation of three scalars, so
 * `useMemo` bought nothing and made it impossible to assert without a render
 * harness — a rule whose whole content is "which of four cases applies" should be
 * testable by calling it.
 */
export function peekNudgeFor({ isPeeking, hideEdge, resolver }: PeekNudgeArgs): number {
  if (!isPeeking || hideEdge === null) return 0
  // The cat (resolver === null) draws from its compiled-in peek SVGs.
  if (resolver === null) return 0
  if (resolver.hasState('peeking')) return 0
  return hideEdge === 'left' ? -PEEK_NUDGE_PX : PEEK_NUDGE_PX
}
