/**
 * Happy-dom does no layout, so these integration tests stub geometry per
 * element. The fit and oversized-chip arithmetic is covered in
 * sidePanelReveal.test.ts.
 */
import type { ComponentProps } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, cleanup, fireEvent, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
// forwardRef: the file tab body hands this a ref, and a plain function component would warn.
vi.mock('../components/MarkdownPanel', async () => {
  const { forwardRef } = await import('react')
  return { default: forwardRef(() => null) }
})
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
const viewport = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import type { SidePanelLeadingTab } from '../pages/chat/SidePanel'
import { usePanelTabs } from '../hooks/usePanelTabs'
import { setSidePanelDock } from '../hooks/useSidePanelDock'

type TabsCtl = ReturnType<typeof usePanelTabs>

// usePanelTabs keeps its buckets in module state, so each spec gets its own slot.
let slotSeq = 0
let SLOT = 'slot-0'

interface HarnessProps {
  onReady: (ctl: TabsCtl) => void
  leadingTabs?: SidePanelLeadingTab[]
  leadingIds?: readonly string[]
  hiddenViews?: ComponentProps<typeof SidePanel>['hiddenViews']
  projectDir?: string
}

function Harness({ onReady, leadingTabs, leadingIds, hiddenViews, projectDir }: HarnessProps) {
  const tabsCtl = usePanelTabs(SLOT, undefined, leadingIds ? { leadingIds } : undefined)
  onReady(tabsCtl)
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={SLOT}
      pins={[]}
      onFileSave={async () => {}}
      onClose={() => {}}
      leadingTabs={leadingTabs}
      hiddenViews={hiddenViews}
      projectDir={projectDir}
    />
  )
}

function renderPanel(props: Omit<HarnessProps, 'onReady'> = {}) {
  SLOT = `slot-${++slotSeq}`
  let ctl: TabsCtl | undefined
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness onReady={c => { ctl = c }} {...props} />
      </Provider>
    </QueryClientProvider>,
  )
  return () => ctl as TabsCtl
}

const trigger = () => screen.getByRole('button', { name: 'List all tabs' })
const dynamicScroller = () => document.querySelector('[role="tablist"]') as HTMLElement
const fixedScroller = () => document.querySelector('[data-testid="side-panel-fixed-tabs"]') as HTMLElement
const chip = (id: string) => document.querySelector(`[data-tab-chip-id="${id}"]`) as HTMLElement

const openAllTabs = () => act(() => {
  fireEvent.pointerDown(trigger(), { button: 0, ctrlKey: false, pointerType: 'mouse' })
})

/**
 * A 300px scrollport that can scroll 400px further. Any chip reports a 100px
 * box placed `chipLeft` px from the scroller's left edge, minus the scroller's
 * current scrollLeft, so a reveal that writes scrollLeft moves it.
 */
function stubGeometry(chipLeft: number, widths: { fixed?: number; dynamic?: number } = {}) {
  const proto = window.HTMLElement.prototype
  const scrolled = new WeakMap<Element, number>()
  const isScroller = (el: Element) => el.getAttribute('role') === 'tablist' || el.getAttribute('data-testid') === 'side-panel-fixed-tabs'
  const widthOf = (el: Element) => (el.getAttribute('data-testid') === 'side-panel-fixed-tabs' ? widths.fixed : widths.dynamic) ?? 300
  vi.spyOn(proto, 'clientWidth', 'get').mockImplementation(function (this: Element) { return isScroller(this) ? widthOf(this) : 0 })
  vi.spyOn(proto, 'scrollWidth', 'get').mockImplementation(function (this: Element) { return isScroller(this) ? 700 : 0 })
  const writes: number[] = []
  // On Element.prototype, where the DOM defines it, so restoreAllMocks puts it back.
  vi.spyOn(Element.prototype, 'scrollLeft', 'get').mockImplementation(function (this: Element) { return scrolled.get(this) ?? 0 })
  vi.spyOn(Element.prototype, 'scrollLeft', 'set').mockImplementation(function (this: Element, v: number) { writes.push(v); scrolled.set(this, v) })
  vi.spyOn(proto, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (isScroller(this)) { const w = widthOf(this); return { left: 0, right: w, top: 0, bottom: 32, width: w, height: 32, x: 0, y: 0, toJSON() {} } as DOMRect }
    if (this.hasAttribute('data-tab-chip-id')) {
      const scroller = this.closest('[role="tablist"], [data-testid="side-panel-fixed-tabs"]')
      const left = chipLeft - (scroller ? (scrolled.get(scroller) ?? 0) : 0)
      return { left, right: left + 100, top: 0, bottom: 32, width: 100, height: 32, x: left, y: 0, toJSON() {} } as DOMRect
    }
    return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON() {} } as DOMRect
  })
  return { writes, scrolled }
}

