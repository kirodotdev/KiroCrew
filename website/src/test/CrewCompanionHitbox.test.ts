/**
 * Cursor-hitbox geometry, and which rects the companion reports.
 *
 * The overlay covers the whole display and is click-through except over the
 * companion, its bubble, and (while open) the context menu. `hitbox.ts` is the
 * pure geometry both sides share; `useMouseForward` is what actually reports the
 * companion + bubble rects to the main process. These pin which rects are
 * reported and that a point is classified inside/outside each one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { MutableRefObject } from 'react'

import {
  BUBBLE_HIT_PAD,
  petHitbox,
  bubbleHitbox,
  pointInRect,
  reportedHitboxes,
  hitsAny,
} from '../apps/crew-companion/hitbox'
import { PET_W, PET_H } from '../apps/crew-companion/constants'
import type { Rect } from '../apps/crew-companion/bubbleLayout'
import { useMouseForward } from '../apps/crew-companion/useMouseForward'
import { petBridge } from '../apps/crew-companion/petBridge'

/** A measured bubble placement rectangle, as pet.tsx computes it. */
function rect(left: number, top: number, w: number, h: number): Rect {
  return { left, top, right: left + w, bottom: top + h }
}

describe('petHitbox', () => {
  it('is the companion box at its position, sized from the shared constants', () => {
    expect(petHitbox({ x: 40, y: 60 })).toEqual({ x: 40, y: 60, w: PET_W, h: PET_H })
  })
})

describe('bubbleHitbox', () => {
  it('is null when no bubble is showing', () => {
    expect(bubbleHitbox(null)).toBeNull()
    expect(bubbleHitbox(undefined)).toBeNull()
  })

  it('converts a placement rect and folds the tail padding into the height', () => {
    expect(bubbleHitbox(rect(90, 10, 240, 60))).toEqual({
      x: 90,
      y: 10,
      w: 240,
      h: 60 + BUBBLE_HIT_PAD,
    })
  })
})

describe('pointInRect', () => {
  const r = { x: 10, y: 20, w: 100, h: 50 }
  it('includes the interior and the edges', () => {
    expect(pointInRect(r, 60, 40)).toBe(true)
    expect(pointInRect(r, 10, 20)).toBe(true)
    expect(pointInRect(r, 110, 70)).toBe(true)
  })
  it('excludes points just outside any edge', () => {
    expect(pointInRect(r, 9, 40)).toBe(false)
    expect(pointInRect(r, 111, 40)).toBe(false)
    expect(pointInRect(r, 60, 19)).toBe(false)
    expect(pointInRect(r, 60, 71)).toBe(false)
  })
  it('never matches a null rect', () => {
    expect(pointInRect(null, 60, 40)).toBe(false)
  })
})

describe('reportedHitboxes — which rects the overlay declares interactive', () => {
  const pos = { x: 100, y: 100 }

  it('reports only the companion when nothing else is showing', () => {
    const boxes = reportedHitboxes({ pos, bubbleRect: null, menuRect: null })
    expect(boxes.pet).toEqual({ x: 100, y: 100, w: PET_W, h: PET_H })
    expect(boxes.bubble).toBeNull()
    expect(boxes.menu).toBeNull()
  })

  it('adds the bubble and the menu when each is present', () => {
    const boxes = reportedHitboxes({
      pos,
      bubbleRect: rect(80, 0, 240, 60),
      menuRect: { x: 400, y: 300, w: 160, h: 90 },
    })
    expect(boxes.bubble).toEqual({ x: 80, y: 0, w: 240, h: 60 + BUBBLE_HIT_PAD })
    expect(boxes.menu).toEqual({ x: 400, y: 300, w: 160, h: 90 })
  })
})

describe('hitsAny — cursor classification against the reported rects', () => {
  const boxes = reportedHitboxes({
    pos: { x: 100, y: 100 },
    bubbleRect: rect(90, 0, 240, 60),
    menuRect: { x: 400, y: 300, w: 160, h: 90 },
  })

  it('is true inside the companion, the bubble, or the menu', () => {
    expect(hitsAny(boxes, 150, 150)).toBe(true) // companion
    expect(hitsAny(boxes, 120, 20)).toBe(true) // bubble
    expect(hitsAny(boxes, 450, 340)).toBe(true) // menu
  })

  it('is false on empty desktop between the rects', () => {
    expect(hitsAny(boxes, 900, 700)).toBe(false)
  })
})

describe('useMouseForward — reports the companion and bubble rects', () => {
  let update: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    update = vi.spyOn(petBridge, 'updateHitbox').mockImplementation(() => {})
  })
  afterEach(() => {
    update.mockRestore()
  })

  const dragging = { current: false } as MutableRefObject<boolean>

  it('reports the companion box, and no bubble, at rest', () => {
    renderHook(() => useMouseForward({ pos: { x: 100, y: 120 }, bubbleRect: null, dragging }))
    expect(update).toHaveBeenCalledWith({ x: 100, y: 120, w: PET_W, h: PET_H }, null)
  })

  it('reports the bubble rect once a bubble is showing', () => {
    const { rerender } = renderHook(
      ({ bubbleRect }: { bubbleRect: Rect | null }) =>
        useMouseForward({ pos: { x: 100, y: 120 }, bubbleRect, dragging }),
      { initialProps: { bubbleRect: null as Rect | null } },
    )
    update.mockClear()
    rerender({ bubbleRect: rect(60, 20, 240, 70) })
    expect(update).toHaveBeenCalledWith(
      { x: 100, y: 120, w: PET_W, h: PET_H },
      { x: 60, y: 20, w: 240, h: 70 + BUBBLE_HIT_PAD },
    )
  })
})
