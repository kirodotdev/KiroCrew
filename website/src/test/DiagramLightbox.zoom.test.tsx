import { render, fireEvent, act } from '@testing-library/react'
import DiagramLightbox from '../components/DiagramLightbox'

// Diagram viewer magnification. #6000 turned page zoom off shell-wide on the rule
// that "a surface that must magnify owns its own zoom" — and this viewer shipped
// without owning one, which left a diagram's labels unmagnifiable by any gesture
// (#6107). These specs pin the gesture, and pin the ONE case where zoom is
// deliberately absent: an SVG with no viewBox is not fit-scaled, so the
// surrounding scroller (not a transform) is what reaches its edges.

/** Mermaid-shaped output. Built through the DOM rather than written as markup:
 *  the component takes a *serialized* SVG string and inserts it with
 *  `createContextualFragment`, so a serialized element is what the fixture
 *  actually is. Authored SVG markup carrying a viewBox attribute is also what
 *  the use-lucide-icons rule blocks in a `.tsx` — correctly, since that shape is
 *  normally a hand-rolled icon — so constructing the fixture keeps that rule
 *  intact instead of carving out a test exemption for it.
 *
 *  (The rule greps diff text, so even prose quoting the pattern trips it. This
 *  comment is worded to describe the shape rather than spell it.) */
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

/** A viewBox is what makes fit-scaling possible, and therefore what makes zoom
 *  the right mechanism for this content. */
const WITH_VIEWBOX = svgFixture({ viewBox: '0 0 100 100' })
/** No viewBox: cannot fit-scale, so it keeps natural size and the scroller — not
 *  a transform — is what reaches its edges. */
const NO_VIEWBOX = svgFixture({ width: '4000', height: '3000' })

/** The transform target is the SVG host — the inner child of the scroller.
 *  Queried from `document.body`, not the render container: this viewer portals
 *  itself to the body, so the container is empty. */
function host(): HTMLElement {
  const el = document.body.querySelector('[role="dialog"] .overflow-auto > div')
  return el as HTMLElement
}

function overlay(): HTMLElement {
  return document.body.querySelector('[role="dialog"]') as HTMLElement
}

const touch = (x: number, y: number, pointerId = 1) =>
  ({ pointerType: 'touch', pointerId, clientX: x, clientY: y })

/** jsdom gives every element a zero layout box, so the pan clamp would pin every
 *  candidate to 0 and no spec could observe a pan. Give the host a real box. */
function sizeHost(el: HTMLElement, w = 800, h = 600) {
  Object.defineProperty(el, 'offsetWidth', { configurable: true, value: w })
  Object.defineProperty(el, 'offsetHeight', { configurable: true, value: h })
}

beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 400 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
})

