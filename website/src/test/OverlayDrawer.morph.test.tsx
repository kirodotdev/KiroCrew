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

/** The framer-motion props this mock READS; every other prop lands in `rest` and
 *  is forwarded to the plain DOM element, which is what the index signature is for. */
interface MotionMockProps {
  [prop: string]: unknown
  children?: React.ReactNode
  initial?: { clipPath?: string }
  animate?: { clipPath?: string }
  exit?: { clipPath?: string }
  transition?: unknown
}

// Capture what framer-motion is ASKED to animate. The real library cannot run
// projection in jsdom, so the props are the observable surface.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const make = (tag: string) =>
    React.forwardRef((props: MotionMockProps, ref: React.Ref<unknown>) => {
      const { children, initial, animate, exit, transition: _transition, ...rest } = props
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
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
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

describe('OverlayDrawer slide mode', () => {
  // The framer-motion mock forwards every prop it does not read (`style`,
  // `x`, `width`) straight onto the DOM element, so the merged style object is
  // observable there. `x` is a MotionValue in production; here it is passed as a
  // plain value purely to assert it survives the merge and is not overridden by
  // `slideStyle`.
  const X = 'X-MOTION-VALUE' as unknown as import('framer-motion').MotionValue<number>

  const slidePanel = (over: Partial<React.ComponentProps<typeof OverlayDrawer>> = {}) => {
    const { container } = render(
      <OverlayDrawer open width={320} slideX={X} {...over}>
        <div data-testid="panel">panel</div>
      </OverlayDrawer>,
    )
    return container.querySelector('.overflow-hidden') as HTMLElement
  }

  it('merges the caller vertical inset and keeps x/width unoverridden', () => {
    reduceMotion = false
    const panel = slidePanel({ slideStyle: { marginTop: 58, marginBottom: 400 } })
    const style = panel.getAttribute('style') ?? ''
    // The caller's vertical inset lands as BLOCK-AXIS MARGINS, so the panel's own
    // `top`/`bottom` CSS edges (which carry `env()` safe insets no script can
    // read) keep owning where the box starts and ends.
    expect(style).toContain('margin-top: 58px')
    expect(style).toContain('margin-bottom: 400px')
    // …the width the caller passed survives the merge…
    expect(style).toContain('width: 320px')
    // …and `x` (the transform channel, a MotionValue in production) is still
    // applied — slideStyle was spread first, so it cannot displace it. A
    // competing vertical transform would be a bug; the inset is margin-only.
    expect(style).toContain('x: X-MOTION-VALUE')
    expect(style).not.toContain('translateY')
    // The inset must not restate an edge either: `top` belongs to the className.
    // Anchored to a declaration boundary so `margin-top:` does not satisfy it.
    expect(style).not.toMatch(/(?:^|;\s*)top:/)
    expect(panel.querySelector('[data-testid="panel"]')).toBeTruthy()
  })

  it('is a no-op when no vertical inset is supplied (the desktop-free slide branch)', () => {
    reduceMotion = false
    const panel = slidePanel()
    const style = panel.getAttribute('style') ?? ''
    // Nothing forces a margin when the caller passes no slideStyle; x/width remain.
    expect(style).not.toContain('margin')
    expect(style).toContain('width: 320px')
    expect(style).toContain('x: X-MOTION-VALUE')
    expect(panel.querySelector('[data-testid="panel"]')).toBeTruthy()
  })
})
