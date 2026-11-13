/**
 * A chat code block keeps long lines whole and scrolls sideways, and the
 * dashboard paints no scrollbar at rest, so a line running past the right edge
 * reads as ending there. The cue is the shared `.edge-fade-x` mask on a box
 * inside the focusable `.pierre-surface` region (a mask on the region itself
 * would hide its focus ring), driven by `data-fade-right` from a
 * `useScrollEdges` measurement of the element that actually scrolls.
 *
 * What each test pins, and what reverting it breaks:
 *
 *   - a clipped stand-in `<pre>` fades its right edge, and the fade goes away at
 *     the end of the scroll (revert symptom: no cue at all, the original gap),
 *   - a block that fits shows no fade (a permanent fade lies that code is hidden),
 *   - there is never a left fade, even when scrolled (it would fade the line
 *     numbers),
 *   - Pierre's scroller is found inside its shadow root, including when Pierre
 *     fills the root after its host mounted, and when the highlighted surface
 *     replaces the stand-in (the scroller is a different element by then).
 *
 * jsdom does no layout, so scroll geometry is stubbed per element.
 */
import { createElement } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { CodeBlock } from '../components/CodeBlock'
import { __resetStagingForTests } from '../components/pierreStaging'

/** How the mocked Pierre fills its shadow root: at once, or only on demand. */
const pierreMode: { lazy: boolean; fill: (() => void) | null } = { lazy: false, fill: null }

vi.mock('../pierre', () => ({
  // A stand-in for Pierre's `diffs-container` host: an open shadow root whose
  // `[data-code]` box is the element that scrolls, as in the real library.
  PierreCode: ({ file }: { file: { contents: string } }) => createElement('diffs-container', {
    ref: (host: HTMLElement | null) => {
      if (!host || host.shadowRoot) return
      const root = host.attachShadow({ mode: 'open' })
      const fill = () => {
        const code = document.createElement('pre')
        code.setAttribute('data-code', '')
        code.textContent = file.contents
        root.appendChild(code)
      }
      if (pierreMode.lazy) pierreMode.fill = fill
      else fill()
    },
  }),
}))

interface Geometry { client: number; scroll: number; left: number }
const geometry = new WeakMap<Element, Geometry>()
const DEFAULT: Geometry = { client: 320, scroll: 320, left: 0 }

function stubGeometry() {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) { return (geometry.get(this) ?? DEFAULT).client })
  vi.spyOn(proto, 'scrollWidth', 'get').mockImplementation(function (this: HTMLElement) { return (geometry.get(this) ?? DEFAULT).scroll })
  vi.spyOn(proto, 'scrollLeft', 'get').mockImplementation(function (this: HTMLElement) { return (geometry.get(this) ?? DEFAULT).left })
}

/** Gives `el` its geometry and tells the hook it moved, as a scroll would. */
function setGeometry(el: Element, g: Geometry) {
  geometry.set(el, g)
  fireEvent.scroll(el)
}

const region = (c: HTMLElement) => c.querySelector('.pierre-surface[role="region"]') as HTMLElement
const surface = (c: HTMLElement) => region(c).querySelector(':scope > .edge-fade-x') as HTMLElement
const fadesRight = (c: HTMLElement) => {
  // The focusable region never wears the mask: it would hide the focus ring.
  expect(region(c).hasAttribute('data-fade-right')).toBe(false)
  return surface(c).hasAttribute('data-fade-right')
}
const fadesLeft = (c: HTMLElement) => surface(c).hasAttribute('data-fade-left')
const shadowScroller = (c: HTMLElement) =>
  c.querySelector('diffs-container')?.shadowRoot?.querySelector('[data-code]') ?? null

const LONG = 'const answer = computeTheAnswerToLifeTheUniverseAndEverything(withAVeryLongArgumentName, andAnotherOne)'

describe('CodeBlock: right-edge fade while code continues past the edge', () => {
  beforeEach(() => {
    __resetStagingForTests()
    pierreMode.lazy = false
    pierreMode.fill = null
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
    stubGeometry()
  })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('fades the right edge of a clipped stand-in block until the scroll reaches the end', () => {
    const { container } = render(<CodeBlock code={LONG} lang="ts" complete={false} />)
    expect(surface(container)).not.toBeNull()
    expect(region(container).classList.contains('edge-fade-x')).toBe(false)
    const pre = container.querySelector('pre.pierre-plain')!
    setGeometry(pre, { client: 320, scroll: 800, left: 0 })
    expect(fadesRight(container)).toBe(true)

    setGeometry(pre, { client: 320, scroll: 800, left: 200 })
    expect(fadesRight(container)).toBe(true)
    expect(fadesLeft(container)).toBe(false)

    setGeometry(pre, { client: 320, scroll: 800, left: 480 })
    expect(fadesRight(container)).toBe(false)
    expect(fadesLeft(container)).toBe(false)
  })

  it('shows no fade on a block that fits', () => {
    const { container } = render(<CodeBlock code="x = 1" lang="py" complete={false} />)
    setGeometry(container.querySelector('pre.pierre-plain')!, { client: 320, scroll: 320, left: 0 })
    expect(fadesRight(container)).toBe(false)
    expect(fadesLeft(container)).toBe(false)
  })

  it('measures the scroller inside Pierre\'s shadow root', async () => {
    const { container } = render(<CodeBlock code={LONG} lang="ts" complete />)
    const scroller = shadowScroller(container)
    expect(scroller).not.toBeNull()
    // The hook re-attaches from a MutationObserver callback (a microtask), so
    // the first read on the shadow scroller lands after this render returns.
    geometry.set(scroller!, { client: 320, scroll: 900, left: 0 })
    await waitFor(() => expect(fadesRight(container)).toBe(true))
    setGeometry(scroller!, { client: 320, scroll: 900, left: 580 })
    expect(fadesRight(container)).toBe(false)
    expect(fadesLeft(container)).toBe(false)
  })

  it('finds the scroller when Pierre fills its shadow root after mount', async () => {
    pierreMode.lazy = true
    const { container } = render(<CodeBlock code={LONG} lang="ts" complete />)
    expect(fadesRight(container)).toBe(false)
    expect(pierreMode.fill).not.toBeNull()
    // The geometry is set before the observer's callback runs (it is a
    // microtask), so the hook's first read on attach must already see the clip.
    pierreMode.fill!()
    const scroller = shadowScroller(container)!
    geometry.set(scroller, { client: 320, scroll: 900, left: 0 })
    await waitFor(() => expect(fadesRight(container)).toBe(true))
  })

  it('moves to Pierre\'s scroller when the highlight replaces the stand-in', async () => {
    const { container, rerender } = render(<CodeBlock code={LONG} lang="ts" complete={false} />)
    setGeometry(container.querySelector('pre.pierre-plain')!, { client: 320, scroll: 320, left: 0 })
    expect(fadesRight(container)).toBe(false)

    pierreMode.lazy = true
    rerender(<CodeBlock code={LONG} lang="ts" complete />)
    pierreMode.fill!()
    const scroller = shadowScroller(container)!
    geometry.set(scroller, { client: 320, scroll: 900, left: 0 })
    await waitFor(() => expect(fadesRight(container)).toBe(true))
    setGeometry(scroller, { client: 320, scroll: 900, left: 580 })
    expect(fadesRight(container)).toBe(false)
  })
})
