import { describe, it, expect } from 'vitest'
import { menuGeometry, MENU_MAX_HEIGHT, bottomUpOrder } from '../lib/pickerMenu'

/** Anchor stub at a fixed viewport position — geometry only reads the rect. */
function anchorAt(top: number, left = 40, width = 600, height = 44): HTMLElement {
  const el = document.createElement('div')
  el.getBoundingClientRect = () =>
    ({ top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}) }) as DOMRect
  return el
}

describe('menuGeometry', () => {
  it('opens above when there is room, positioning by the row-height estimate', () => {
    const g = menuGeometry(anchorAt(500), 3, 48)
    expect(g.above).toBe(true)
    // menuH = 3*48 + 8 = 152; top = 500 - 152 - 4
    expect(g.top).toBe(500 - 152 - 4)
    expect(g.maxHeight).toBe(MENU_MAX_HEIGHT)
  })

  it('budgets extraH into an above-anchor menu: the top edge shifts up by exactly extraH', () => {
    // Non-row chrome (e.g. the skill picker's pinned scope footer) must move
    // the menu's top edge up, not overhang the anchor/composer below it.
    const base = menuGeometry(anchorAt(500), 3, 48)
    const withFooter = menuGeometry(anchorAt(500), 3, 48, 28)
    expect(withFooter.above).toBe(true)
    expect(base.top - withFooter.top).toBe(28)
  })

  it('extraH defaults to 0 for the callers that pass none (@file, /command)', () => {
    const threeArg = menuGeometry(anchorAt(500), 3, 48)
    const explicitZero = menuGeometry(anchorAt(500), 3, 48, 0)
    expect(threeArg).toEqual(explicitZero)
  })

  it('caps the height budget at MENU_MAX_HEIGHT even with extraH', () => {
    // Tall list: menuH saturates, so extraH must not push the top edge past
    // the cap-derived position.
    const g = menuGeometry(anchorAt(800), 50, 48, 28)
    expect(g.top).toBe(800 - MENU_MAX_HEIGHT - 4)
  })

  it('falls below the anchor when the budgeted height does not fit above', () => {
    // 3*48+8+28 = 180 > 100-4 available above → opens below.
    const g = menuGeometry(anchorAt(100, 40, 600, 44), 3, 48, 28)
    expect(g.above).toBe(false)
    expect(g.top).toBe(100 + 44 + 4)
  })
})

describe('bottomUpOrder', () => {
  it('reverses and selects the bottom row when the menu opens above', () => {
    const { ordered, initialIndex } = bottomUpOrder(['a', 'b', 'c'], true)
    expect(ordered).toEqual(['c', 'b', 'a'])
    expect(initialIndex).toBe(2)
  })

  it('keeps order and selects the top row when the menu opens below', () => {
    const { ordered, initialIndex } = bottomUpOrder(['a', 'b', 'c'], false)
    expect(ordered).toEqual(['a', 'b', 'c'])
    expect(initialIndex).toBe(0)
  })
})
