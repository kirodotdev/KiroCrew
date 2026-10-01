import React, { useRef } from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UseVirtualChatOptions, UseVirtualChatReturn } from '../hooks/virtualizer/types'
import type { DisplayItem } from '../pages/chat/types'

const live = vi.hoisted(() => new Map<string, UseVirtualChatReturn<DisplayItem>>())
// Observe the real hook, including its HeightIndex, measurement callbacks and
// persistence. No mocked height/index/window implementation can mask the race.
vi.mock('../hooks/virtualizer/useVirtualChat', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/virtualizer/useVirtualChat')>()
  return {
    ...actual,
    useVirtualChat: (opts: UseVirtualChatOptions<DisplayItem>) => {
      const result = actual.useVirtualChat(opts)
      live.set(opts.sessionId, result)
      return result
    },
  }
})

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { HeightCache } from '../hooks/virtualizer/HeightCache'
import { HeightIndex } from '../hooks/virtualizer/HeightIndex'
import VirtualTranscript from '../chat-core/transcript/VirtualTranscript'
import TranscriptScrollShell, { PANE_WIDTH_PROPERTY, useTranscriptWidth } from '../pages/chat/TranscriptScrollShell'

const items: DisplayItem[] = Array.from({ length: 120 }, (_, idx) => ({
  kind: 'single', idx, msg: { role: 'assistant', content: 'table', ts: `m${idx}` },
}))
const keyAt = (_item: DisplayItem, index: number) => `row-m${index}`
const renderRow = () => <span />

// The main page's width/virtualizer/shell composition without dashboard data
// providers. chatTableHeightScope.test pins that ChatPage uses this same hook,
// binds the shell element, and passes canMeasure beside the unchanged template.
// `shell={false}` is ChatPage's welcome hero: the host and its stable ref are
// mounted while no scroller exists, exactly as the page's first render on an
// empty slot and its conversation -> empty -> conversation round trip.
function MainHost({ shell = true, id = 'main' }: { shell?: boolean; id?: string }) {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const { widthBucket: scrollerWidthBucket, canMeasure, bindScroller } = useTranscriptWidth()
  const virt = useVirtualChat({
    items, getKey: keyAt, sessionId: id,
    heightScopeKey: `${id}:tables1:w${scrollerWidthBucket}`, canMeasure,
    externalScrollerRef: scrollerRef, initialPlacement: 'top', followOutput: false,
  })
  if (!shell) return <div data-welcome />
  return <TranscriptScrollShell scrollerRef={scrollerRef} onScrollerElement={bindScroller} virt={virt} onScroll={() => {}} loadingOlder={false}>
    {virt.virtualItems.filter(row => row.mounted).map(row =>
      <div key={row.key} ref={virt.measureRef(row.index)} data-display-index={row.index} />)}
  </TranscriptScrollShell>
}

class Observer {
  static all: Observer[] = []
  targets = new Set<Element>()
  constructor(readonly callback: ResizeObserverCallback) { Observer.all.push(this) }
  observe(el: Element) { this.targets.add(el) }
  unobserve(el: Element) { this.targets.delete(el) }
  disconnect() { this.targets.clear() }
  fire(targets = [...this.targets]) {
    this.callback(targets.map(target => ({ target })) as ResizeObserverEntry[], this as unknown as ResizeObserver)
  }
}

let paneWidth = 1220
let scrollTop = 0
const owners = new Set<HeightIndex>()

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  live.clear()
  owners.clear()
  Observer.all = []
  paneWidth = 1220
  scrollTop = 0
  vi.stubGlobal('ResizeObserver', Observer)
  // Frame delivery is explicit in this test: width debounce and persistence
  // timers run, but unrelated follow/window rAFs do not move the test viewport.
  vi.stubGlobal('requestAnimationFrame', () => 0)
  vi.stubGlobal('cancelAnimationFrame', () => {})
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => paneWidth)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(400)
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(30000)
  vi.spyOn(HTMLElement.prototype, 'scrollTop', 'get').mockImplementation(() => scrollTop)
  // Park the reader explicitly; background anchor/pin writes must not move
  // this fixture to row zero between our resize and the cache assertion.
  vi.spyOn(HTMLElement.prototype, 'scrollTop', 'set').mockImplementation(() => {})
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const index = this.dataset.displayIndex
    const shell = this.closest<HTMLElement>('.chat-container')
    const published = Number.parseFloat(shell?.style.getPropertyValue(PANE_WIDTH_PROPERTY) ?? '') || 900
    // A shrinking pane can wrap prose BEFORE the publisher runs; a growing
    // table waits for publication. This drives both callback-order branches.
    const base = Math.min(paneWidth, published) > 1100 ? 100 : 300
    const height = index === undefined ? 400 : base + Number(index)
    // The sentinel lives at the scroll origin, not the viewport origin. Its
    // rect must move with scrolling or leadingOffset cancels all scrollTop.
    const top = this.classList.contains('chat-container') ? 0 : index === undefined ? -scrollTop
      : base * Number(index) + Number(index) * (Number(index) - 1) / 2 - scrollTop
    return { x: 0, y: top, top, left: 0, right: paneWidth, bottom: top + height, width: paneWidth, height, toJSON() {} }
  })
  const setMeasured = HeightIndex.prototype.setMeasured
  vi.spyOn(HeightIndex.prototype, 'setMeasured').mockImplementation(function (this: HeightIndex, index, height) {
    owners.add(this)
    setMeasured.call(this, index, height)
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  localStorage.clear()
})

