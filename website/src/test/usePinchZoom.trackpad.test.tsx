import { render, act, cleanup } from '@testing-library/react'
import DiagramLightbox from '../components/DiagramLightbox'
import { Lightbox } from '../components/MarkdownRenderer'

// Trackpad magnification for the two surfaces that own their own zoom. A trackpad
// pinch emits NO pointer events, so it reaches none of the two-finger code: Blink
// sends `wheel` + ctrlKey, WebKit sends `gesturestart`/`gesturechange` with a
// cumulative `scale`. These specs pin that both signals are claimed, that a PLAIN
// wheel is not (the scroller needs it), and that both viewers get it from the one
// shared hook rather than one of them being wired and the other forgotten.

/** Built through the DOM rather than written as markup: the component takes a
 *  serialized SVG string, so that is what this is. Authored SVG markup carrying a
 *  viewBox is also what the use-lucide-icons rule blocks in a `.tsx`. */
function svgFixture(attrs: Record<string, string>): string {
  const NS = 'http://www.w3.org/2000/svg'
  const el = document.createElementNS(NS, 'svg')
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v)
  const label = document.createElementNS(NS, 'text')
  label.setAttribute('x', '1')
  label.setAttribute('y', '9')
  label.textContent = 'n1'
  el.appendChild(label)
  return el.outerHTML
}

const WITH_VIEWBOX = svgFixture({ viewBox: '0 0 100 100' })
/** No viewBox: cannot fit-scale, so it keeps natural size and gets no transform. */
const NO_VIEWBOX = svgFixture({ width: '4000', height: '3000' })

/** The diagram viewer portals to the body, so query there and not the container. */
function diagramHost(): HTMLElement {
  return document.body.querySelector('[role="dialog"] .overflow-auto > div') as HTMLElement
}

function sizeBox(el: HTMLElement, w = 800, h = 600) {
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: w })
  Object.defineProperty(el, 'offsetHeight', { configurable: true, value: h })
}

/** Cancelable so `defaultPrevented` is observable — claiming the event is the
 *  half that stops the browser applying its own page zoom on top.
 *
 *  `ctrlKey` and the coordinates are assigned AFTER construction on purpose:
 *  jsdom's `WheelEvent` constructor silently drops every field it inherits from
 *  MouseEvent, and `fireEvent.wheel(el, { ctrlKey: true })` drops them too. A
 *  missing `ctrlKey` reads as "the handler never ran", and a missing `clientX`
 *  poisons the anchor arithmetic into `NaN` — neither looks like a broken
 *  fixture. */
function wheel(el: HTMLElement, deltaY: number, ctrlKey: boolean, x = 300, y = 500, deltaMode = 0) {
  const e = new WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true })
  Object.defineProperty(e, 'ctrlKey', { value: ctrlKey })
  Object.defineProperty(e, 'clientX', { value: x })
  Object.defineProperty(e, 'clientY', { value: y })
  // Same jsdom trap as the fields above: the constructor drops `deltaMode` too,
  // and a dropped 0 is indistinguishable from a dropped 1 by eye.
  Object.defineProperty(e, 'deltaMode', { value: deltaMode })
  act(() => { el.dispatchEvent(e) })
  return e
}

/** jsdom has no GestureEvent; WebKit's is a plain Event carrying these fields. */
function gesture(el: HTMLElement, type: string, scale: number, x = 300, y = 500) {
  const e = new Event(type, { bubbles: true, cancelable: true })
  Object.assign(e, { scale, clientX: x, clientY: y })
  act(() => { el.dispatchEvent(e) })
  return e
}

function openImage() {
  window.dispatchEvent(new CustomEvent('lightbox', {
    detail: { images: [{ src: 'a.png', alt: 'a' }], index: 0 },
  }))
}

/** jsdom implements no `matchMedia`, and the hook treats its absence as a COARSE
 *  pointer, so every gesture spec below has to declare the pointer class it means.
 *  Fine is the default here because these specs are about trackpads; the coarse
 *  case is asserted explicitly, since that is where the double-zoom lived. */
function setPointer(fine: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      matches: query.includes('pointer: fine') ? fine : !fine,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  })
}

beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
  setPointer(true)
})

