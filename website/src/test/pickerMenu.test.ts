import { describe, it, expect } from 'vitest'
import { menuGeometry, MENU_MAX_HEIGHT, bottomUpOrder } from '../lib/pickerMenu'

/** Anchor stub at a fixed viewport position — geometry only reads the rect. */
function anchorAt(top: number, left = 40, width = 600, height = 44): HTMLElement {
  const el = document.createElement('div')
  el.getBoundingClientRect = () =>
    ({ top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}) }) as DOMRect
  return el
}

/** Run `fn` with window.innerHeight pinned to `h`, restoring it afterwards. */
function withViewportHeight(h: number, fn: () => void) {
  // Restore the ORIGINAL descriptor (happy-dom's accessor), not a frozen data
  // property — a leaked data property would make later suites order-dependent.
  const desc = Object.getOwnPropertyDescriptor(window, 'innerHeight')
  Object.defineProperty(window, 'innerHeight', { value: h, configurable: true, writable: true })
  try {
    fn()
  } finally {
    if (desc) Object.defineProperty(window, 'innerHeight', desc)
    else delete (window as unknown as Record<string, unknown>).innerHeight
  }
}

describe('menuGeometry', () => {
  it('opens above when there is room, positioning by the row-height estimate', () => {
    const g = menuGeometry(anchorAt(500), 3, 48)
    expect(g.above).toBe(true)
    // menuH = 3*48 + 8 = 152; top = 500 - 152 - 4
    expect(g.top).toBe(500 - 152 - 4)
    // 496px available above — plenty, so the clamp leaves the full cap.
    expect(g.maxHeight).toBe(MENU_MAX_HEIGHT)
  })

  it('places an opens-above menu by `bottom`, independent of the row estimate', () => {
    // A zero-row announcement's wrapped height varies by locale and width, which
    // no count*rowH estimate predicts; pinning the bottom edge makes it moot.
    const zeroRow = menuGeometry(anchorAt(500), 0, 48)
    expect(zeroRow.above).toBe(true)
    expect(zeroRow.bottom).toBe(window.innerHeight - 500 + 4)
    // The property `top` placement lacked: the anchor does not move with count.
    expect(menuGeometry(anchorAt(500), 3, 48).bottom).toBe(zeroRow.bottom)
    expect(menuGeometry(anchorAt(500), 3, 48, 28).bottom).toBe(zeroRow.bottom)
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

  it('clamps an opens-above menu to the space above the anchor', () => {
    // (a) Short rect.top: only rect.top - margin px exist above, so the menu
    // cannot extend past the viewport top however tall its real rows render.
    const g = menuGeometry(anchorAt(200), 1, 48)
    expect(g.above).toBe(true)
    expect(g.maxHeight).toBe(200 - 4)
    expect(g.maxHeight).toBeLessThan(MENU_MAX_HEIGHT)
  })

  it('clamps an opens-below menu to the space under the anchor', () => {
    // (b) Near the viewport bottom: available = viewportH - rect.bottom - margin.
    withViewportHeight(300, () => {
      const g = menuGeometry(anchorAt(40, 40, 600, 44), 3, 48)
      expect(g.above).toBe(false)
      expect(g.maxHeight).toBe(300 - (40 + 44) - 4)
      expect(g.maxHeight).toBeLessThan(MENU_MAX_HEIGHT)
    })
  })

  it('keeps the full MENU_MAX_HEIGHT cap when either side has generous space', () => {
    // (c) Above: 496px available at rect.top=500. Below: 670px under rect.bottom=94.
    expect(menuGeometry(anchorAt(500), 3, 48).maxHeight).toBe(MENU_MAX_HEIGHT)
    const below = menuGeometry(anchorAt(50, 40, 600, 44), 3, 48)
    expect(below.above).toBe(false)
    expect(below.maxHeight).toBe(MENU_MAX_HEIGHT)
  })

  it('pins the issue #8252 worked example: 3 rows at rect.top=160 opens above with maxHeight 156', () => {
    // (d) menuH = 3*48+8 = 152; aboveTop = 160-152-4 = 4 > 0 → above. The real
    // height (~167px at 53px/row) beats the estimate, but only 156px exist
    // above — the clamp makes the menu scroll instead of crossing the top edge.
    const g = menuGeometry(anchorAt(160), 3, 48)
    expect(g.above).toBe(true)
    expect(g.maxHeight).toBe(156)
  })

  it('flips to the roomier side when the chosen side cannot show even one row', () => {
    // 200px viewport, composer-like anchor low in it: the estimate picks below
    // (152 > 136 above), but below has only 12px — under one row — while 136px
    // sit unused above. Pure rect math flips the side; the menu scrolls there.
    withViewportHeight(200, () => {
      const g = menuGeometry(anchorAt(140, 40, 600, 44), 3, 48)
      expect(g.above).toBe(true)
      expect(g.maxHeight).toBe(140 - 4)
    })
  })

  it('caps an opens-above clamp at the viewport for an anchor scrolled past the bottom edge', () => {
    // rect.top (800) exceeds the 300px viewport: `bottom` floors at 0 so the
    // menu's bottom edge pins to the viewport bottom — the box can only ever
    // use the viewport itself, never the off-screen rect.top - margin.
    withViewportHeight(300, () => {
      const g = menuGeometry(anchorAt(800), 3, 48)
      expect(g.above).toBe(true)
      expect(g.bottom).toBe(0)
      expect(g.maxHeight).toBe(300)
    })
  })

  it('includes extraH in the floor so the pinned chrome cannot eat the one guaranteed row', () => {
    // Skill picker with its 30px scope footer at a hopeless viewport: the
    // floor must fund one row PLUS the footer, not have the footer eat it.
    withViewportHeight(100, () => {
      const g = menuGeometry(anchorAt(40, 40, 600, 44), 3, 48, 30)
      expect(g.maxHeight).toBe(48 + 8 + 30)
    })
  })

  it('floors maxHeight at one row plus padding when no side has room', () => {
    // 100px viewport: below offers 12px, above 36px — the flip lands above
    // (roomier), but even that side is under one row, so the floor wins and
    // the menu must not degrade to a zero- or sliver-height box.
    withViewportHeight(100, () => {
      const g = menuGeometry(anchorAt(40, 40, 600, 44), 3, 48)
      expect(g.above).toBe(true)
      expect(g.maxHeight).toBe(48 + 8)
    })
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
