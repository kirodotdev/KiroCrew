/**
 * Pins how the two-pane SettingsSubNav keeps its section list in view while
 * the settings scroll:
 *   - wide: the list's <nav> is sticky with a top inset and carries no height
 *     cap while the list fits
 *   - wide, list taller than the scroll area: the <nav> is capped to the
 *     measured room and the list inside it scrolls on its own; the cap lifts
 *     when the list fits again; the room also leaves the space below the list's
 *     row (at least the top inset), because a sticky list cannot leave its row
 *   - wide, capped: a decorative "More below" strip sits under the scrolling
 *     box while rows remain below, hides at the end, and is absent otherwise
 *   - wide again after a narrow stretch: no cap from the earlier measurement
 *     renders before the fresh one lands
 *   - narrow: the <nav> is exactly the plain full-width element (no sticky
 *     class, no inline style) and no ResizeObserver is created
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { SettingsSubNav } from '../components/SettingsSubNav'

let mockWidth: number | null = 1200
vi.mock('../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, mockWidth],
}))
let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const ITEMS = [
  { key: 'transcript', label: 'Transcript' },
  { key: 'composer', label: 'Composer' },
] as const

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  targets: Element[] = []
  constructor(private callback: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this)
  }
  observe(target: Element) {
    this.targets.push(target)
  }
  unobserve() {}
  disconnect() {
    this.targets = []
  }
  fire() {
    if (this.targets.length === 0) return
    this.callback([], this as unknown as ResizeObserver)
  }
}

function fireObservers() {
  act(() => {
    for (const observer of FakeResizeObserver.instances) observer.fire()
  })
}

function setSize(el: Element, prop: 'clientHeight' | 'offsetHeight', value: number) {
  Object.defineProperty(el, prop, { configurable: true, value })
}

const CUE = 'settings-subnav-more'

/** The box that scrolls once the list is capped: the listbox's parent. */
const scroller = (listbox: HTMLElement) => listbox.parentElement as HTMLElement

function renderWide() {
  const view = render(
    <MemoryRouter initialEntries={['/settings/chat']}>
      <div data-testid="scroll-area" style={{ overflowY: 'auto' }}>
        <SettingsSubNav items={ITEMS} listLabel="Chat settings" railWidth={220}>
          {active => <div data-testid="pane">{active ?? 'none'}</div>}
        </SettingsSubNav>
      </div>
    </MemoryRouter>,
  )
  const scrollArea = screen.getByTestId('scroll-area')
  const listbox = screen.getByRole('listbox', { name: 'Chat settings' })
  const nav = listbox.closest('nav') as HTMLElement
  return { ...view, scrollArea, listbox, nav }
}