describe('trackpad magnification, in the diagram viewer', () => {
  it('zooms on ctrl+wheel and anchors the scale at the cursor', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = diagramHost()
    sizeBox(h)

    // deltaY -100 exponentiates to e, which the per-event clamp holds at 1.25 so a
    // single mouse notch is a step rather than a jump.
    wheel(h, -100, true)

    // Anchoring: the point under (300,500) is at content-local (100,100) with the
    // viewport centre at (200,400) and no pan yet, so holding it there at 1.25
    // puts the pan at 100 - 100*1.25 = -25 on x. On y the clamp wins: the content
    // is 600*1.25 = 750 tall against an 800 viewport, so it cannot travel at all.
    const style = h.getAttribute('style') ?? ''
    expect(style).toContain('scale(1.25)')
    expect(style).toContain('translate(-25px, 0px)')
  })

  it('claims the event, so the browser does not also page-zoom', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    sizeBox(diagramHost())
    expect(wheel(diagramHost(), -100, true).defaultPrevented).toBe(true)
  })

  it('leaves a PLAIN wheel alone, because a scroller may own it', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = diagramHost()
    sizeBox(h)
    const e = wheel(h, -100, false)
    // Untouched: not claimed, and no zoom applied. A no-viewBox diagram keeps its
    // natural size and reaches its edges by scrolling, which this must not eat.
    expect(e.defaultPrevented).toBe(false)
    expect(h.getAttribute('style') ?? '').toContain('scale(1)')
  })

  it('zooms out on the opposite wheel direction, and stops at fit', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = diagramHost()
    sizeBox(h)
    wheel(h, -100, true)
    expect(h.getAttribute('style') ?? '').toContain('scale(1.25)')
    // Back down, then past the floor: the clamp holds it at fit rather than
    // inverting the image.
    wheel(h, 100, true)
    wheel(h, 100, true)
    expect(h.getAttribute('style') ?? '').toContain('scale(1)')
  })

  it('zooms on a WebKit gesture, reading `scale` as cumulative', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = diagramHost()
    sizeBox(h)
    gesture(h, 'gesturestart', 1)
    // Two frames of the SAME gesture. `scale` is cumulative from the start, so the
    // per-frame factors are 1.5 then 3/1.5 = 2, and from fit the zoom lands on the
    // cumulative value itself: 3. Reading `scale` as a per-frame factor instead
    // would compound it to 1.5 * 3 = 4.5, so this number is what tells the two
    // readings apart.
    gesture(h, 'gesturechange', 1.5)
    gesture(h, 'gesturechange', 3)
    expect(h.getAttribute('style') ?? '').toContain('scale(3)')
  })

  it('starts each gesture from a fresh baseline, not the previous one', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = diagramHost()
    sizeBox(h)
    gesture(h, 'gesturestart', 1)
    gesture(h, 'gesturechange', 2)
    gesture(h, 'gestureend', 2)
    expect(h.getAttribute('style') ?? '').toContain('scale(2)')
    // A SECOND gesture also reports `scale` from 1. Without resetting the stored
    // scale on start, its first frame would read a factor of 2/2 = 1 and the
    // gesture would appear dead until it passed the previous one's magnitude.
    gesture(h, 'gesturestart', 1)
    gesture(h, 'gesturechange', 1.5)
    expect(h.getAttribute('style') ?? '').toContain('scale(3)')
  })

  it('ignores a wheel outside the zoom target', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    sizeBox(diagramHost())
    const outside = document.body
    const e = new WheelEvent('wheel', { deltaY: -100, ctrlKey: true, bubbles: false, cancelable: true })
    act(() => { outside.dispatchEvent(e) })
    expect(e.defaultPrevented).toBe(false)
    expect(diagramHost().getAttribute('style') ?? '').toContain('scale(1)')
  })

  it('claims a gesture over the overlay padding, not only over the SVG', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    sizeBox(diagramHost())
    // The area around a fit-scaled diagram is visually part of the viewer, so the
    // gesture is claimed from the dialog root down rather than from the SVG host.
    const overlay = document.body.querySelector('[role="dialog"]') as HTMLElement
    expect(wheel(overlay, -100, true).defaultPrevented).toBe(true)
    expect(diagramHost().getAttribute('style') ?? '').toContain('scale(1.25)')
  })

  it('does NOT claim the gesture on a no-viewBox diagram, leaving page zoom intact', () => {
    render(<DiagramLightbox svg={NO_VIEWBOX} onClose={() => {}} />)
    const h = diagramHost()
    sizeBox(h)
    // Not fit-scaled, so it gets no transform — and page zoom, unlike on
    // fit-to-viewport content, genuinely DOES magnify it. Claiming the pinch here
    // would suppress that and leave no magnification path at all.
    const e = wheel(h, -100, true)
    expect(e.defaultPrevented).toBe(false)
    expect(h.getAttribute('style')).toBeNull()
  })
})

