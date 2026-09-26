import { act, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { UseVirtualChatOptions, UseVirtualChatResult } from '../hooks/virtualizer/types'
import type { DisplayItem } from '../pages/chat/types'

const observedCalls = vi.hoisted(() => [] as { sessionId: string; scope: string }[])

vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (options: UseVirtualChatOptions<DisplayItem>) => {
    observedCalls.push({ sessionId: options.sessionId, scope: options.heightScopeKey ?? '' })
    return {
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(() => false),
      estimateRowTop: vi.fn(() => null),
      isAtBottom: true,
      virtualItems: [],
      measureRef: vi.fn(),
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
    } as unknown as UseVirtualChatResult<DisplayItem>
  },
}))

vi.mock('../pages/chat/TranscriptScrollShell', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ scrollerRef, children }: {
      scrollerRef: React.MutableRefObject<HTMLDivElement | null>
      children: React.ReactNode
    }) => createElement('div', { ref: scrollerRef }, children),
  }
})

import VirtualTranscript from '../chat-core/transcript/VirtualTranscript'
import { HeightCache } from '../hooks/virtualizer/HeightCache'
import { gcOrphanedStorage, gcSessionStorage } from '../utils/storageGc'

class FakeResizeObserver {
  static instance: FakeResizeObserver | undefined
  readonly callback: ResizeObserverCallback

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    FakeResizeObserver.instance = this
  }

  observe() {}
  disconnect() {}

  fire() {
    this.callback([], this as unknown as ResizeObserver)
  }
}

let paneWidth = 1220
let originalResizeObserver: typeof ResizeObserver | undefined
let clientWidthDescriptor: PropertyDescriptor | undefined

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  observedCalls.length = 0
  paneWidth = 1220
  FakeResizeObserver.instance = undefined
  originalResizeObserver = globalThis.ResizeObserver
  globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
  clientWidthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => paneWidth,
  })
})

afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  globalThis.ResizeObserver = originalResizeObserver as typeof ResizeObserver
  if (clientWidthDescriptor) Object.defineProperty(HTMLElement.prototype, 'clientWidth', clientWidthDescriptor)
  else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
})

it.each([
  ['chat-1-1', 'chat-1-1:tables1'],
  ['pane:chat-1-1', 'chat-1-1:tables1:pane'],
  ['side:chat-1-1', 'chat-1-1:tables1:side'],
  ['embed:chat-1-1', 'chat-1-1:tables1:embed'],
])('keeps %s anchor identity while rescoping desktop widths above the prose cap', (sessionId, scope) => {
  const { unmount } = render(<VirtualTranscript items={[]} renderRow={() => null} sessionId={sessionId} />)
  expect(observedCalls.at(-1)).toEqual({ sessionId, scope: `${scope}:w1216` })

  act(() => {
    paneWidth = 1020
    FakeResizeObserver.instance?.fire()
    vi.advanceTimersByTime(199)
  })
  expect(observedCalls.at(-1)?.scope).toBe(`${scope}:w1216`)
  act(() => { vi.advanceTimersByTime(1) })
  expect(observedCalls.at(-1)).toEqual({ sessionId, scope: `${scope}:w1024` })
  expect(observedCalls.every(call => call.sessionId === sessionId)).toBe(true)
  unmount()
})

it('keeps the main chat host uncapped at initialization and resize', () => {
  const source = readFileSync(join(__dirname, '../pages/ChatPage.tsx'), 'utf8')
  expect(source).toContain("typeof window !== 'undefined' ? Math.round(window.innerWidth / 16) * 16 : 944")
  expect(source).toContain('setScrollerWidthBucket(Math.round(el.clientWidth / 16) * 16)')
})

function sharedScope(sessionId: string): string {
  const { unmount } = render(<VirtualTranscript items={[]} renderRow={() => null} sessionId={sessionId} />)
  const call = observedCalls.at(-1)!
  expect(call.sessionId).toBe(sessionId)
  unmount()
  return call.scope
}

// Actual ChatPane / SideChat / ChatEmbed input forms. The existing
// chatCoreTranscript.contract test also pins the callers' prefix wiring.
const HOSTS = ['ChatPage', 'VirtualTranscript', 'pane', 'side', 'embed'] as const
type Host = typeof HOSTS[number]

/** Shared scopes come from the real component; the main page's unchanged
 *  template is read from source to avoid mounting the whole dashboard. */
function productionScope(host: Host, slot: string): string {
  if (host !== 'ChatPage') return sharedScope(host === 'VirtualTranscript' ? slot : `${host}:${slot}`)
  const source = readFileSync(join(__dirname, '../pages/ChatPage.tsx'), 'utf8')
  const template = /heightScopeKey: `\$\{activeSlot \?\? '__no_slot__'\}([^`]*)`/.exec(source)
  expect(template, 'ChatPage heightScopeKey template').not.toBeNull()
  return slot + template![1].replace('${scrollerWidthBucket}', '1216')
}

function persistHeight(scope: string, height = 120): void {
  const cache = new HeightCache(scope)
  cache.set('row-a', height)
  cache.flush()
  expect(localStorage.getItem(`vc_heights_${scope}`)).not.toBeNull()
}

it.each(HOSTS)('%s retains live raw-slot heights across startup GC', host => {
  const scope = productionScope(host, 'chat-1-1')
  persistHeight(scope)
  expect(gcOrphanedStorage(new Set(['chat-1-1']))).toBe(0)
  expect(new HeightCache(scope).peek('row-a')).toBe(120)
})

it.each(HOSTS)('%s deletes only the exact raw slot, not a prefixed sibling', host => {
  const scope = productionScope(host, 'chat-1-1')
  const sibling = productionScope(host, 'chat-1-10')
  persistHeight(scope)
  persistHeight(sibling)
  gcSessionStorage('chat-1-1')
  expect(localStorage.getItem(`vc_heights_${scope}`)).toBeNull()
  expect(new HeightCache(sibling).peek('row-a')).toBe(120)
})

it.each(HOSTS)('%s sweeps only orphaned raw-slot heights', host => {
  const scope = productionScope(host, 'chat-1-1')
  const sibling = productionScope(host, 'chat-1-10')
  persistHeight(scope)
  persistHeight(sibling)
  expect(gcOrphanedStorage(new Set(['chat-1-10']))).toBe(1)
  expect(localStorage.getItem(`vc_heights_${scope}`)).toBeNull()
  expect(new HeightCache(sibling).peek('row-a')).toBe(120)
})

it('partitions main, pane, side and embed heights for the same raw slot', () => {
  const scopes = (['ChatPage', 'pane', 'side', 'embed'] as const).map(host => productionScope(host, 'chat-1-1'))
  expect(new Set(scopes).size).toBe(4)
  scopes.forEach((scope, i) => persistHeight(scope, 100 + i))
  expect(gcOrphanedStorage(new Set(['chat-1-1']))).toBe(0)
  scopes.forEach((scope, i) => expect(new HeightCache(scope).peek('row-a')).toBe(100 + i))
  gcSessionStorage('chat-1-1')
  scopes.forEach(scope => expect(localStorage.getItem(`vc_heights_${scope}`)).toBeNull())
})

it.each(['custom:chat-1-1', 'pane:', 'pane:custom:chat-1-1', 'chat-1-1:pane', 'pane'])('keeps unrelated or incomplete caller ID %s opaque', sessionId => {
  expect(sharedScope(sessionId)).toBe(`${sessionId}:tables1:w1216`)
})
