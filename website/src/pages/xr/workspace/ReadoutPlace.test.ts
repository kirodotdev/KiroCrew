import { describe, expect, it } from 'vitest'
import { boxAt, cornerFor } from './ReadoutPlace'

const area = { x0: 0, y0: 0, x1: 1200, y1: 800 }
const size = { w: 700, h: 60 }

describe('where the readout\'s block stands in the wide view', () => {
  it('stands low on the left when no page lies there', () => {
    expect(cornerFor(size, area, [{ x0: 200, y0: 130, x1: 1000, y1: 680 }])).toBe('lowerLeft')
    expect(boxAt('lowerLeft', size, area)).toEqual({ x0: 8, y0: 732, x1: 708, y1: 792 })
  })

  it('takes the first corner that lies on no page', () => {
    // A page down to the foot on the left: low on the right is free.
    expect(cornerFor(size, area, [{ x0: 0, y0: 300, x1: 450, y1: 800 }])).toBe('lowerRight')
    // Both low corners taken: high on the left.
    expect(cornerFor(size, area, [{ x0: 0, y0: 300, x1: 1200, y1: 800 }])).toBe('upperLeft')
  })

  it('with no free corner, takes the one that covers least', () => {
    const pages = [{ x0: 0, y0: 0, x1: 1200, y1: 790 }, { x0: 0, y0: 0, x1: 600, y1: 800 }]
    // Every corner lies on the first page; the low right lies on it least (to 790 only) and not on the second.
    expect(cornerFor(size, area, pages)).toBe('lowerRight')
  })
})