describe('trackpad magnification, in the image viewer', () => {
  // The point of the shared hook: wiring it once gave BOTH surfaces this. A spec
  // here is what stops the image viewer being the one that silently lacks it —
  // which is exactly how the diagram viewer shipped without a pinch.
  it('zooms an open image on ctrl+wheel', () => {
    const { container } = render(<Lightbox />)
    act(() => openImage())
    const img = container.querySelector('img') as HTMLElement
    sizeBox(img)

    wheel(img, -100, true)

    expect(img.getAttribute('style') ?? '').toContain('scale(1.25)')
  })

  it('claims the event there too', () => {
    const { container } = render(<Lightbox />)
    act(() => openImage())
    const img = container.querySelector('img') as HTMLElement
    sizeBox(img)
    expect(wheel(img, -100, true).defaultPrevented).toBe(true)
  })

  it('claims a pinch on the backdrop around a small image', () => {
    const { container } = render(<Lightbox />)
    act(() => openImage())
    const img = container.querySelector('img') as HTMLElement
    sizeBox(img)
    // A small image leaves most of the full-screen overlay as letterbox. That area
    // is visually the viewer, so a pinch there must zoom rather than fall through
    // and page-zoom the dashboard behind a viewer that looks unchanged.
    const overlay = container.querySelector('[role="button"]') as HTMLElement
    expect(wheel(overlay, -100, true).defaultPrevented).toBe(true)
    expect(img.getAttribute('style') ?? '').toContain('scale(1.25)')
  })

  it('does nothing while no image is open, since the target is absent', () => {
    render(<Lightbox />)
    // `Lightbox` mounts once for the app's lifetime and returns null when closed.
    // A ctrl+wheel then belongs to the page — and nothing of ours should be
    // listening, because a non-passive `wheel` listener taxes every scroll.
    const e = new WheelEvent('wheel', { deltaY: -100, ctrlKey: true, bubbles: true, cancelable: true })
    act(() => { document.body.dispatchEvent(e) })
    expect(e.defaultPrevented).toBe(false)
  })

  it('binds no wheel listener at all while closed, and binds one when opened', () => {
    // The behavioural spec above cannot tell "listener absent" from "listener
    // present but declining" — and the whole point of gating is that none is
    // REGISTERED, since non-passive registration is itself the cost.
    const original = window.addEventListener.bind(window)
    let wheelBinds = 0
    const spy = vi.spyOn(window, 'addEventListener').mockImplementation(
      ((type: string, listener: EventListenerOrEventListenerObject, opts?: unknown) => {
        if (type === 'wheel') wheelBinds++
        original(type, listener, opts as AddEventListenerOptions | undefined)
      }) as typeof window.addEventListener,
    )
    try {
      render(<Lightbox />)
      expect(wheelBinds, 'a closed viewer must not register a non-passive wheel listener').toBe(0)
      act(() => openImage())
      expect(wheelBinds, 'opening the viewer is what registers it').toBe(1)
    } finally {
      spy.mockRestore()
    }
  })
})

