import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import TurnNavigationMinimap, {
  bucketTurns,
  markerPosition,
  pointerToTurnIndex,
  shortenTurnPreview,
} from '../pages/chat/TurnNavigationMinimap'
import type { ChatSection } from '../hooks/useChatNavigation'

function section(id: string, displayIdx: number, prompt: string, response: string): ChatSection {
  return { id, label: prompt, prompt, response, msgIdx: displayIdx, displayIdx }
}

const ITEMS: ChatSection[] = [
  section('one', 1, 'First prompt', 'First response'),
  section('two', 4, 'Second prompt', 'Second response'),
  section('three', 7, 'Third prompt', ''),
]

function rect(top: number, bottom: number, left = 100, width = 900): DOMRect {
  return { top, bottom, left, right: left + width, width, height: bottom - top, x: left, y: top, toJSON: () => ({}) }
}

/** Hover a rail position and wait out the first-open delay. */
async function hover(button: HTMLElement, clientY: number) {
  fireEvent.mouseMove(button, { clientY })
  await waitFor(() => expect(screen.getByRole('tooltip')).toBeInTheDocument())
}

function buildScroller() {
  const scroller = document.createElement('div')
  Object.defineProperty(scroller, 'clientWidth', { configurable: true, value: 1100 })
  scroller.getBoundingClientRect = () => rect(0, 600, 0, 1100)
  const rowRects = [rect(80, 180), rect(240, 340), rect(700, 800)]
  ITEMS.forEach((item, index) => {
    const row = document.createElement('div')
    row.dataset.displayIndex = String(item.displayIdx)
    row.getBoundingClientRect = () => rowRects[index]
    scroller.append(row)
  })
  document.body.append(scroller)
  return scroller
}