describe('DiagramLightbox magnification', () => {
  it('starts at fit and scales on a two-finger pinch', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    expect(h.getAttribute('style')).toContain('scale(1)')
    // Two contacts 100px apart, then spread to 200px → 2x.
    act(() => { fireEvent.pointerDown(h, touch(150, 400, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(250, 400, 2)) })
    act(() => { fireEvent.pointerMove(h, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(h, touch(300, 400, 2)) })
    expect(h.getAttribute('style')).toContain('scale(2)')
  })

  it('anchors the pinch at the gesture midpoint rather than the element centre', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    // Pinch centred well away from the viewport centre (200,400).
    act(() => { fireEvent.pointerDown(h, touch(60, 200, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(140, 200, 2)) })
    act(() => { fireEvent.pointerMove(h, touch(20, 200, 1)) })
    act(() => { fireEvent.pointerMove(h, touch(180, 200, 2)) })
    const style = h.getAttribute('style') ?? ''
    expect(style).toContain('scale(2)')
    // An unanchored zoom would leave translate at 0,0; anchoring must move it.
    expect(style).not.toContain('translate(0px, 0px)')
  })

  it('keeps the pinch alive when a third finger lands and one of the first two lifts', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    act(() => { fireEvent.pointerDown(h, touch(150, 400, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(250, 400, 2)) })
    act(() => { fireEvent.pointerMove(h, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(h, touch(300, 400, 2)) })
    expect(h.getAttribute('style')).toContain('scale(2)')
    // Third finger joins, then the first lifts — two contacts remain, so the
    // gesture must continue from the CURRENT zoom rather than dying.
    act(() => { fireEvent.pointerDown(h, touch(350, 400, 3)) })
    act(() => { fireEvent.pointerUp(h, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(h, touch(400, 400, 3)) })
    const style = h.getAttribute('style') ?? ''
    expect(style).toMatch(/scale\((?!1\))/)
  })

  it('toggles fit <-> 2.5x on a double-tap and back', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    act(() => { fireEvent.pointerDown(h, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerUp(h, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(202, 402, 2)) })
    expect(h.getAttribute('style')).toContain('scale(2.5)')
    // A second double-tap returns to fit with the pan cleared.
    act(() => { fireEvent.pointerUp(h, touch(202, 402, 2)) })
    act(() => { fireEvent.pointerDown(h, touch(202, 402, 3)) })
    act(() => { fireEvent.pointerUp(h, touch(202, 402, 3)) })
    act(() => { fireEvent.pointerDown(h, touch(203, 403, 4)) })
    const style = h.getAttribute('style') ?? ''
    expect(style).toContain('scale(1)')
    expect(style).toContain('translate(0px, 0px)')
  })

  it('does not read two far-apart taps as one double-tap', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    act(() => { fireEvent.pointerDown(h, touch(50, 100, 1)) })
    act(() => { fireEvent.pointerUp(h, touch(50, 100, 1)) })
    // Same time window, but well beyond DOUBLE_TAP_SLOP (32px).
    act(() => { fireEvent.pointerDown(h, touch(300, 700, 2)) })
    expect(h.getAttribute('style')).toContain('scale(1)')
  })

  it('does not read two quick flick-pans as a double-tap that resets the zoom', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    // Get zoomed by a legitimate double-tap first.
    act(() => { fireEvent.pointerDown(h, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerUp(h, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(202, 402, 2)) })
    act(() => { fireEvent.pointerUp(h, touch(202, 402, 2)) })
    expect(h.getAttribute('style')).toContain('scale(2.5)')

    // Now traverse the zoomed diagram the natural way: two quick flick-pans
    // beginning near the same point. Each committed drag must retire its own tap
    // candidate, or the second flick's pointerdown lands inside the double-tap
    // window and snaps the diagram back to fit — losing the position mid-pan.
    act(() => { fireEvent.pointerDown(h, touch(300, 500, 3)) })
    act(() => { fireEvent.pointerMove(h, touch(360, 540, 3)) })
    act(() => { fireEvent.pointerUp(h, touch(360, 540, 3)) })
    act(() => { fireEvent.pointerDown(h, touch(305, 505, 4)) })
    act(() => { fireEvent.pointerMove(h, touch(365, 545, 4)) })
    act(() => { fireEvent.pointerUp(h, touch(365, 545, 4)) })

    expect(h.getAttribute('style')).toContain('scale(2.5)')
  })

  it('does not let a pinch leave a tap candidate a later tap can complete', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    // A pinch's FIRST contact runs the tap path (one contact is not yet a pinch),
    // so seating the second must retire that candidate. Otherwise a single tap
    // near the same point after the pinch lifts completes a double-tap nobody
    // performed, toggling the zoom the pinch just set.
    act(() => { fireEvent.pointerDown(h, touch(300, 500, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(340, 540, 2)) })
    act(() => { fireEvent.pointerUp(h, touch(340, 540, 2)) })
    act(() => { fireEvent.pointerUp(h, touch(300, 500, 1)) })
    const afterPinch = h.getAttribute('style') ?? ''

    act(() => { fireEvent.pointerDown(h, touch(302, 502, 3)) })
    expect(h.getAttribute('style') ?? '').toBe(afterPinch)
  })

  it('pans a zoomed diagram on a one-finger drag, and not at fit', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    const h = host()
    sizeHost(h)
    // At fit a drag must not pan — there is nothing off-screen to reach.
    act(() => { fireEvent.pointerDown(h, touch(200, 400, 1)) })
    act(() => { fireEvent.pointerMove(h, touch(260, 400, 1)) })
    expect(h.getAttribute('style')).toContain('translate(0px, 0px)')
    act(() => { fireEvent.pointerUp(h, touch(260, 400, 1)) })
    // Zoom in, then drag: now the pan moves.
    act(() => { fireEvent.pointerDown(h, touch(200, 400, 2)) })
    act(() => { fireEvent.pointerUp(h, touch(200, 400, 2)) })
    act(() => { fireEvent.pointerDown(h, touch(201, 401, 3)) })
    expect(h.getAttribute('style')).toContain('scale(2.5)')
    act(() => { fireEvent.pointerUp(h, touch(201, 401, 3)) })
    act(() => { fireEvent.pointerDown(h, touch(200, 400, 4)) })
    act(() => { fireEvent.pointerMove(h, touch(240, 400, 4)) })
    expect(h.getAttribute('style')).not.toContain('translate(0px, 0px)')
  })

  it('does not claim mouse pointers, so desktop click-out is untouched', () => {
    const onClose = vi.fn()
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={onClose} />)
    const h = host()
    sizeHost(h)
    act(() => { fireEvent.pointerDown(h, { pointerType: 'mouse', pointerId: 1, clientX: 200, clientY: 400 }) })
    act(() => { fireEvent.pointerDown(h, { pointerType: 'mouse', pointerId: 1, clientX: 202, clientY: 402 }) })
    expect(h.getAttribute('style')).toContain('scale(1)')
  })

  it('leaves a no-viewBox SVG unzoomed and scrollable, since it is not fit-scaled', () => {
    render(<DiagramLightbox svg={NO_VIEWBOX} onClose={() => {}} />)
    const h = host()
    // No transform at all: the scroller reaches this one's edges, and taking
    // native touch scrolling away from it (via touch-none) would crop it instead.
    expect(h.getAttribute('style')).toBeNull()
    expect(document.body.querySelector('.overflow-auto')).not.toBeNull()
    // Assert the whole ANCESTOR CHAIN, not just the host. `touch-action` resolves
    // from the hit-test target up to the element that would scroll, so a
    // `touch-none` anywhere above the scroller disables its touch panning just as
    // effectively as one on the host — and a host-only assertion is structurally
    // incapable of seeing it.
    const chain: string[] = []
    for (let el: HTMLElement | null = h; el && el !== document.body; el = el.parentElement) {
      chain.push(el.className)
      expect(el.className).not.toContain('touch-none')
    }
    // Cheap non-vacuity check: a chain of one would pass the loop trivially while
    // testing nothing about the ancestors this spec exists to cover.
    expect(chain.length).toBeGreaterThan(2)
  })

  it('opts a fit-scaled diagram out of the root pan-only touch-action', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={() => {}} />)
    // `touch-action` is intersected from the hit-test target up to the root, and
    // the shell's root is `pan-x pan-y` — without this the browser keeps the pan
    // and the two-finger gesture never reaches these handlers.
    expect(host().className).toContain('touch-none')
  })

  it('does not close on the click that follows a pinch', () => {
    const onClose = vi.fn()
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={onClose} />)
    const h = host()
    sizeHost(h)
    act(() => { fireEvent.pointerDown(h, touch(150, 400, 1)) })
    act(() => { fireEvent.pointerDown(h, touch(250, 400, 2)) })
    act(() => { fireEvent.pointerMove(h, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerMove(h, touch(300, 400, 2)) })
    act(() => { fireEvent.pointerUp(h, touch(100, 400, 1)) })
    act(() => { fireEvent.pointerUp(h, touch(300, 400, 2)) })
    // The click synthesised after the last finger lifts is gesture residue.
    act(() => { fireEvent.click(overlay()) })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('zooms in, out, and resets via toolbar buttons', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={vi.fn()} />)
    const findBtn = (pattern: RegExp) =>
      [...document.body.querySelectorAll('[role="dialog"] button')].find(b =>
        pattern.test(b.getAttribute('aria-label') || ''),
      ) as HTMLButtonElement

    const zoomInBtn = findBtn(/zoom in/i)
    const zoomOutBtn = findBtn(/zoom out/i)

    expect(zoomOutBtn.disabled).toBe(true)
    expect(zoomInBtn.disabled).toBe(false)

    act(() => { fireEvent.click(zoomInBtn) })
    expect(host().style.transform).toContain('scale(1.5)')
    expect(zoomOutBtn.disabled).toBe(false)

    const resetBtn = findBtn(/reset/i)
    expect(resetBtn).not.toBeNull()

    act(() => { fireEvent.click(zoomInBtn) })
    expect(host().style.transform).toContain('scale(2)')

    act(() => { fireEvent.click(zoomOutBtn) })
    expect(host().style.transform).toContain('scale(1.5)')

    act(() => { fireEvent.click(resetBtn) })
    expect(host().style.transform).toContain('scale(1)')
    expect(zoomOutBtn.disabled).toBe(true)
  })

  it('zooms in, out, and resets via keyboard shortcuts (+, -, 0)', () => {
    render(<DiagramLightbox svg={WITH_VIEWBOX} onClose={vi.fn()} />)
    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    expect(host().style.transform).toContain('scale(1.5)')

    act(() => { fireEvent.keyDown(window, { key: '=' }) })
    expect(host().style.transform).toContain('scale(2)')

    act(() => { fireEvent.keyDown(window, { key: '-' }) })
    expect(host().style.transform).toContain('scale(1.5)')

    act(() => { fireEvent.keyDown(window, { key: '0' }) })
    expect(host().style.transform).toContain('scale(1)')
  })

  it('gates zoom controls and keyboard shortcuts on fitted diagrams', () => {
    // A no-viewBox SVG is not fitted; zoom buttons must be absent and keys ignored
    render(<DiagramLightbox svg={NO_VIEWBOX} onClose={vi.fn()} />)
    const hasZoomIn = [...document.body.querySelectorAll('button')].some(button =>
      /zoom in/i.test(button.getAttribute('aria-label') || ''),
    )
    expect(hasZoomIn).toBe(false)

    act(() => { fireEvent.keyDown(window, { key: '+' }) })
    expect(host().style.transform).toBe('')
  })

  it('ignores zoom keys when focus is inside an editable input target', () => {
    render(
      <div>
        <input data-testid="inp" aria-label="test input" />
        <DiagramLightbox svg={WITH_VIEWBOX} onClose={vi.fn()} />
      </div>,
    )
    const inp = document.querySelector('[data-testid="inp"]') as HTMLInputElement
    act(() => { fireEvent.keyDown(inp, { key: '+' }) })
    expect(host().style.transform).not.toContain('scale(1.5)')
  })
})

