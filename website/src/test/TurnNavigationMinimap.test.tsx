import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import TurnNavigationMinimap, {
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
    expect(shortenTurnPreview('## Plan\n\n1. `first` step', 40)).toBe('Plan first step')
  })

  it('shortens previews at a word boundary with three dots', () => {
    expect(shortenTurnPreview('short preview', 32)).toBe('short preview')
    expect(shortenTurnPreview('update the rfc to address the fable review findings', 35))
      .toBe('update the rfc to address the...')
    expect(shortenTurnPreview('abcdefghijklmnopqrstuvwxyz', 12)).toBe('abcdefghi...')
  })

  it('shows prompt and response preview, highlights visible turns, and navigates by pointer', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)

    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    await waitFor(() => {
      const markers = screen.getAllByTestId('turn-navigation-marker')
      expect(markers[0]).toHaveAttribute('data-in-view', 'true')
      expect(markers[1]).toHaveAttribute('data-in-view', 'true')
      expect(markers[2]).toHaveAttribute('data-in-view', 'false')
      expect(markers.map(marker => marker.style.width)).toEqual(['14px', '14px', '14px'])
    })

    await hover(button, 200)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second response')
    fireEvent.click(button)
    expect(onNavigate).toHaveBeenCalledWith(4)
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

  it('a pointer crossing the rail does not open the card; a 28px hit area leaves the gutter clickable-free', async () => {
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 28)
    expect(button.className).toContain('w-7')
    fireEvent.mouseMove(button, { clientY: 200 })
    fireEvent.mouseLeave(button)
    await new Promise(resolve => setTimeout(resolve, 250))
    expect(screen.queryByRole('tooltip')).toBeNull()
    await hover(button, 200)
    // Once open, moving updates instantly.
    fireEvent.mouseMove(button, { clientY: 300 })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Third prompt')
  })

  it('places 80 markers proportionally for a dense session', async () => {
    const scroller = buildScroller()
    const dense = Array.from({ length: 80 }, (_, i) => section(`t${i}`, i * 3 + 1, `Prompt ${i}`, ''))
    render(<TurnNavigationMinimap items={dense} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await screen.findByRole('button')
    // (The rail's `min(calc(100% - 8px), 711px)` height is CSS jsdom cannot parse; placement is what is asserted here.)
    const markers = screen.getAllByTestId('turn-navigation-marker')
    expect(markers).toHaveLength(80)
    expect(markers[1].style.top).toBe(`${(1 / 79) * 100}%`)
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

  it('shows an end-cap that loads older history when the window is incomplete, and scopes the count to loaded turns', async () => {
    const scroller = buildScroller()
    const onLoad = vi.fn()
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} earlier={{ loading: false, onLoad }} />)
    const cap = await screen.findByTestId('turn-navigation-earlier')
    fireEvent.click(cap)
    expect(onLoad).toHaveBeenCalledTimes(1)
    const rail = screen.getAllByRole('button').find(b => b !== cap)!
    rail.getBoundingClientRect = () => rect(100, 300, 8, 28)
    rail.focus()
    fireEvent.keyDown(rail, { key: 'Home' })
    expect(rail.getAttribute('aria-label')).toContain('of 3 loaded')
    view.rerender(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} earlier={{ loading: true, onLoad }} />)
    fireEvent.click(screen.getByTestId('turn-navigation-earlier'))
    expect(onLoad).toHaveBeenCalledTimes(1)
    view.rerender(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    expect(screen.queryByTestId('turn-navigation-earlier')).toBeNull()
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
