import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import {
  AllTabsMenu,
  ALL_TABS_SECONDARY_CLASS,
  ALL_TABS_MARK_SLOT_CLASS,
  ALL_TABS_CURRENT_TITLE_CLASS,
  type AllTabsGroup,
  type AllTabsMenuProps,
} from '../components/AllTabsMenu'

const LABEL = 'List all tabs'

const GROUPS: AllTabsGroup[] = [
  { id: 'leading', rows: [
    { id: 'a', title: 'Alpha', glyph: <span data-testid="glyph-a">g</span> },
    { id: 'b', title: 'Bravo', secondary: '/repo/src/bravo.ts' },
  ] },
  { id: 'empty', rows: [] },
  { id: 'pinned', rows: [{ id: 'c', title: 'Charlie' }] },
  { id: 'dynamic', rows: [{ id: 'd', title: <em data-testid="live-title">Delta live</em>, badge: <span data-testid="badge-d">3</span> }] },
]

function renderMenu(overrides: Partial<AllTabsMenuProps> = {}) {
  const onSelect = vi.fn()
  const props: AllTabsMenuProps = { groups: GROUPS, currentId: 'b', onSelect, label: LABEL, ...overrides }
  const utils = render(<AllTabsMenu {...props} />)
  const rerender = (next: Partial<AllTabsMenuProps>) =>
    utils.rerender(<AllTabsMenu {...props} {...next} />)
  return { ...utils, onSelect, rerender }
}

const trigger = () => screen.getByTestId('all-tabs-trigger')

// Radix opens a dropdown on a primary-button pointerdown, as in sidePanelAddMenu.test.tsx.
const openMenu = () => act(() => {
  fireEvent.pointerDown(trigger(), { button: 0, ctrlKey: false, pointerType: 'mouse' })
})

const rows = () => screen.getAllByRole('menuitemradio')
const rowById = (id: string) => {
  const el = document.querySelector<HTMLElement>(`[role="menuitemradio"][data-row-id="${id}"]`)
  if (!el) throw new Error(`row ${id} not rendered`)
  return el
}
const markSlot = (row: HTMLElement) => {
  const slot = Array.from(row.querySelectorAll('span')).find(s => s.className === ALL_TABS_MARK_SLOT_CLASS)
  if (!slot) throw new Error('mark slot missing')
  return slot
}
const titleSpan = (row: HTMLElement) => {
  const span = row.querySelector('span.truncate')
  if (!span) throw new Error('title span missing')
  return span
}

describe('AllTabsMenu trigger', () => {
  it('uses the label as aria-label and title and renders only the chevron', () => {
    renderMenu()
    const btn = trigger()
    expect(btn.tagName).toBe('BUTTON')
    expect(btn.getAttribute('type')).toBe('button')
    expect(btn.getAttribute('aria-label')).toBe(LABEL)
    expect(btn.getAttribute('title')).toBe(LABEL)
    expect(screen.getByRole('button', { name: LABEL })).toBe(btn)
    const svg = btn.querySelector('svg')
    expect(svg?.getAttribute('class')).toContain('lucide-chevron-down')
    expect(svg?.getAttribute('aria-hidden')).toBe('true')
    expect(btn.textContent).toBe('')
  })

  it('shows no rows while closed', () => {
    renderMenu()
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.queryAllByRole('menuitemradio')).toHaveLength(0)
  })
})

