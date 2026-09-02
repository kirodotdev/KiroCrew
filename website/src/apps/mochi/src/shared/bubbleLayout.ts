/** A plain rectangle, used for bounds and overlap tests. */
export interface Rect {
  left: number
  top: number
  right: number
  bottom: number
}

/** The anchor the bubble points at — the pet window's rectangle. */
export interface AnchorRect {
  left: number
  top: number
  right: number
  bottom: number
  width: number
  height: number
}

/** A candidate placement for the bubble. */
export interface Candidate {
  name: 'top' | 'top-right' | 'top-left'
  left: number
  top: number
  /** X the bubble's arrow points at, on the pet's top edge. */
  targetX: number
  /** Y the bubble's arrow points at — the pet's top edge (anchorRect.top). */
  targetY: number
  /** Arrow direction. Only `bottom` is supported today: the bubble sits above the pet. */
  arrowSide: 'bottom'
  bias: number
}

/** Bubble size. */
export interface Size {
  width: number
  height: number
}

/** Bubble priority, used to decide whether a new bubble may replace a live one. */
export enum BubblePriority {
  Chat = 0,
  Notification = 1,
  Error = 2,
  Approval = 3,
}

/** Gap inputs. */
export interface GapOptions {
  minGap?: number
  maxGap?: number
  gapGrowthFactor?: number
  heightThreshold?: number
}

/** Layout inputs. */
export interface LayoutOptions {
  margin?: number
  petPadding?: number
  jitterX?: number
  jitterY?: number
  /** When set, used verbatim instead of calling resolveBubbleGap. */
  gap?: number
  random?: () => number
  minGap?: number
  maxGap?: number
  gapGrowthFactor?: number
  heightThreshold?: number
}

/** Inputs for widening a tall bubble. */
export interface WideSizeOptions {
  minWidth?: number
  maxWidth?: number
  widthStep?: number
}

/** Layout defaults. */
export const BUBBLE_LAYOUT_DEFAULTS = {
  margin: 12,
  gap: 20,
  minGap: 10,
  maxGap: 20,
  petPadding: 10,
  jitterX: 24,
  jitterY: 14,
  minWidth: 180,
  maxWidth: 280,
  widthStep: 24,
  gapGrowthFactor: 0.1,
} as const


/** Clamp to [min, max]; returns min when max < min. */
export function clamp(value: number, min: number, max: number): number {
  if (max < min) return min
  return Math.max(min, Math.min(max, value))
}

/** Grow a rectangle by `padding` on every side. */
export function expandRect(rect: Rect, padding: number): Rect {
  return {
    left: rect.left - padding,
    top: rect.top - padding,
    right: rect.right + padding,
    bottom: rect.bottom + padding,
  }
}

/** Overlap area of two rectangles; 0 when they do not intersect. */
export function rectOverlapArea(a: Rect, b: Rect): number {
  const overlapWidth = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left))
  const overlapHeight = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top))
  return overlapWidth * overlapHeight
}


/** Gap between bubble and pet, derived from the bubble's height. */
export function resolveBubbleGap(
  anchorRect: AnchorRect,
  bubbleHeight: number,
  options?: GapOptions
): number {
  const minGap = options?.minGap ?? BUBBLE_LAYOUT_DEFAULTS.minGap
  const maxGap = options?.maxGap ?? BUBBLE_LAYOUT_DEFAULTS.maxGap
  const gapGrowthFactor = options?.gapGrowthFactor ?? BUBBLE_LAYOUT_DEFAULTS.gapGrowthFactor
  const heightThreshold = options?.heightThreshold ?? anchorRect.height * 0.5
  const heightOffset = Math.max(0, bubbleHeight - heightThreshold)
  return clamp(Math.round(minGap + heightOffset * gapGrowthFactor), minGap, maxGap)
}


/** The three candidate placements above the pet. */
export function buildBubbleCandidates(
  anchorRect: AnchorRect,
  bubbleWidth: number,
  bubbleHeight: number,
  jitterX: number,
  jitterY: number,
  gap: number
): Candidate[] {
  const cx = anchorRect.left + anchorRect.width / 2

  return [
    {
      name: 'top',
      left: cx - bubbleWidth / 2 + jitterX,
      top: anchorRect.top - bubbleHeight - gap + jitterY,
      targetX: cx,
      targetY: anchorRect.top,
      arrowSide: 'bottom',
      bias: 0,
    },
    {
      name: 'top-right',
      left: anchorRect.right - 36 + jitterX,
      top: anchorRect.top - bubbleHeight - gap + jitterY,
      targetX: anchorRect.right - anchorRect.width * 0.24,
      targetY: anchorRect.top,
      arrowSide: 'bottom',
      bias: 2,
    },
    {
      name: 'top-left',
      left: anchorRect.left - bubbleWidth + 36 + jitterX,
      top: anchorRect.top - bubbleHeight - gap + jitterY,
      targetX: anchorRect.left + anchorRect.width * 0.24,
      targetY: anchorRect.top,
      arrowSide: 'bottom',
      bias: 2,
    },
  ]
}


/** Clamp a candidate inside the screen bounds and return the final rectangle. */
export function resolveBubbleRectWithinBounds(
  candidate: Candidate,
  bubbleWidth: number,
  bubbleHeight: number,
  boundsRect: Rect,
  margin: number
): Rect {
  const minLeft = boundsRect.left + margin
  const maxLeft = boundsRect.right - bubbleWidth - margin
  const minTop = boundsRect.top + margin
  const maxTop = boundsRect.bottom - bubbleHeight - margin
  const left = clamp(candidate.left, minLeft, maxLeft)
  const top = clamp(candidate.top, minTop, maxTop)
  return {
    left,
    top,
    right: left + bubbleWidth,
    bottom: top + bubbleHeight,
  }
}