function persisted(scope: string, index: number) {
  for (const owner of owners) owner.flush()
  return new HeightCache(scope).peek(`row-m${index}`)
}

const hosts = [
  ['main', 'main:tables1'],
  ['bare', 'bare:tables1'],
  ['pane:slot', 'slot:tables1:pane'],
  ['side:slot', 'slot:tables1:side'],
  ['embed:slot', 'slot:tables1:embed'],
  ['custom:opaque:id', 'custom:opaque:id:tables1'],
] as const

describe.each(hosts)('%s width-cache ownership', (sessionId, prefix) => {
  it('rejects a new-scope measurement until its table width has been published', () => {
    const view = render(sessionId === 'main' ? <MainHost /> :
      <VirtualTranscript sessionId={sessionId} items={items} renderRow={renderRow}
        initialPlacement="top" followOutput={false} />)
    const shell = view.container.querySelector<HTMLElement>('.chat-container')!
    // Native observation delivers an initial row sample after mount.
    act(() => { for (const observer of Observer.all) {
      if ([...observer.targets].some(el => el.hasAttribute('data-display-index'))) observer.fire()
    } })
    const active = Observer.all.filter(observer => observer.targets.has(shell))
    const rows = active.find(observer => [...observer.targets].some(el => el.hasAttribute('data-display-index')))!
    // Child shell layout effect subscribes the publisher before the host's
    // width-scope effect. Deliver only the latter, keeping publication pending.
    const widths = active.filter(observer => observer !== rows)
    expect(widths).toHaveLength(2)
    act(() => { paneWidth = 1020; widths[1].fire(); vi.advanceTimersByTime(200) })
    expect(shell.style.getPropertyValue(PANE_WIDTH_PROPERTY)).toBe('1220px')
    const currentRows = () => Observer.all.find(observer => observer.targets.has(shell)
      && [...observer.targets].some(el => el.hasAttribute('data-display-index')))!
    act(() => { currentRows().fire() })
    expect(persisted(`${prefix}:w1024`, 0)).toBeUndefined()
    expect(live.get(sessionId)!.farmRecord(110, 'row-m110', 410)).toBe(false)
    act(() => { widths[0].fire(); currentRows().fire() })
    expect(persisted(`${prefix}:w1024`, 0)).toBe(300)
    expect(persisted(`${prefix}:w1216`, 0)).toBe(100)
  })

  it.each(['rows-first', 'publisher-first'] as const)('preserves old/new heights through away/back with %s delivery', order => {
    const view = render(sessionId === 'main' ? <MainHost /> :
      <VirtualTranscript sessionId={sessionId} items={items} renderRow={renderRow}
        initialPlacement="top" followOutput={false} />)
    const shell = view.container.querySelector<HTMLElement>('.chat-container')!
    // Native observation delivers an initial row sample after mount.
    act(() => { for (const observer of Observer.all) {
      if ([...observer.targets].some(el => el.hasAttribute('data-display-index'))) observer.fire()
    } })
    const virt = () => live.get(sessionId)!
    const oldScope = `${prefix}:w1216`
    const newScope = `${prefix}:w1024`
    const row = (index: number) => view.container.querySelector(`[data-display-index="${index}"]`)
    expect(row(0)).not.toBeNull()
    expect(persisted(oldScope, 0)).toBe(100)
    expect(virt().farmRecord(110, 'row-m110', 210)).toBe(true)
    expect(persisted(oldScope, 110)).toBe(210)

    const resize = (width: number) => {
      paneWidth = width
      const active = Observer.all.filter(observer => observer.targets.has(shell))
      const rowObservers = active.filter(observer => [...observer.targets].some(el => el.hasAttribute('data-display-index')))
      const widthObservers = active.filter(observer => !rowObservers.includes(observer))
      const ordered = order === 'rows-first' ? [...rowObservers, ...widthObservers] : [...widthObservers, ...rowObservers]
      for (const observer of ordered) observer.fire()
      // Also deliver the follow-up row reflow after the width property changed.
      for (const observer of rowObservers) observer.fire()
    }
    act(() => { resize(1020) })
    expect(shell.style.getPropertyValue(PANE_WIDTH_PROPERTY)).toBe('1020px')
    expect(persisted(oldScope, 0)).toBe(100)
    expect(virt().farmRecord(110, 'row-m110', 410)).toBe(false)
    expect(persisted(oldScope, 110)).toBe(210)

    // Ref seeds are another writer: mount a previously offscreen row DURING
    // the transition, while the old owner is still active. Row 0 leaves DOM.
    act(() => { scrollTop = 6000; expect(virt().mountIndex(60)).toBe(true) })
    expect(row(0)).toBeNull()
    expect(row(60)).not.toBeNull()
    expect(persisted(oldScope, 60)).toBeUndefined()
    act(() => { vi.advanceTimersByTime(199) })
    expect(persisted(oldScope, 0)).toBe(100)
    act(() => { vi.advanceTimersByTime(1) })
    // No new RO callback: the scope-change remeasurement must seed row 60.
    expect(persisted(newScope, 60)).toBe(360)
    expect(virt().farmRecord(110, 'row-m110', 410)).toBe(true)
    expect(persisted(newScope, 110)).toBe(410)
    expect(persisted(oldScope, 0)).toBe(100)

    act(() => { scrollTop = 21000; virt().mountIndex(60) })
    expect(row(60)).not.toBeNull()
    act(() => { resize(1220) })
    expect(persisted(newScope, 60)).toBe(360)
    act(() => { vi.advanceTimersByTime(200) })
    expect(persisted(oldScope, 60)).toBe(160)
    expect(persisted(newScope, 60)).toBe(360)
    expect(persisted(oldScope, 110)).toBe(210)
    expect(row(0)).toBeNull()
    // Real prefix-sum reuse for an UNMOUNTED row, not just a visible anchor:
    // returning to the old scope must price its spacer from the old height.
    expect(virt().estimateRowTop(1)! - virt().estimateRowTop(0)!).toBe(100)
    expect(virt().farmIsMeasured(0)).toBe(true)
    view.unmount()
    expect(new HeightCache(oldScope).peek('row-m0')).toBe(100)
    expect(new HeightCache(newScope).peek('row-m60')).toBe(360)
  })
})