describe('TurnNavigationMinimap', () => {
  it('maps marker and pointer positions proportionally', () => {
    expect(markerPosition(0, 5)).toBe(0)
    expect(markerPosition(2, 5)).toBe(0.5)
    expect(markerPosition(4, 5)).toBe(1)
    expect(pointerToTurnIndex(100, 100, 400, 5)).toBe(0)
    expect(pointerToTurnIndex(300, 100, 400, 5)).toBe(2)
    expect(pointerToTurnIndex(500, 100, 400, 5)).toBe(4)
  })

  it('previews markdown as plain text: images by alt, links by label, no heading/fence/emphasis syntax', () => {
    expect(shortenTurnPreview('![screenshot](/p/a.png)', 40)).toBe('screenshot')
    expect(shortenTurnPreview('![](/p/a.png) look', 40)).toBe('look')
    expect(shortenTurnPreview('## Plan\n- **bold** step with [a link](https://x.y)\n```ts\ncode\n```', 80)).toBe('Plan bold step with a link code')
    expect(shortenTurnPreview('## Plan\n\n1. `first` step', 40)).toBe('Plan · first step')
  })

  it('shortens previews at a word boundary with three dots', () => {
    expect(shortenTurnPreview('short preview', 32)).toBe('short preview')
    expect(shortenTurnPreview('update the rfc to address the fable review findings', 35))
      .toBe('update the rfc to address the...')
    expect(shortenTurnPreview('abcdefghijklmnopqrstuvwxyz', 12)).toBe('abcdefghi...')
  })

  it('highlights on-screen turns and navigates by pointer', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)

    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    await waitFor(() => {
      const markers = screen.getAllByTestId('turn-navigation-marker')
      // Turns 1 and 2 are on screen and read as text; the rest is one gray.
      expect(markers.map(m => m.dataset.inView)).toEqual(['true', 'true', 'false'])
      expect(markers[0].style.background).toBe('var(--text)')
      expect(markers[1].style.background).toBe('var(--text)')
      expect(markers[2].style.background).toBe('var(--border)')
      expect(markers.map(marker => marker.style.width)).toEqual(['10px', '10px', '10px'])
    })

    await hover(button, 200)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second response')
    // Fisheye: the scrubbed marker grows, neighbours taper off — and the
    // in-view highlight yields so the wave is the only lit element.
    const markers = screen.getAllByTestId('turn-navigation-marker')
    expect(markers[1].style.width).toBe('26px')
    expect(markers[0].style.width).toBe('20px')
    expect(markers[2].style.width).toBe('20px')
    expect(markers[1].style.background).toBe('var(--text)')
    expect(markers[0].style.background).toBe('var(--muted)')
    fireEvent.click(button)
    expect(onNavigate).toHaveBeenCalledWith(4)
    view.unmount()
  })

  it('re-measures on row mutations but not on streamed text inside a row', async () => {
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await screen.findByRole('button')
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 20))
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    const row = scroller.querySelector<HTMLElement>('[data-display-index]')!
    row.append(document.createTextNode('streamed token'))
    row.append(document.createElement('span'))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(raf).not.toHaveBeenCalled()
    const newRow = document.createElement('div')
    newRow.dataset.displayIndex = '9'
    scroller.append(newRow)
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(raf).toHaveBeenCalled()
    raf.mockRestore()
  })

  it('a new items array with the same display indexes (a streamed token) does not re-subscribe or re-measure', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    const scrollerRef = { current: scroller }
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={scrollerRef} onNavigate={onNavigate} />)
    await screen.findByRole('button')
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 20))
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    const addListener = vi.spyOn(scroller, 'addEventListener')
    const streamed = ITEMS.map(item => ({ ...item, response: `${item.response} more` }))
    view.rerender(<TurnNavigationMinimap items={streamed} scrollerRef={scrollerRef} onNavigate={onNavigate} />)
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(raf).not.toHaveBeenCalled()
    expect(addListener).not.toHaveBeenCalled()
    raf.mockRestore(); addListener.mockRestore()
  })

  it('hover-intent gate: a graze never opens the card, a settled hover opens at the latest position, open tracking is instant', async () => {
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 28)
    expect(button.className).toContain('w-7')
    // A graze — enter then leave inside the intent window — never flashes the card.
    fireEvent.mouseMove(button, { clientY: 200 })
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.mouseLeave(button)
    await new Promise(resolve => setTimeout(resolve, 150))
    expect(screen.queryByRole('tooltip')).toBeNull()
    // A settled hover opens after the gate — at the LATEST hovered position
    // (movement retargets the pending open without restarting the countdown).
    fireEvent.mouseMove(button, { clientY: 100 })
    fireEvent.mouseMove(button, { clientY: 200 })
    await waitFor(() => expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt'))
    // Once open, moving tracks instantly.
    fireEvent.mouseMove(button, { clientY: 300 })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Third prompt')
    // Leaving the rail closes it after the short grace period.
    fireEvent.mouseLeave(button)
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
  })

  it('caps a dense session at 45 markers with even bucketing and a dedicated last tick', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    const dense = Array.from({ length: 80 }, (_, i) => section(`t${i}`, i * 3 + 1, `Prompt ${i}`, ''))
    render(<TurnNavigationMinimap items={dense} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    // (The rail's `min(calc(100% - 8px), ...)` height is CSS jsdom cannot parse; placement is what is asserted here.)
    const markers = screen.getAllByTestId('turn-navigation-marker')
    expect(markers).toHaveLength(45)
    expect(markers[1].style.top).toBe(`${(1 / 44) * 100}%`)
    // The newest turn always keeps its own dedicated final tick.
    expect(markers[44].dataset.targetDisplayIndex).toBe(String(79 * 3 + 1))
    button.focus()
    fireEvent.keyDown(button, { key: 'End' })
    fireEvent.keyDown(button, { key: 'Enter' })
    expect(onNavigate).toHaveBeenLastCalledWith(79 * 3 + 1)
    // A body tick jumps to its bucket's first turn: bucket 1 starts at
    // floor(1 * 79/44) = turn 1 (displayIdx 4).
    fireEvent.keyDown(button, { key: 'Home' })
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    fireEvent.keyDown(button, { key: 'Enter' })
    expect(onNavigate).toHaveBeenLastCalledWith(Math.floor(79 / 44) * 3 + 1)
  })

  it('one marker per turn under the cap; bucketTurns keeps coverage contiguous past it', () => {
    expect(bucketTurns(3)).toEqual([
      { first: 0, size: 1 },
      { first: 1, size: 1 },
      { first: 2, size: 1 },
    ])
    const buckets = bucketTurns(150)
    expect(buckets).toHaveLength(45)
    expect(buckets[44]).toEqual({ first: 149, size: 1 })
    // Body buckets tile the older history without gaps.
    for (let b = 1; b < 44; b++) {
      expect(buckets[b].first).toBe(buckets[b - 1].first + buckets[b - 1].size)
    }
    expect(buckets[43].first + buckets[43].size).toBe(149)
  })

  it('press-and-drag scrubs the chat live with instant scrolls and suppresses the trailing click', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    // Pointer-down bypasses the hover-intent delay: the card opens immediately.
    fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientY: 100 })
    expect(screen.getByRole('tooltip')).toHaveTextContent('First prompt')
    fireEvent.pointerMove(button, { pointerId: 1, clientY: 200 })
    expect(onNavigate).toHaveBeenLastCalledWith(4, { instant: true })
    fireEvent.pointerMove(button, { pointerId: 1, clientY: 300 })
    expect(onNavigate).toHaveBeenLastCalledWith(7, { instant: true })
    fireEvent.pointerUp(button, { pointerId: 1 })
    // The chat already followed the pointer — the derived trailing click must
    // not fire a second (smooth) jump at the same target.
    fireEvent.click(button, { detail: 1, clientY: 300 })
    expect(onNavigate).toHaveBeenCalledTimes(2)
    // The suppression is one-shot: the next plain click navigates again.
    fireEvent.click(button, { detail: 1, clientY: 300 })
    expect(onNavigate).toHaveBeenCalledTimes(3)
  })

  it('a plain click (full pointer sequence, no drag) still navigates and never takes pointer capture', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    const capture = vi.fn()
    ;(button as HTMLElement & { setPointerCapture: typeof capture }).setPointerCapture = capture
    fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientY: 300 })
    fireEvent.pointerUp(button, { pointerId: 1 })
    fireEvent.click(button, { detail: 1, clientY: 300 })
    expect(onNavigate).toHaveBeenCalledWith(7)
    expect(capture).not.toHaveBeenCalled()
  })

  it('the preview card shows faint neighbour titles and a position meta row with relative time', async () => {
    const scroller = buildScroller()
    const twoHoursAgo = new Date(Date.now() - 2 * 3600_000).toISOString()
    const stamped = ITEMS.map(item => ({ ...item, ts: twoHoursAgo }))
    render(<TurnNavigationMinimap items={stamped} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    await hover(button, 200)
    const neighbours = screen.getAllByTestId('turn-navigation-preview-neighbor')
    // Each neighbour line carries its #n index chip before the title.
    expect(neighbours.map(n => n.textContent)).toEqual(['#1First prompt', '#3Third prompt'])
    const meta = screen.getByTestId('turn-navigation-preview-meta')
    expect(meta.textContent).toContain('Turn 2 of 3')
    expect(meta.textContent).toContain('2h ago')
    // An edge turn has only one neighbour.
    fireEvent.mouseMove(button, { clientY: 100 })
    expect(screen.getAllByTestId('turn-navigation-preview-neighbor')).toHaveLength(1)
  })

  it("a bucketed marker's meta row names the span it covers", async () => {
    const scroller = buildScroller()
    const dense = Array.from({ length: 89 }, (_, i) => section(`t${i}`, i * 3 + 1, `Prompt ${i}`, ''))
    render(<TurnNavigationMinimap items={dense} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    button.focus()
    fireEvent.keyDown(button, { key: 'Home' })
    // Bucket 0 of 89 turns over 44 body ticks covers exactly 2 turns.
    expect(screen.getByTestId('turn-navigation-preview-meta').textContent).toContain('Turns 1\u20132 of 89')
  })

  it('a mounted reply row keeps its turn highlighted after the prompt row scrolls away', async () => {
    const scroller = buildScroller()
    // Replace the fixture rows: only a reply row of turn two (display index 5) is on screen.
    scroller.querySelectorAll<HTMLElement>('[data-display-index]').forEach(row => row.remove())
    const reply = document.createElement('div')
    reply.dataset.displayIndex = '5'
    reply.getBoundingClientRect = () => rect(100, 300)
    scroller.append(reply)
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await screen.findByRole('button')
    await waitFor(() => {
      const markers = screen.getAllByTestId('turn-navigation-marker')
      expect(markers.map(m => m.dataset.inView)).toEqual(['false', 'true', 'false'])
    })
  })

  it('focus alone does not open the card; the first Arrow reveals the last on-screen turn', async () => {
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 28)
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 20))
    button.focus()
    expect(screen.queryByRole('tooltip')).toBeNull()
    // Rows for turns 1 and 2 are on screen in the fixture; browsing starts at turn 2.
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Third prompt')
  })

  it('announces keyboard selection through a live region only while focused', async () => {
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    const live = screen.getByTestId('turn-navigation-minimap').querySelector('[aria-live="polite"]')!
    await hover(button, 200)
    expect(live).toHaveTextContent('')
    button.focus()
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    expect(live.textContent).toContain('Third prompt')
    fireEvent.blur(button)
    expect(live).toHaveTextContent('')
  })

  it('uses one keyboard target for Arrow, Home, End, Enter, Space, and Escape', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)

    button.focus()
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.keyDown(button, { key: 'Home' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('First prompt')
    fireEvent.keyDown(button, { key: 'End' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Third prompt')
    fireEvent.keyDown(button, { key: 'Enter' })
    expect(onNavigate).toHaveBeenLastCalledWith(7)
    fireEvent.keyDown(button, { key: 'Home' })
    fireEvent.keyDown(button, { key: ' ' })
    expect(onNavigate).toHaveBeenLastCalledWith(1)
    fireEvent.keyDown(button, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('below the md breakpoint the CSS hides the rail and no markers render', async () => {
    const original = Object.getOwnPropertyDescriptor(window, 'innerWidth')
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 700 })
    const scroller = buildScroller() // pane itself is wide enough — the viewport is what is narrow
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    if (original) Object.defineProperty(window, 'innerWidth', original)
  })

  it('windowed rail says "loaded" in the aria label and the meta row', async () => {
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} windowed />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    button.focus()
    fireEvent.keyDown(button, { key: 'Home' })
    expect(button.getAttribute('aria-label')).toContain('Turn 1 of 3 loaded')
    expect(screen.getByTestId('turn-navigation-preview-meta').textContent).toContain('Turn 1 of 3 loaded')
  })

  it('a bucketed marker on a windowed rail keeps the "loaded" disclosure', async () => {
    const scroller = buildScroller()
    const dense = Array.from({ length: 89 }, (_, i) => section(`t${i}`, i * 3 + 1, `Prompt ${i}`, ''))
    render(<TurnNavigationMinimap items={dense} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} windowed />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    button.focus()
    fireEvent.keyDown(button, { key: 'Home' })
    expect(screen.getByTestId('turn-navigation-preview-meta').textContent).toContain('Turns 1\u20132 of 89 loaded')
    expect(button.getAttribute('aria-label')).toContain('Turns 1\u20132 of 89 loaded')
  })

  it('the right-edge rail replaces the native scrollbar while shown and restores it on unmount', async () => {
    const scroller = buildScroller()
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} side="right" />)
    await screen.findByRole('button')
    await waitFor(() => expect(scroller.style.scrollbarWidth).toBe('none'))
    view.unmount()
    expect(scroller.style.scrollbarWidth).toBe('')
  })

  it('the left rail (default) never touches the native scrollbar', async () => {
    const scroller = buildScroller()
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await screen.findByRole('button')
    // Never assigned at all — jsdom reads an untouched property as undefined.
    expect(scroller.style.scrollbarWidth || '').toBe('')
    view.unmount()
    expect(scroller.style.scrollbarWidth || '').toBe('')
  })

  it('below the md breakpoint a right-edge rail is hidden and the native scrollbar stays', async () => {
    const original = Object.getOwnPropertyDescriptor(window, 'innerWidth')
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 700 })
    const scroller = buildScroller() // pane itself is wide enough — the viewport is what is narrow
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} side="right" />)
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    expect(scroller.style.scrollbarWidth || '').toBe('')
    if (original) Object.defineProperty(window, 'innerWidth', original)
  })

  it('does not render for one turn or without a safe left gutter', async () => {
    const scroller = buildScroller()
    const first = render(<TurnNavigationMinimap items={ITEMS.slice(0, 1)} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    first.unmount()

    Object.defineProperty(scroller, 'clientWidth', { configurable: true, value: 500 })
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    // Hidden rail: the initial measure frame runs once and does not reschedule itself.
    const frames = raf.mock.calls.length
    for (let i = 0; i < 3; i++) await new Promise(resolve => setTimeout(resolve, 20))
    expect(raf.mock.calls.length).toBe(frames)
    raf.mockRestore()
  })

  it('measures the constrained child of a full-width grouped-turn wrapper', async () => {
    const scroller = buildScroller()
    const first = scroller.querySelector<HTMLElement>('[data-display-index]')!
    first.getBoundingClientRect = () => rect(80, 180, 0, 1100)
    const constrained = document.createElement('div')
    constrained.dataset.contentColumn = ''
    constrained.getBoundingClientRect = () => rect(80, 180, 100, 900)
    first.append(constrained)

    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    expect(await screen.findByTestId('turn-navigation-minimap')).toBeInTheDocument()
  })

  it('keeps selection on the same turn across prepends and closes it when removed', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    button.focus()
    fireEvent.keyDown(button, { key: 'Home' })
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')

    const prepended = [section('zero', 0, 'Earlier prompt', ''), ...ITEMS]
    view.rerender(<TurnNavigationMinimap items={prepended} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')

    view.rerender(<TurnNavigationMinimap items={prepended.filter(item => item.id !== 'two')} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
    fireEvent.click(button)
    expect(onNavigate).toHaveBeenCalled()
  })

  it('does not mount the hover rail for coarse pointers', async () => {
    const original = window.matchMedia
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await new Promise(resolve => requestAnimationFrame(resolve))
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    window.matchMedia = original
  })
})