/** Same, for a screen whose origin is (0,0). */
export function resolveBubbleRect(
  candidate: Candidate,
  bubbleWidth: number,
  bubbleHeight: number,
  boundsWidth: number,
  boundsHeight: number,
  margin: number
): Rect {
  return resolveBubbleRectWithinBounds(candidate, bubbleWidth, bubbleHeight, {
    left: 0,
    top: 0,
    right: boundsWidth,
    bottom: boundsHeight,
  }, margin)
}


/** Score a candidate. Lower is better. */
export function scoreBubblePlacementWithinBounds(
  candidate: Candidate,
  bubbleWidth: number,
  bubbleHeight: number,
  boundsRect: Rect,
  avoidRect: Rect,
  margin: number
): number {
  const bubbleRect = resolveBubbleRectWithinBounds(candidate, bubbleWidth, bubbleHeight, boundsRect, margin)
  const overlapArea = rectOverlapArea(bubbleRect, avoidRect)
  const clampPenalty = Math.abs(bubbleRect.left - candidate.left) + Math.abs(bubbleRect.top - candidate.top)

  return (
    (candidate.bias || 0) +
    clampPenalty * 2 +
    (overlapArea > 0 ? 100000 + overlapArea : 0)
  )
}

/** Same, for a screen whose origin is (0,0). */
export function scoreBubblePlacement(
  candidate: Candidate,
  bubbleWidth: number,
  bubbleHeight: number,
  boundsWidth: number,
  boundsHeight: number,
  avoidRect: Rect,
  margin: number
): number {
  return scoreBubblePlacementWithinBounds(candidate, bubbleWidth, bubbleHeight, {
    left: 0,
    top: 0,
    right: boundsWidth,
    bottom: boundsHeight,
  }, avoidRect, margin)
}


/** Pick the best bubble placement, combining every step above. */
export function pickBubblePlacementWithinBounds(
  anchorRect: AnchorRect,
  bubbleWidth: number,
  bubbleHeight: number,
  boundsRect: Rect,
  options?: LayoutOptions
): Candidate {
  const margin = options?.margin || BUBBLE_LAYOUT_DEFAULTS.margin
  const petPadding = options?.petPadding || BUBBLE_LAYOUT_DEFAULTS.petPadding
  const jitterXRange = options?.jitterX || BUBBLE_LAYOUT_DEFAULTS.jitterX
  const jitterYRange = options?.jitterY || BUBBLE_LAYOUT_DEFAULTS.jitterY
  const random = options?.random || Math.random

  let gap = options?.gap
  if (gap == null) {
    gap = resolveBubbleGap(anchorRect, bubbleHeight, options)
  }

  const jitterX = Math.round((random() - 0.5) * jitterXRange)
  const jitterY = Math.round((random() - 0.5) * jitterYRange)
  const avoidRect = expandRect(anchorRect as unknown as Rect, petPadding)
  const candidates = buildBubbleCandidates(anchorRect, bubbleWidth, bubbleHeight, jitterX, jitterY, gap)

  candidates.sort((a, b) =>
    scoreBubblePlacementWithinBounds(a, bubbleWidth, bubbleHeight, boundsRect, avoidRect, margin) -
    scoreBubblePlacementWithinBounds(b, bubbleWidth, bubbleHeight, boundsRect, avoidRect, margin)
  )

  const bestScore = scoreBubblePlacementWithinBounds(candidates[0], bubbleWidth, bubbleHeight, boundsRect, avoidRect, margin)
  const viable = candidates.filter(candidate =>
    scoreBubblePlacementWithinBounds(candidate, bubbleWidth, bubbleHeight, boundsRect, avoidRect, margin) <= bestScore + 6
  )

  return viable[Math.floor(random() * viable.length)]
}

/** Convenience wrapper for a screen whose origin is (0,0). */
export function pickBubblePlacement(
  anchorRect: AnchorRect,
  bubbleWidth: number,
  bubbleHeight: number,
  boundsWidth: number,
  boundsHeight: number,
  options?: LayoutOptions
): Candidate {
  return pickBubblePlacementWithinBounds(anchorRect, bubbleWidth, bubbleHeight, {
    left: 0,
    top: 0,
    right: boundsWidth,
    bottom: boundsHeight,
  }, options)
}


/** Widen the bubble until it is wider than tall, or maxWidth is reached. */
export function resolveWideBubbleSize(
  measureAtWidth: (width: number | null) => Size,
  options?: WideSizeOptions
): Size {
  const minWidth = options?.minWidth || BUBBLE_LAYOUT_DEFAULTS.minWidth
  const maxWidth = options?.maxWidth || BUBBLE_LAYOUT_DEFAULTS.maxWidth
  const widthStep = options?.widthStep || BUBBLE_LAYOUT_DEFAULTS.widthStep

  let measured = measureAtWidth(null)
  let targetWidth = Math.max(measured.width, minWidth)

  if (targetWidth !== measured.width) {
    measured = measureAtWidth(targetWidth)
  }

  while (measured.height >= measured.width && targetWidth < maxWidth) {
    targetWidth = Math.min(maxWidth, targetWidth + widthStep)
    measured = measureAtWidth(targetWidth)
  }

  return measured
}


/** Whether a bubble of one priority may replace a bubble of another. */
export function canReplaceBubble(incoming: BubblePriority, current: BubblePriority): boolean {
  return incoming >= current
}
