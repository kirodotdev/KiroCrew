import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { ICON_MOTIONS, IconMotionLoader, pickIconMotion } from '../components/IconLoaderMotions'

// Strip comments so a selector mentioned in prose cannot satisfy the sizing contract.
const CSS = readFileSync(resolve(__dirname, '../styles/chat-transcript.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
const Mark = () => <i className="mark" />

describe('pickIconMotion', () => {
  it('can land on the carousel and on every motion, and stays in range', () => {
    const seen = new Set<string>()
    for (let i = 0; i < 100; i++) seen.add(pickIconMotion(() => i / 100))
    expect(seen).toEqual(new Set(['carousel', ...ICON_MOTIONS]))
    expect(pickIconMotion(() => 0.9999999)).toBe(ICON_MOTIONS[ICON_MOTIONS.length - 1])
  })
})

describe('IconMotionLoader', () => {
  it.each(['svg', 'img', 'canvas'])('sizes %s art to the slot, including reduced-motion pack canvases', tag => {
    // PackLoaderImage can swap img for canvas while the motion loader stays mounted.
    const rule = CSS.match(new RegExp(`\\.ilm-slot\\s*>\\s*${tag}(?=\\s*[,\\{])[^{}]*\\{([^}]*)\\}`))
    expect(rule, `missing slot-art sizing for ${tag}`).not.toBeNull()
    expect(rule![1]).toMatch(/(?:^|;)\s*display\s*:\s*block\s*(?:;|$)/)
    expect(rule![1]).toMatch(/(?:^|;)\s*width\s*:\s*100%\s*(?:;|$)/)
    expect(rule![1]).toMatch(/(?:^|;)\s*height\s*:\s*100%\s*(?:;|$)/)
  })

  it.each(ICON_MOTIONS)('%s renders one aria-hidden slot per pick, at most four', motion => {
    const { getByTestId } = render(<IconMotionLoader motion={motion} icons={[Mark, Mark, Mark, Mark, Mark]} picks={[4, 0, 2, 1, 3]} />)
    const root = getByTestId('loader-motion')
    expect(root).toHaveAttribute('data-motion', motion)
    expect(root).toHaveAttribute('aria-hidden', 'true')
    expect(root.querySelectorAll('.ilm-slot .mark')).toHaveLength(4)
    expect(root.querySelector('.ilm-dot') !== null).toBe(motion === 'spotlight')
  })

  it('wraps picks from a larger pool instead of rendering undefined icons', () => {
    const { getByTestId } = render(<IconMotionLoader motion="drop" icons={[Mark, Mark]} picks={[5, 6, 7, 8]} />)
    expect(getByTestId('loader-motion').querySelectorAll('.mark')).toHaveLength(4)
  })

  it('renders nothing for an empty pool', () => {
    const { container } = render(<IconMotionLoader motion="shuffle" icons={[]} picks={[0, 1]} />)
    expect(container.innerHTML).toBe('')
  })
})