describe('the magnify-surface rule is enforced by count, not by example', () => {
  it('gives every full-viewport magnify overlay the shared pinch hook', async () => {
    // This is the guard the original zoom suppression lacked. The rule "a surface
    // that must magnify owns its own zoom" read as satisfied because the ONE
    // documented example obeyed it, while a second overlay shipped with no gesture
    // at all and became unmagnifiable (#6107). A rule believed from its example
    // fails silently; one checked against its instances does not.
    //
    // The population is "components that render a full-viewport overlay whose
    // content is scaled to fit" — detected by the `fixed inset-0` + `z-[9999]`
    // shape both viewers use, since that is what makes a surface the only thing
    // on screen and therefore the only thing that can magnify.
    //
    // Sweep BOTH trees. A magnify overlay can live in either, and scoping the
    // population to one directory would make this check count instances of a set
    // it had itself narrowed — the same failure as believing the rule from its
    // documented example. A glob boundary is invisible to a reader; the named
    // exception below is not.
    const { glob } = await import('node:fs/promises')
    const { readFile } = await import('node:fs/promises')
    const { join } = await import('node:path')
    const root = join(__dirname, '..')
    /** Known-unfixed magnify surfaces, each with the issue that will close it.
     *  An entry here is a visible debt with an owner; deleting the entry is part
     *  of that issue's work. Do NOT add one to make a red sweep green — the
     *  point of the sweep is that a NEW overlay cannot ship without the hook. */
    const deferred = new Map<string, string>()
    const offenders: string[] = []
    const magnifySurfaces: string[] = []
    let seen = 0
    const files: string[] = []
    for await (const file of glob(['components/**/*.tsx', 'pages/**/*.tsx'], { cwd: root, withFileTypes: false })) {
      const normFile = String(file).replace(/\\/g, '/')
      files.push(normFile)
    }
    // OneDrive, antivirus and network-backed worktrees can make a sequential
    // read of the whole TSX population exceed the generic 15s unit-test budget.
    // Read in bounded batches: enough concurrency to avoid a filesystem-latency
    // sum, but not an unbounded Promise.all that can exhaust Windows handles.
    for (let start = 0; start < files.length; start += 32) {
      const batch = await Promise.all(
        files.slice(start, start + 32).map(async file => [file, await readFile(join(root, file), 'utf8')] as const),
      )
      for (const [file, src] of batch) {
        if (!/fixed inset-0[^"'`]*z-\[9999\]/.test(src)) continue
        seen++
        // A magnify overlay either uses the shared hook, or is one of the overlays
        // that never magnifies (a popover, a confirm sheet, a menu scrim). The
        // discriminator is whether it renders a single piece of CONTENT scaled to
        // fit — an `<img>`, or an SVG it fit-scales itself. It is content-based
        // rather than a sizing-utility scan because a `max-h-[…]` means only "this
        // has a maximum height": a menu carries one and is not a magnifier.
        const magnifies = /<img|querySelector\('svg'\)|preserveAspectRatio|object-contain/.test(src)
        if (!magnifies) continue
        magnifySurfaces.push(file)
        if (!src.includes('usePinchZoom') && !deferred.has(file)) offenders.push(file)
      }
    }
    // Prove the sweep reached the tree rather than passing on an empty match set.
    expect(seen, 'no full-viewport overlay matched — the shape detector broke').toBeGreaterThan(1)
    // Every deferred entry must still BE a magnify surface this sweep finds. When
    // its issue lands, the file gains the hook and the entry must be deleted; if
    // instead the file stops matching (renamed, reshaped, deleted), the entry is
    // excusing nothing and would sit here forever, so fail on a stale excuse too.
    for (const [file, issue] of deferred) {
      expect(
        magnifySurfaces,
        `${file} is listed as a deferred magnify surface (${issue}) but the sweep no longer finds it — delete the entry`,
      ).toContain(file)
    }
    expect(
      offenders,
      'this full-viewport overlay scales content to fit but has no own zoom, so on touch its content is unmagnifiable — page zoom is off shell-wide. Use hooks/usePinchZoom.ts',
    ).toEqual([])
  })
})