// The regression a maintainer caught on real iOS Safari: WebKit's `gesture*` is
// NOT trackpad-exclusive. iOS fires it for a two-finger TOUCH pinch, and those
// same two fingers are already driving the hook's pointer path — so before the
// media-query gate, one pinch was scaled twice by two independent formulas.
describe('a coarse pointer, where gesture events mean touch and not a trackpad', () => {
  it('ignores gesturechange, so a touch pinch is scaled once by the pointer path', () => {
    setPointer(false)
    const { container } = render(<DiagramLightbox svg={WITH_VIEWBOX} open onClose={() => {}} />)
    void container
    const host = diagramHost()
    sizeBox(host)
    const before = host.style.transform

    gesture(host, 'gesturestart', 1)
    const e = gesture(host, 'gesturechange', 2)

    expect(host.style.transform, 'a coarse-pointer gesture must not scale').toBe(before)
    expect(e.defaultPrevented, 'and must not be claimed, or iOS loses its own handling').toBe(false)
  })

  it('registers no gesture listener at all under a coarse pointer', () => {
    // Behaviour alone cannot separate "declined the event" from "never bound it",
    // and the fix is specifically about NOT BINDING: a bound-but-declining
    // listener would still have claimed the event above.
    const original = window.addEventListener.bind(window)
    const bound: string[] = []
    const spy = vi.spyOn(window, 'addEventListener').mockImplementation(
      ((type: string, listener: EventListenerOrEventListenerObject, opts?: unknown) => {
        if (type.startsWith('gesture')) bound.push(type)
        original(type, listener, opts as AddEventListenerOptions | undefined)
      }) as typeof window.addEventListener,
    )
    try {
      setPointer(false)
      render(<DiagramLightbox svg={WITH_VIEWBOX} open onClose={() => {}} />)
      expect(bound, 'a touch device must bind none of the three').toEqual([])

      // And the same viewer under a fine pointer still binds all three, so the
      // spec above is a real gate rather than a broken fixture.
      cleanup()
      setPointer(true)
      render(<DiagramLightbox svg={WITH_VIEWBOX} open onClose={() => {}} />)
      expect(bound.sort()).toEqual(['gesturechange', 'gestureend', 'gesturestart'])
    } finally {
      spy.mockRestore()
    }
  })

  it('still zooms on ctrl+wheel, which the pointer class must not gate', () => {
    // A coarse-pointer device can carry a mouse or an attached keyboard; the fix
    // was scoped to `gesture*` precisely so this path is untouched.
    setPointer(false)
    render(<DiagramLightbox svg={WITH_VIEWBOX} open onClose={() => {}} />)
    const host = diagramHost()
    sizeBox(host)

    const e = wheel(host, -100, true)

    expect(e.defaultPrevented, 'ctrl+wheel is still claimed under a coarse pointer').toBe(true)
    expect(host.style.transform).toMatch(/scale\(/)
  })
})

// `deltaY` has no intrinsic unit; `deltaMode` names it. Firefox reports a mouse
// notch as DOM_DELTA_LINE with deltaY≈3, so treating that as pixels made the same
// physical notch zoom ~8x slower than in Chrome's pixel mode.
describe('wheel deltaMode normalisation', () => {
  function scaleOf(el: HTMLElement): number {
    const m = /scale\(([\d.]+)\)/.exec(el.style.transform)
    return m ? Number(m[1]) : 1
  }

  /** One notch per engine: Chrome 100 pixels, Firefox 3 lines. Same gesture. */
  function zoomForNotch(deltaY: number, deltaMode: number): number {
    cleanup()
    render(<DiagramLightbox svg={WITH_VIEWBOX} open onClose={() => {}} />)
    const host = diagramHost()
    sizeBox(host)
    wheel(host, deltaY, true, 300, 500, deltaMode)
    return scaleOf(host)
  }

  it('gives a line-mode notch the same zoom as a pixel-mode notch', () => {
    const pixel = zoomForNotch(-100, 0)
    const line = zoomForNotch(-3, 1)

    expect(pixel).toBeGreaterThan(1)
    // Within 2%: 33 is 100/3 rounded, so the two are equal by construction rather
    // than by coincidence, and the clamp bounds whatever residue the rounding leaves.
    expect(line).toBeCloseTo(pixel, 2)
  })

  it('does not read a line-mode notch as a 3-pixel nudge', () => {
    // The regression's signature: unnormalised, deltaY=-3 gives exp(0.03)≈1.03.
    // This is the assertion that fails on the old code, so it is what pins the fix.
    const line = zoomForNotch(-3, 1)

    expect(line, 'a line notch must not degrade to a ~3% step').toBeGreaterThan(1.1)
  })

  it('treats page mode as a viewport-sized delta rather than a few pixels', () => {
    // DOM_DELTA_PAGE is rare but is part of the same enum; unhandled it would be
    // an even smaller nudge than line mode. The clamp is what keeps it sane.
    const page = zoomForNotch(-1, 2)

    expect(page).toBeGreaterThan(1.1)
    expect(page, 'and the per-event clamp still bounds it').toBeLessThanOrEqual(1.25)
  })
})