describe('AllTabsMenu rows', () => {
  it('lists the surviving groups in order, with separators only between them', () => {
    renderMenu()
    openMenu()
    expect(rows().map(r => r.getAttribute('data-row-id'))).toEqual(['a', 'b', 'c', 'd'])
    const menu = screen.getByRole('menu')
    expect(menu.querySelectorAll('[role="separator"]')).toHaveLength(2)
    const roles = Array.from(menu.querySelectorAll('[role="menuitemradio"], [role="separator"]'))
      .map(el => el.getAttribute('data-row-id') ?? 'sep')
    expect(roles).toEqual(['a', 'b', 'sep', 'c', 'sep', 'd'])
  })

  it('checks exactly the current row', () => {
    renderMenu({ currentId: 'c' })
    openMenu()
    const checked = rows().filter(r => r.getAttribute('aria-checked') === 'true')
    expect(checked).toHaveLength(1)
    expect(checked[0].getAttribute('data-row-id')).toBe('c')
  })

  it('checks no row when currentId is null', () => {
    renderMenu({ currentId: null })
    openMenu()
    expect(rows().filter(r => r.getAttribute('aria-checked') === 'true')).toHaveLength(0)
    expect(rows().every(r => markSlot(r).childElementCount === 0)).toBe(true)
  })

  it('draws the mark and the accent title on the current row only, and a slot on every row', () => {
    renderMenu({ currentId: 'b' })
    openMenu()
    for (const row of rows()) {
      const isCurrent = row.getAttribute('data-row-id') === 'b'
      const slot = markSlot(row)
      expect(slot.className).toBe(ALL_TABS_MARK_SLOT_CLASS)
      const mark = slot.querySelector('svg')
      if (isCurrent) {
        expect(mark?.getAttribute('class')).toContain('lucide-check')
        expect(mark?.getAttribute('class')).toContain('text-accent')
      } else {
        expect(mark).toBeNull()
      }
      expect(titleSpan(row).classList.contains(ALL_TABS_CURRENT_TITLE_CLASS)).toBe(isCurrent)
    }
  })

  it('applies the exact layout classes', () => {
    expect(ALL_TABS_SECONDARY_CLASS).toBe('truncate text-[11px] leading-4 text-muted')
    expect(ALL_TABS_MARK_SLOT_CLASS).toBe('flex w-[13px] shrink-0 items-center justify-center')
    expect(ALL_TABS_CURRENT_TITLE_CLASS).toBe('text-accent')
    renderMenu()
    openMenu()
    const menuClasses = screen.getByRole('menu').className.split(/\s+/)
    for (const token of ['min-w-[240px]', 'max-w-[min(360px,calc(100vw-16px))]']) expect(menuClasses).toContain(token)
    const secondary = Array.from(rowById('b').querySelectorAll('span')).find(s => s.className === ALL_TABS_SECONDARY_CLASS)
    expect(secondary?.textContent).toBe('/repo/src/bravo.ts')
  })

  it('renders a secondary line only when given', () => {
    renderMenu()
    openMenu()
    const hasSecondary = (id: string) =>
      Array.from(rowById(id).querySelectorAll('span')).some(s => s.className === ALL_TABS_SECONDARY_CLASS)
    expect(hasSecondary('b')).toBe(true)
    expect(hasSecondary('a')).toBe(false)
    expect(hasSecondary('c')).toBe(false)
  })

  it('renders a node title, the glyph and the badge before the mark slot', () => {
    renderMenu()
    openMenu()
    expect(screen.getByTestId('live-title').textContent).toBe('Delta live')
    expect(rowById('d').contains(screen.getByTestId('live-title'))).toBe(true)
    expect(screen.getByTestId('glyph-a').parentElement?.className).toBe('text-muted shrink-0')
    const badge = screen.getByTestId('badge-d')
    expect(badge.nextElementSibling).toBe(markSlot(rowById('d')))
  })

  it('keeps row order and DOM nodes when only currentId changes', () => {
    const { rerender } = renderMenu({ currentId: 'a' })
    openMenu()
    const before = rows()
    rerender({ currentId: 'c' })
    const after = rows()
    expect(after.map(r => r.getAttribute('data-row-id'))).toEqual(['a', 'b', 'c', 'd'])
    after.forEach((el, i) => expect(el).toBe(before[i]))
    expect(rowById('c').getAttribute('aria-checked')).toBe('true')
    expect(rowById('a').getAttribute('aria-checked')).toBe('false')
  })
})