describe('side panel "List all tabs" menu', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  const placementModes: Array<[string, () => void, () => void]> = [
    ['right dock', () => {}, () => {}],
    ['bottom dock', () => { act(() => { setSidePanelDock('bottom') }) }, () => { act(() => { setSidePanelDock('right') }) }],
    ['narrow viewport', () => { viewport.mobile = true }, () => { viewport.mobile = false }],
  ]
  it.each(placementModes)('sits at the trailing edge, after the gap and outside both scrollers, in the %s', (_name, setup, teardown) => {
    setup()
    try {
      const ctl = renderPanel()
      // Overflowing dynamic group: enough tabs to need the scroller.
      for (let i = 0; i < 12; i++) act(() => { ctl().openFile(`/repo/file-${i}.md`, `c${i}`, SLOT) })
      const plus = screen.getByRole('button', { name: 'Open side panel tab' })
      // + keeps the leading position, then the flexible gap, then the trigger.
      const gap = plus.nextElementSibling as HTMLElement
      expect(gap.getAttribute('aria-hidden')).toBe('true')
      expect(gap.nextElementSibling).toBe(trigger())
      expect(fixedScroller().contains(trigger())).toBe(false)
      expect(dynamicScroller().contains(trigger())).toBe(false)
      expect(trigger().nextElementSibling?.getAttribute('aria-hidden')).toBe('true')
      expect(trigger().getAttribute('title')).toBe('List all tabs')
    } finally { teardown(); cleanup() }
  })

  it('lists a row for every tab kind the strip renders, in step with its chips', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    act(() => { ctl().openFolder('/repo/pkg', SLOT) })
    act(() => { ctl().openDiff('/repo/a.md', 'new', 'old') })
    act(() => { ctl().openView('pins') })
    act(() => { ctl().openTerminal({ cwd: '/repo' }) })
    openAllTabs()
    const rows = within(screen.getByRole('menu')).getAllByRole('menuitemradio')
    const chipIds = Array.from(document.querySelectorAll('[data-tab-chip-id]')).map(el => el.getAttribute('data-tab-chip-id'))
    expect(rows.map(r => r.getAttribute('data-row-id'))).toEqual(chipIds)
    for (const prefix of ['file:', 'folder:', 'diff:', 'terminal:']) {
      expect(chipIds.some(id => id?.startsWith(prefix))).toBe(true)
    }
    expect(chipIds).toContain('pins')
    // The terminal row shows the live title, not its stored fallback.
    expect(within(screen.getByRole('menu')).getAllByText('Terminal').length).toBeGreaterThan(0)
  })

  it('lists exactly the chips the strip renders, in order, with the active one checked', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/alpha/index.ts', 'a', SLOT) })
    act(() => { ctl().openFile('/repo/beta/index.ts', 'b', SLOT) })
    openAllTabs()
    const menu = screen.getByRole('menu')
    const rows = within(menu).getAllByRole('menuitemradio')
    const chipIds = Array.from(document.querySelectorAll('[data-tab-chip-id]')).map(el => el.getAttribute('data-tab-chip-id'))
    expect(rows.map(r => r.getAttribute('data-row-id'))).toEqual(chipIds)
    expect(rows).toHaveLength(5)
    expect(within(menu).getAllByRole('separator')).toHaveLength(1)
    const checked = rows.filter(r => r.getAttribute('aria-checked') === 'true')
    expect(checked).toHaveLength(1)
    expect(checked[0].textContent).toContain('index.ts')
    // Two tabs share a basename, so the second line is what tells them apart.
    expect(within(menu).getByText('/repo/alpha/index.ts')).toBeTruthy()
    expect(within(menu).getByText('/repo/beta/index.ts')).toBeTruthy()
  })

  it('activates a picked row, and the chip follows', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    openAllTabs()
    const files = within(screen.getByRole('menu')).getByRole('menuitemradio', { name: /Files/ })
    act(() => { fireEvent.click(files) })
    expect(screen.getByRole('tab', { name: 'Files' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('keeps the old tab when the leave guard refuses', async () => {
    const onBeforeLeave = vi.fn(async () => false)
    const leadingTabs: SidePanelLeadingTab[] = [
      { id: 'crew-notes', title: 'Notes', icon: <span />, render: () => <div />, onBeforeLeave },
    ]
    renderPanel({ leadingTabs, leadingIds: ['crew-notes'] })
    openAllTabs()
    await act(async () => { fireEvent.click(within(screen.getByRole('menu')).getByRole('menuitemradio', { name: /Files/ })) })
    expect(onBeforeLeave).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('tab', { name: 'Notes' }).getAttribute('aria-selected')).toBe('true')
  })

  it('finds a chip whose id carries path punctuation, quotes and a backslash', () => {
    const ctl = renderPanel()
    const { writes } = stubGeometry(450)
    act(() => { ctl().openFile('/repo/it\'s "odd" [a]:b\\c.md', 'x', SLOT) })
    expect(writes).toContain(250)
  })

  it('leaves out a view the host withholds, in step with the chips', () => {
    renderPanel({ hiddenViews: new Set(['files']) })
    expect(document.querySelector('[data-tab-chip-id="files"]')).toBeNull()
    openAllTabs()
    const ids = within(screen.getByRole('menu')).getAllByRole('menuitemradio').map(r => r.getAttribute('data-row-id'))
    expect(ids).not.toContain('files')
    expect(ids).toEqual(Array.from(document.querySelectorAll('[data-tab-chip-id]')).map(el => el.getAttribute('data-tab-chip-id')))
  })

  it('shows a folder tab with its path as the second line and as the tooltip', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFolder('/repo/packages/design-system/src', SLOT) })
    openAllTabs()
    const row = within(screen.getByRole('menu')).getByRole('menuitemradio', { name: /src/ })
    expect(within(row).getByText('/repo/packages/design-system/src')).toBeTruthy()
    expect(row.getAttribute('title')).toBe('/repo/packages/design-system/src')
  })

  it('shows a project-relative path as the second line and keeps the full path in the tooltip', () => {
    const ctl = renderPanel({ projectDir: '/repo' })
    act(() => { ctl().openFile('/repo/src/db/migrations/0042_add_plans.sql', 'a', SLOT) })
    act(() => { ctl().openFile('/elsewhere/notes.md', 'b', SLOT) })
    openAllTabs()
    const menu = within(screen.getByRole('menu'))
    const inside = menu.getByRole('menuitemradio', { name: /0042_add_plans/ })
    expect(within(inside).getByText('src/db/migrations/0042_add_plans.sql')).toBeTruthy()
    expect(inside.getAttribute('title')).toBe('/repo/src/db/migrations/0042_add_plans.sql')
    // A path outside the project root stays absolute.
    expect(menu.getByText('/elsewhere/notes.md')).toBeTruthy()
  })

  it('omits the second line for a file at the project root', () => {
    const ctl = renderPanel({ projectDir: '/repo' })
    act(() => { ctl().openFile('/repo/package.json', 'a', SLOT) })
    openAllTabs()
    const row = within(screen.getByRole('menu')).getByRole('menuitemradio', { name: /package\.json/ })
    expect(within(row).getAllByText('package.json')).toHaveLength(1)
    expect(row.getAttribute('title')).toBe('/repo/package.json')
  })

  it('closes the menu and returns focus when the focused row\'s tab is closed', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    act(() => { ctl().openFile('/repo/b.md', 'b', SLOT) })
    openAllTabs()
    const rowFor = (id: string) => document.querySelector<HTMLElement>(`[role="menuitemradio"][data-row-id="${id}"]`)!
    act(() => { rowFor('file:/repo/a.md').focus() })
    act(() => { ctl().closeTab('file:/repo/a.md') })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(trigger())
  })

  it('is present on a narrow viewport and in the bottom dock', () => {
    viewport.mobile = true
    try {
      renderPanel()
      expect(trigger().previousElementSibling?.getAttribute('aria-hidden')).toBe('true')
    } finally { viewport.mobile = false; cleanup() }
    act(() => { setSidePanelDock('bottom') })
    try {
      renderPanel()
      expect(trigger().previousElementSibling?.getAttribute('aria-hidden')).toBe('true')
    } finally { act(() => { setSidePanelDock('right') }) }
  })

  it('reveals the chip when the current row is picked (no active-id change)', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    // Geometry arrives after the open, as a resize or a manual scroll would leave it.
    const { writes } = stubGeometry(450)
    const before = writes.length
    openAllTabs()
    const current = within(screen.getByRole('menu')).getAllByRole('menuitemradio').find(r => r.getAttribute('aria-checked') === 'true')!
    act(() => { fireEvent.click(current) })
    expect(writes.length).toBeGreaterThan(before)
    expect(writes[writes.length - 1]).toBe(250)
  })
})

