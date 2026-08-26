/**
 * OverlayDrawer morph clip geometry.
 *
 * The sidebar panel never moves or deforms — only its VISIBLE WINDOW morphs, a
 * clip-path inset animating between the panel rect and the toggle button's
 * rect. This test pins the inset ARITHMETIC, which is where the illusion lives:
 * a wrong side turns "panel converging into the button" into "panel sliding off
 * the wrong edge", and a negative inset drops the clip entirely so the panel
 * flashes full-size.
 *
 * Locks the contract:
 *  (1) The open clip is the full panel rect; the collapse target is the button.
 *  (2) `expandFrom` overrides the OPENING clip only — collapse still converges
 *      on the button, because that is where the button actually is.
 *  (3) A rect wider or taller than the panel clamps instead of emitting a
 *      negative inset.
 *  (4) Reduced motion, a zero width, or a missing morphTarget disables the clip
 *      path entirely rather than emitting a broken one.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import OverlayDrawer from '../components/OverlayDrawer'

let reduceMotion = false

// Capture what framer-motion is ASKED to animate. The real library cannot run
// projection in jsdom, so the props are the observable surface.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const { children, initial, animate, exit, transition, ...rest } = props
      return React.createElement(tag, {
        ...rest,
        ref,
        'data-initial-clip': initial?.clipPath ?? '',
        'data-animate-clip': animate?.clipPath ?? '',
        'data-exit-clip': typeof exit?.clipPath === 'string' ? exit.clipPath : '',
      }, children)
    })
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }),
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => reduceMotion,
  }
})

const BTN = { x: 8, y: 9, size: 28 }

function mount(over: Partial<React.ComponentProps<typeof OverlayDrawer>> = {}) {
  return render(
    <OverlayDrawer
      open
      width={260}
      morph
      morphTarget={BTN}
      contentH={600}
      {...over}
    >
      <div data-testid="panel">panel</div>
    </OverlayDrawer>,
  )
}

/** The clipped inner element is the one carrying clip props. */
const clipped = (c: HTMLElement) => c.querySelector('[data-initial-clip]:not([data-initial-clip=""])')
  ?? c.querySelector('[data-animate-clip]')!

describe('OverlayDrawer morph clip', () => {
  it('animates from the button rect to the full panel rect', () => {
    reduceMotion = false
    const { container } = mount()
    const el = clipped(container)!
    // right = width - x - size = 260 - 8 - 28 = 224
    // bottom = contentH - y - size = 600 - 9 - 28 = 563
    expect(el.getAttribute('data-initial-clip')).toBe('inset(9px 224px 563px 8px round 6px)')
    expect(el.getAttribute('data-animate-clip')).toBe('inset(0px 0px 0px 0px round 12px)')
  })

  it('collapses back onto the button rect', () => {
    reduceMotion = false
    const { container } = mount()
    expect(clipped(container)!.getAttribute('data-exit-clip')).toBe('inset(9px 224px 563px 8px round 6px)')
  })

  it('expandFrom replaces the OPENING clip so the panel grows out of the flyout', () => {
    reduceMotion = false
    const { container } = mount({ expandFrom: { x: 8, y: 9, w: 244, h: 300 } })
    const el = clipped(container)!
    // right = 260 - 8 - 244 = 8, bottom = 600 - 9 - 300 = 291, radius matches
    // the flyout's rounded-xl so corner curvature is continuous.
    expect(el.getAttribute('data-initial-clip')).toBe('inset(9px 8px 291px 8px round 12px)')
  })

  it('expandFrom does NOT change where the panel collapses to', () => {
    reduceMotion = false
    const { container } = mount({ expandFrom: { x: 8, y: 9, w: 244, h: 300 } })
    expect(clipped(container)!.getAttribute('data-exit-clip')).toBe('inset(9px 224px 563px 8px round 6px)')
  })

  it('clamps a rect wider or taller than the panel instead of going negative', () => {
    reduceMotion = false
    const { container } = mount({ expandFrom: { x: 0, y: 0, w: 400, h: 900 } })
    // A negative inset is invalid CSS and drops the clip, flashing the panel
    // full-size. Every side must floor at 0.
    const clip = clipped(container)!.getAttribute('data-initial-clip')!
    expect(clip).toBe('inset(0px 0px 0px 0px round 12px)')
    expect(clip).not.toContain('-')
  })

  it('disables the clip under reduced motion', () => {
    reduceMotion = true
    const { container } = mount()
    expect(container.querySelector('[data-initial-clip]:not([data-initial-clip=""])')).toBeNull()
    reduceMotion = false
  })

  it('disables the clip when there is no morphTarget or no measured height', () => {
    reduceMotion = false
    const noTarget = mount({ morphTarget: undefined })
    expect(noTarget.container.querySelector('[data-initial-clip]:not([data-initial-clip=""])')).toBeNull()
    noTarget.unmount()

    // containerH starts at 0 before the ResizeObserver fires; a 0-height panel
    // would emit a bottom inset larger than the box.
    const unmeasured = mount({ contentH: 0 })
    expect(unmeasured.container.querySelector('[data-initial-clip]:not([data-initial-clip=""])')).toBeNull()
  })

  it('renders children in every mode', () => {
    for (const [reduce, morph] of [[false, true], [true, true], [false, false]] as const) {
      reduceMotion = reduce
      const view = mount({ morph })
      expect(view.getByTestId('panel')).toBeTruthy()
      view.unmount()
    }
    reduceMotion = false
  })
})
