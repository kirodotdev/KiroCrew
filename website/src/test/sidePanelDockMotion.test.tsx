import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { AnimatePresence, motion } from 'framer-motion'
import { sidePanelDockMotion } from '../pages/chat/sidePanelMount'

/** The dock wrapper as ChatPage renders it: ONE stable key across a dock flip,
 *  so framer-motion re-renders the same DOM node instead of remounting it. The
 *  size classes are the ones the flip swaps between. */
function DockWrapper({ dock }: { dock: 'right' | 'bottom' }) {
  const anim = sidePanelDockMotion(dock)
  return (
    <AnimatePresence initial={false}>
      <motion.div
        key="side-panel"
        data-testid="dock-wrapper"
        initial={anim.initial}
        animate={anim.animate}
        exit={anim.exit}
        transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
        className={dock === 'bottom' ? 'w-full overflow-visible flex flex-col justify-end' : 'h-full overflow-visible flex justify-end'}
      >
        <div style={{ width: 320, height: 240 }}>panel</div>
      </motion.div>
    </AnimatePresence>
  )
}

const settle = () => new Promise(r => setTimeout(r, 400))

describe('sidePanelDockMotion', () => {
  it('names BOTH axes in every target so neither can be un-targeted', () => {
    for (const dock of ['right', 'bottom'] as const) {
      const anim = sidePanelDockMotion(dock)
      for (const phase of ['initial', 'animate', 'exit'] as const) {
        expect(Object.keys(anim[phase]).sort()).toEqual(['height', 'opacity', 'width'])
      }
    }
  })

  it('holds the cross axis full-bleed while the other one travels 0 -> auto', () => {
    const right = sidePanelDockMotion('right')
    expect(right.animate).toMatchObject({ width: 'auto', height: '100%' })
    expect(right.initial).toMatchObject({ width: 0, height: '100%' })
    expect(right.exit).toMatchObject({ width: 0, height: '100%' })

    const bottom = sidePanelDockMotion('bottom')
    expect(bottom.animate).toMatchObject({ height: 'auto', width: '100%' })
    expect(bottom.initial).toMatchObject({ height: 0, width: '100%' })
    expect(bottom.exit).toMatchObject({ height: 0, width: '100%' })
  })

  /** The regression: with only the travelling axis named, flipping right ->
   *  bottom -> right left the wrapper's height frozen inline at whatever the
   *  bottom row had resolved to. An inline style outranks the `h-full` class, so
   *  the right-docked panel came back that short instead of full height. jsdom
   *  cannot lay out, so the frozen value surfaces here as `0px` rather than the
   *  real browser's px height — either way it is not the `100%` the class asks
   *  for, which is what this pins. */
  it('leaves no collapsed inline height after right -> bottom -> right', async () => {
    const { getByTestId, rerender } = render(<DockWrapper dock="right" />)
    const el = getByTestId('dock-wrapper')
    await settle()

    rerender(<DockWrapper dock="bottom" />)
    await settle()
    expect(el.style.width).toBe('100%')

    rerender(<DockWrapper dock="right" />)
    await settle()
    expect(el.style.height).toBe('100%')
    expect(el.style.height).not.toBe('0px')
  })

  /** Mirror direction: the width must not come back collapsed either. */
  it('leaves no collapsed inline width after bottom -> right -> bottom', async () => {
    const { getByTestId, rerender } = render(<DockWrapper dock="bottom" />)
    const el = getByTestId('dock-wrapper')
    await settle()

    rerender(<DockWrapper dock="right" />)
    await settle()
    expect(el.style.height).toBe('100%')

    rerender(<DockWrapper dock="bottom" />)
    await settle()
    expect(el.style.width).toBe('100%')
    expect(el.style.width).not.toBe('0px')
  })
})