describe('side panel reveal of the active tab', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('scrolls a newly opened, clipped tab into the dynamic scroller', () => {
    const ctl = renderPanel()
    const { writes, scrolled } = stubGeometry(450)
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    expect(writes).toContain(250)
    expect(scrolled.get(dynamicScroller())).toBe(250)
    expect(scrolled.get(fixedScroller()) ?? 0).toBe(0)
  })

  it('writes nothing for a chip that is already inside', () => {
    const ctl = renderPanel()
    const { writes } = stubGeometry(100)
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    expect(writes).toEqual([])
  })

  it('moves the pinned scroller, not the dynamic one, for a pinned chip', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    const { scrolled } = stubGeometry(450)
    act(() => { fireEvent.click(chip('files')) })
    expect(scrolled.get(fixedScroller())).toBe(250)
    expect(scrolled.get(dynamicScroller()) ?? 0).toBe(0)
  })

  it('does not undo the user scrolling when an unrelated tab opens or closes', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/a.md', 'a', SLOT) })
    const { writes, scrolled } = stubGeometry(450)
    act(() => { scrolled.set(dynamicScroller(), 0) })
    const base = writes.length
    act(() => { ctl().openFile('/repo/b.md', 'b', SLOT) })
    act(() => { fireEvent.click(chip(ctl().tabs.find(t => t.path === '/repo/a.md')!.id)) })
    const afterSwitch = writes.length
    expect(afterSwitch).toBeGreaterThan(base)
    act(() => { scrolled.set(dynamicScroller(), 10) })
    act(() => { ctl().closeTab(ctl().tabs.find(t => t.path === '/repo/b.md')!.id) })
    expect(writes.length).toBe(afterSwitch)
  })
})