describe('AllTabsMenu selection', () => {
  it('calls onSelect once with a non-current row id and closes', async () => {
    const { onSelect } = renderMenu({ currentId: 'b' })
    openMenu()
    act(() => { fireEvent.click(rowById('c')) })
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('c')
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('calls onSelect for the current row too, which onValueChange alone would never report', async () => {
    const { onSelect } = renderMenu({ currentId: 'b' })
    openMenu()
    act(() => { fireEvent.click(rowById('b')) })
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('b')
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('selects with the arrow keys and Enter from wherever Radix puts focus', async () => {
    const { onSelect } = renderMenu({ currentId: null })
    openMenu()
    await waitFor(() => expect(screen.getByRole('menu')).toBeTruthy())
    const menu = screen.getByRole('menu')
    // Radix may open with focus on the menu or on a row. Arrow down reaches a row either way.
    act(() => { fireEvent.keyDown(document.activeElement ?? menu, { key: 'ArrowDown' }) })
    const focused = document.activeElement as HTMLElement
    expect(focused.getAttribute('role')).toBe('menuitemradio')
    const id = focused.getAttribute('data-row-id')
    act(() => { fireEvent.keyDown(focused, { key: 'Enter' }) })
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith(id)
  })

  it('puts a native tooltip on a row only when one is given', () => {
    renderMenu({ groups: [{ id: 'g', rows: [
      { id: 'x', title: 'x.ts', tooltip: '/very/long/path/to/x.ts' },
      { id: 'y', title: 'y.ts' },
    ] }] })
    openMenu()
    expect(rowById('x').getAttribute('title')).toBe('/very/long/path/to/x.ts')
    expect(rowById('y').hasAttribute('title')).toBe(false)
  })

  it('closes on Escape and returns focus to the trigger', async () => {
    renderMenu()
    openMenu()
    act(() => { fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' }) })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(document.activeElement).toBe(trigger())
  })
})

describe('AllTabsMenu focus rule', () => {
  const without = (id: string): AllTabsGroup[] =>
    GROUPS.map(g => ({ ...g, rows: g.rows.filter(r => r.id !== id) }))

  it('closes and focuses the trigger when the focused row disappears', async () => {
    const { rerender } = renderMenu({ currentId: 'a' })
    openMenu()
    act(() => { rowById('b').focus() })
    expect(document.activeElement).toBe(rowById('b'))
    act(() => { rerender({ groups: without('b') }) })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(document.activeElement).toBe(trigger())
  })

  it('stays open when a row that is not focused disappears', () => {
    const { rerender } = renderMenu({ currentId: 'a' })
    openMenu()
    act(() => { rowById('b').focus() })
    act(() => { rerender({ groups: without('c') }) })
    expect(screen.getByRole('menu')).toBeTruthy()
    expect(rows().map(r => r.getAttribute('data-row-id'))).toEqual(['a', 'b', 'd'])
    expect(document.activeElement).toBe(rowById('b'))
  })

  it('forgets the focused row when the pointer leaves it, so a later removal keeps the menu open', () => {
    const { rerender } = renderMenu({ currentId: 'a' })
    openMenu()
    act(() => { rowById('b').focus() })
    act(() => { fireEvent.pointerMove(rowById('b'), { pointerType: 'mouse' }) })
    act(() => { fireEvent.pointerLeave(rowById('b'), { pointerType: 'mouse' }) })
    expect(document.activeElement).toBe(screen.getByRole('menu'))
    act(() => { rerender({ groups: without('b') }) })
    expect(screen.getByRole('menu')).toBeTruthy()
  })
})

describe('AllTabsMenu scrolls the current row into view on open', () => {
  type Rect = { top: number; bottom: number }
  const restore: Array<() => void> = []
  afterEach(() => { while (restore.length) restore.pop()?.() })

  /**
   * The menu mounts in a portal on open, so geometry cannot be stubbed on the
   * element in advance. The stub dispatches on the element instead: the menu
   * and the named row get rects, and scrollTop becomes a stored own value per
   * element with every write recorded.
   */
  function stubGeometry({ content, row, rowId, initialScrollTop = 0 }: {
    content: Rect; row: Rect; rowId: string; initialScrollTop?: number
  }) {
    const rect = (r: Rect) => ({ x: 0, y: r.top, left: 0, right: 300, top: r.top, bottom: r.bottom, width: 300, height: r.bottom - r.top, toJSON() { return {} } }) as DOMRect
    const rectSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.getAttribute('role') === 'menu') return rect(content)
      if (this.getAttribute('data-row-id') === rowId) return rect(row)
      return rect({ top: 0, bottom: 0 })
    })
    restore.push(() => rectSpy.mockRestore())

    let owner: object | null = HTMLDivElement.prototype
    while (owner && !Object.getOwnPropertyDescriptor(owner, 'scrollTop')) owner = Object.getPrototypeOf(owner)
    if (!owner) throw new Error('scrollTop descriptor not found')
    const proto = owner
    const original = Object.getOwnPropertyDescriptor(proto, 'scrollTop')
    const values = new WeakMap<Element, number>()
    const writes: Array<{ el: Element; value: number }> = []
    Object.defineProperty(proto, 'scrollTop', {
      configurable: true,
      get(this: Element) {
        return values.get(this) ?? (this.getAttribute('role') === 'menu' ? initialScrollTop : 0)
      },
      set(this: Element, value: number) {
        writes.push({ el: this, value })
        values.set(this, value)
      },
    })
    restore.push(() => { if (original) Object.defineProperty(proto, 'scrollTop', original) })
    return { writes }
  }

  function spyScrollIntoView() {
    const had = 'scrollIntoView' in Element.prototype
    const original = Element.prototype.scrollIntoView
    const spy = vi.fn()
    Element.prototype.scrollIntoView = spy
    restore.push(() => {
      if (had) Element.prototype.scrollIntoView = original
      else delete (Element.prototype as Partial<Element>).scrollIntoView
    })
    return spy
  }

  function renderInsideScroller(currentId: string) {
    const { container } = render(
      <div data-testid="outer" style={{ overflow: 'auto', height: 100 }}>
        <AllTabsMenu groups={GROUPS} currentId={currentId} onSelect={() => {}} label={LABEL} />
      </div>,
    )
    return container
  }

  it('scrolls only the portalled menu down to a row below its visible area', () => {
    const { writes } = stubGeometry({ content: { top: 100, bottom: 300 }, row: { top: 500, bottom: 532 }, rowId: 'd' })
    const scrollIntoView = spyScrollIntoView()
    const container = renderInsideScroller('d')
    const outer = screen.getByTestId('outer')
    const pageBefore = { y: window.scrollY, doc: document.documentElement.scrollTop }
    openMenu()
    const menu = screen.getByRole('menu')
    expect(container.contains(menu)).toBe(false)
    expect(menu.scrollTop).toBe(232)
    expect(writes.every(w => w.el === menu)).toBe(true)
    expect(writes).toHaveLength(1)
    expect(outer.scrollTop).toBe(0)
    expect(window.scrollY).toBe(pageBefore.y)
    expect(document.documentElement.scrollTop).toBe(pageBefore.doc)
    expect(scrollIntoView).not.toHaveBeenCalled()
  })

  it('scrolls the menu up to a row above its visible area', () => {
    stubGeometry({ content: { top: 100, bottom: 300 }, row: { top: 20, bottom: 52 }, rowId: 'a', initialScrollTop: 300 })
    renderInsideScroller('a')
    openMenu()
    expect(screen.getByRole('menu').scrollTop).toBe(220)
  })

  it('keeps the current row in view across height changes, then stops at the first user input', () => {
    // Live observers only: a disconnected one must stop receiving callbacks, as in a browser.
    const live = new Set<() => void>()
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      private cb: () => void
      constructor(cb: () => void) { this.cb = cb }
      // Radix runs its own observers too, so keep only the one watching the menu.
      observe(el: Element) { if (el.getAttribute('role') === 'menu') live.add(this.cb) }
      unobserve() {}
      disconnect() { live.delete(this.cb) }
    } as unknown as typeof ResizeObserver
    restore.push(() => { globalThis.ResizeObserver = original })
    // Radix applies max-height after the first layout, so the row starts inside the content.
    const row = { top: 150, bottom: 182 }
    const { writes } = stubGeometry({ content: { top: 100, bottom: 300 }, row, rowId: 'd' })
    renderInsideScroller('d')
    openMenu()
    const menu = screen.getByRole('menu')
    expect(writes).toHaveLength(0)
    expect(live.size).toBe(1)

    // The height settles in more than one pass, and each pass reveals the row again.
    row.top = 500
    row.bottom = 532
    act(() => { Array.from(live).forEach(cb => cb()) })
    expect(menu.scrollTop).toBe(232)
    row.top = 540
    row.bottom = 572
    act(() => { Array.from(live).forEach(cb => cb()) })
    expect(menu.scrollTop).toBe(504)
    expect(live.size).toBe(1)

    // The person scrolls: observing stops, and a later resize leaves their scroll alone.
    act(() => { fireEvent.wheel(menu) })
    expect(live.size).toBe(0)
    menu.scrollTop = 40
    const writesBefore = writes.length
    row.top = 700
    row.bottom = 732
    act(() => { Array.from(live).forEach(cb => cb()) })
    expect(menu.scrollTop).toBe(40)
    expect(writes.length).toBe(writesBefore)
  })

  it('converts scaled rects to scroll distance while the open animation runs', () => {
    // Layout height 200 drawn at 95%: rects are 5% short of the scrollTop distance they stand for.
    const { writes } = stubGeometry({ content: { top: 100, bottom: 290 }, row: { top: 380, bottom: 408 }, rowId: 'd' })
    const heightSpy = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.getAttribute('role') === 'menu' ? 200 : 0
    })
    restore.push(() => heightSpy.mockRestore())
    renderInsideScroller('d')
    openMenu()
    // 118 rect px below the visible bottom is 118 / 0.95 scroll px.
    expect(writes).toHaveLength(1)
    expect(writes[0].value).toBeCloseTo((408 - 290) / 0.95, 5)
  })

  it('does not write scrollTop when the current row is already visible', () => {
    const { writes } = stubGeometry({ content: { top: 100, bottom: 300 }, row: { top: 150, bottom: 182 }, rowId: 'b' })
    const scrollIntoView = spyScrollIntoView()
    renderInsideScroller('b')
    openMenu()
    expect(screen.getByRole('menu')).toBeTruthy()
    expect(writes).toHaveLength(0)
    expect(scrollIntoView).not.toHaveBeenCalled()
  })
})