describe('main host whose shell mounts after the host (welcome hero)', () => {
  const fireAll = (shell: HTMLElement) => {
    const active = Observer.all.filter(observer => observer.targets.has(shell))
    for (const observer of active) observer.fire()
    for (const observer of active) if (observer.targets.size > 1) observer.fire()
  }
  const resizeTo = (shell: HTMLElement, width: number) => {
    act(() => { paneWidth = width; fireAll(shell) })
    act(() => { vi.advanceTimersByTime(200) })
  }
  // The width-scope observer is the one bound LAST on the scroller alone: the
  // shell's publisher subscribes in the child's layout effect, the host's scope
  // observer after the shell hands it the element.
  const scopeObserver = (shell: HTMLElement) =>
    Observer.all.filter(observer => observer.targets.has(shell) && observer.targets.size === 1).at(-1)

  it('binds a late shell and its replacement, measuring each at its own width', () => {
    const innerWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth')
    // Initial bucket (w1504) deliberately differs from the pane (w1216).
    Object.defineProperty(window, 'innerWidth', { configurable: true, get: () => 1504 })
    try {
      const view = render(<MainHost shell={false} id="late" />)
      expect(view.container.querySelector('.chat-container')).toBeNull()
      expect(Observer.all.some(observer => observer.targets.size > 0)).toBe(false)

      view.rerender(<MainHost id="late" />)
      const first = view.container.querySelector<HTMLElement>('.chat-container')!
      const firstScope = scopeObserver(first)!
      expect(firstScope).toBeDefined()
      // Settled from the bound element without any resize notification.
      expect(persisted('late:tables1:w1216', 0)).toBe(100)
      act(() => { fireAll(first) })
      expect(persisted('late:tables1:w1216', 0)).toBe(100)
      expect(persisted('late:tables1:w1504', 0)).toBeUndefined()
      expect(live.get('late')!.farmRecord(110, 'row-m110', 210)).toBe(true)
      expect(persisted('late:tables1:w1216', 110)).toBe(210)

      resizeTo(first, 1020)
      expect(persisted('late:tables1:w1024', 0)).toBe(300)
      resizeTo(first, 1220)
      expect(persisted('late:tables1:w1216', 0)).toBe(100)
      expect(persisted('late:tables1:w1024', 0)).toBe(300)

      // Conversation -> empty welcome: the shell leaves, same stable ref.
      view.rerender(<MainHost shell={false} id="late" />)
      expect(firstScope.targets.size).toBe(0)
      paneWidth = 700
      view.rerender(<MainHost id="late" />)
      const second = view.container.querySelector<HTMLElement>('.chat-container')!
      expect(second).not.toBe(first)
      const secondScope = scopeObserver(second)!
      expect(secondScope).not.toBe(firstScope)
      expect(secondScope.targets.has(second)).toBe(true)
      expect(firstScope.targets.size).toBe(0)
      expect(persisted('late:tables1:w704', 0)).toBe(300)
      act(() => { fireAll(second) })
      expect(persisted('late:tables1:w704', 1)).toBe(301)

      resizeTo(second, 1220)
      expect(persisted('late:tables1:w1216', 0)).toBe(100)
      expect(persisted('late:tables1:w704', 0)).toBe(300)
      expect(live.get('late')!.farmRecord(110, 'row-m110', 210)).toBe(true)
      view.unmount()
      expect(secondScope.targets.size).toBe(0)
    } finally {
      if (innerWidth) Object.defineProperty(window, 'innerWidth', innerWidth)
      else delete (window as { innerWidth?: number }).innerWidth
    }
  })
})
