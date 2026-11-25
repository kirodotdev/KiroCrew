import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const env = vi.hoisted(() => ({ mobile: false, reduced: false }))
vi.mock('../../hooks/useIsMobile', async importOriginal => ({
  ...(await importOriginal<typeof import('../../hooks/useIsMobile')>()),
  useIsMobile: () => env.mobile,
}))
vi.mock('../../hooks/useReducedMotion', () => ({ useReducedMotion: () => env.reduced }))

import { LookingGhost } from './LookingGhost'
import { markPaths } from '../../lib/kiroGhostAvatar'
import markSvg from '../kiro-ghost-mark.svg?raw'
import bodySvg from './looking-ghost-body.svg?raw'
import eyesSvg from './looking-ghost-eyes.svg?raw'

/** The pointermove listeners LookingGhost put on the window during the test. */
function pointerListeners(spy: ReturnType<typeof vi.spyOn>) {
  return spy.mock.calls.filter(([type]) => type === 'pointermove')
}

describe('LookingGhost', () => {
  let add: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    env.mobile = false
    env.reduced = false
    add = vi.spyOn(window, 'addEventListener')
  })
  afterEach(() => add.mockRestore())

  it('draws the ghost as two image layers inside the disc, eyes on top', () => {
    render(<LookingGhost size={72} />)
    const disc = screen.getByTestId('looking-ghost')
    expect(disc).toHaveStyle({ width: '72px', height: '72px' })
    expect(disc).toHaveAttribute('aria-hidden', 'true')
    const imgs = disc.querySelectorAll('img')
    expect(imgs).toHaveLength(2)
    expect(imgs[1]).toBe(screen.getByTestId('looking-ghost-eyes'))
  })

  it('keeps its asset layers in step with the brand mark', () => {
    const [body, eyeA, eyeB] = markPaths(markSvg)
    const paths = (svg: string) => [...svg.matchAll(/\sd="([^"]+)"/g)].map((m) => m[1])
    expect(paths(bodySvg)).toEqual([body])
    expect(paths(eyesSvg)).toEqual([eyeA, eyeB])
  })

  it('follows the pointer on desktop', () => {
    const { unmount } = render(<LookingGhost />)
    expect(pointerListeners(add)).toHaveLength(1)
    const remove = vi.spyOn(window, 'removeEventListener')
    unmount()
    expect(remove.mock.calls.filter(([type]) => type === 'pointermove')).toHaveLength(1)
    remove.mockRestore()
  })

  it('aims the eyes from the disc it measures on each pointer move', () => {
    render(<LookingGhost size={72} />)
    const disc = screen.getByTestId('looking-ghost')
    const rect = vi.spyOn(disc, 'getBoundingClientRect').mockReturnValue(
      { left: 100, top: 100, width: 72, height: 72, right: 172, bottom: 172, x: 100, y: 100, toJSON: () => ({}) },
    )
    const [[, onMove]] = pointerListeners(add) as [[string, (e: Pick<PointerEvent, 'clientX' | 'clientY'>) => void]]
    // Far to the right, then right on the eyes' anchor (a zero distance must not divide by zero).
    expect(() => {
      onMove({ clientX: 900, clientY: 140 })
      onMove({ clientX: 100 + 72 * 0.56, clientY: 100 + 72 * 0.72 })
    }).not.toThrow()
    expect(rect).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['on a phone', { mobile: true, reduced: false }],
    ['under reduced motion', { mobile: false, reduced: true }],
  ])('keeps its eyes still %s', (_label, setting) => {
    Object.assign(env, setting)
    render(<LookingGhost />)
    expect(pointerListeners(add)).toHaveLength(0)
    expect(screen.getByTestId('looking-ghost-eyes')).toBeInTheDocument()
  })
})