describe('SettingsSubNav two-pane section list stays in view', () => {
  beforeEach(() => {
    mockWidth = 1200
    mobile = false
    FakeResizeObserver.instances = []
    vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('pins the wide list with sticky and a top inset, uncapped before any measurement', () => {
    const { nav, listbox } = renderWide()
    expect(nav.classList.contains('sticky')).toBe(true)
    expect(nav.classList.contains('shrink-0')).toBe(true)
    expect(nav.classList.contains('-ml-2.5')).toBe(true)
    expect(nav.style.width).toBe('220px')
    expect(nav.style.top).not.toBe('')
    expect(nav.style.maxHeight).toBe('')
    expect(scroller(listbox).classList.contains('overflow-y-auto')).toBe(false)
    expect(screen.queryByTestId(CUE)).toBeNull()
  })

  it('caps a list taller than the scroll area to the measured room and lifts the cap when it fits', () => {
    const { nav, listbox, scrollArea } = renderWide()
    setSize(scrollArea, 'clientHeight', 200)
    setSize(listbox, 'offsetHeight', 400)
    fireObservers()

    const cap = parseFloat(nav.style.maxHeight)
    expect(cap).toBeGreaterThan(0)
    expect(cap).toBeLessThan(200)
    expect(scroller(listbox).classList.contains('overflow-y-auto')).toBe(true)
    const targets = FakeResizeObserver.instances.flatMap(o => o.targets)
    expect(targets).toContain(scrollArea)
    expect(targets).toContain(listbox)

    setSize(listbox, 'offsetHeight', 100)
    fireObservers()
    expect(nav.style.maxHeight).toBe('')
    expect(scroller(listbox).classList.contains('overflow-y-auto')).toBe(false)
  })

  it('shows a "More below" cue on a capped list while rows remain below and hides it at the end', () => {
    const { nav, listbox, scrollArea } = renderWide()
    const scrolling = scroller(listbox)
    setSize(scrollArea, 'clientHeight', 200)
    setSize(listbox, 'offsetHeight', 400)
    Object.defineProperty(scrolling, 'clientHeight', { configurable: true, value: 150 })
    Object.defineProperty(scrolling, 'scrollHeight', { configurable: true, value: 400 })
    Object.defineProperty(scrolling, 'scrollTop', { configurable: true, writable: true, value: 0 })
    fireObservers()

    const cue = screen.getByTestId(CUE)
    expect(cue.className).not.toContain('invisible')
    expect(cue).toHaveAttribute('aria-hidden', 'true')
    expect(cue.querySelector('button, a, [tabindex]')).toBeNull()
    // The strip sits beside the scrolling box, not in it, so it stays on the
    // visible bottom edge while the rows scroll.
    expect(cue.parentElement).toBe(nav)
    expect(scrolling.contains(cue)).toBe(false)

    scrolling.scrollTop = 250
    act(() => { fireEvent.scroll(scrolling) })
    expect(screen.getByTestId(CUE).className).toContain('invisible')

    setSize(listbox, 'offsetHeight', 100)
    fireObservers()
    expect(screen.queryByTestId(CUE)).toBeNull()
  })

  it('leaves room for the space under the list row, and never less than the top inset', () => {
    const { nav, listbox, scrollArea } = renderWide()
    const row = nav.parentElement as HTMLElement
    Object.defineProperty(scrollArea, 'clientHeight', { configurable: true, value: 238 })
    Object.defineProperty(scrollArea, 'scrollHeight', { configurable: true, value: 1330 })
    setSize(listbox, 'offsetHeight', 400)
    const rowBottom = (bottom: number) =>
      vi.spyOn(row, 'getBoundingClientRect').mockReturnValue({ bottom } as DOMRect)

    // 32px of content below the row: the cap leaves that much at the end.
    rowBottom(1298)
    fireObservers()
    const inset = parseFloat(nav.style.top)
    expect(parseFloat(nav.style.maxHeight)).toBe(238 - inset - 32)
    expect(FakeResizeObserver.instances.flatMap(o => o.targets)).toContain(row)

    // Less space below than the inset: the inset applies at both ends.
    rowBottom(1328)
    fireObservers()
    expect(parseFloat(nav.style.maxHeight)).toBe(238 - 2 * inset)
  })

  it('renders no stale cap when re-entering two-pane after the list height changed', () => {
    const tree = () => (
      <MemoryRouter initialEntries={['/settings/chat']}>
        <div data-testid="scroll-area" style={{ overflowY: 'auto' }}>
          <SettingsSubNav items={ITEMS} listLabel="Chat settings" railWidth={220}>
            {active => <div data-testid="pane">{active ?? 'none'}</div>}
          </SettingsSubNav>
        </div>
      </MemoryRouter>
    )
    const { rerender } = render(tree())
    const scrollArea = screen.getByTestId('scroll-area')
    setSize(scrollArea, 'clientHeight', 200)
    setSize(screen.getByRole('listbox', { name: 'Chat settings' }), 'offsetHeight', 400)
    fireObservers()
    expect(parseFloat(screen.getByRole('listbox').closest('nav')!.style.maxHeight)).toBeGreaterThan(0)

    mockWidth = 400
    mobile = true
    rerender(tree())

    mockWidth = 1200
    mobile = false
    rerender(tree())
    const listbox = screen.getByRole('listbox', { name: 'Chat settings' })
    const nav = listbox.closest('nav') as HTMLElement
    expect(nav.style.maxHeight).toBe('')
    expect(scroller(listbox).classList.contains('overflow-y-auto')).toBe(false)

    setSize(scrollArea, 'clientHeight', 300)
    setSize(screen.getByRole('listbox', { name: 'Chat settings' }), 'offsetHeight', 400)
    fireObservers()
    expect(parseFloat(nav.style.maxHeight)).toBeGreaterThan(200)
  })

  it('leaves the narrow single-column list as the plain full-width element', () => {
    mockWidth = 400
    mobile = true
    render(
      <MemoryRouter initialEntries={['/settings/chat']}>
        <SettingsSubNav items={ITEMS} listLabel="Chat settings">
          {active => <div data-testid="pane">{active ?? 'none'}</div>}
        </SettingsSubNav>
      </MemoryRouter>,
    )
    const list = screen.getByRole('list', { name: 'Chat settings' })
    const nav = list.closest('nav') as HTMLElement
    expect(nav.className).toBe('w-full')
    expect(nav.hasAttribute('style')).toBe(false)
    expect(screen.queryByTestId(CUE)).toBeNull()
    expect(FakeResizeObserver.instances).toHaveLength(0)
  })
})