describe('side panel wheel scrolling', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('moves each scroller by its own geometry', () => {
    renderPanel()
    // The pinned group is narrower than the dynamic one, so its end is nearer.
    const { scrolled } = stubGeometry(0, { fixed: 650, dynamic: 300 })
    act(() => { scrolled.set(fixedScroller(), 40) })
    const atFixedEnd = new WheelEvent('wheel', { deltaY: 40, cancelable: true, bubbles: true })
    act(() => { fixedScroller().dispatchEvent(atFixedEnd) })
    expect(scrolled.get(fixedScroller())).toBe(50)
    expect(scrolled.get(dynamicScroller()) ?? 0).toBe(0)
  })

  it('scrolls the dynamic group and the pinned group sideways, and cancels only when it moved', () => {
    renderPanel()
    const { scrolled } = stubGeometry(0)
    for (const el of [dynamicScroller(), fixedScroller()]) {
      const ev = new WheelEvent('wheel', { deltaY: 40, cancelable: true, bubbles: true })
      act(() => { el.dispatchEvent(ev) })
      expect(scrolled.get(el)).toBe(40)
      expect(ev.defaultPrevented).toBe(true)
    }
    act(() => { scrolled.set(dynamicScroller(), 400) })
    const atEnd = new WheelEvent('wheel', { deltaY: 40, cancelable: true, bubbles: true })
    act(() => { dynamicScroller().dispatchEvent(atEnd) })
    expect(atEnd.defaultPrevented).toBe(false)
    const mixed = new WheelEvent('wheel', { deltaY: 40, deltaX: 5, cancelable: true, bubbles: true })
    act(() => { dynamicScroller().dispatchEvent(mixed) })
    expect(mixed.defaultPrevented).toBe(false)
  })
})
