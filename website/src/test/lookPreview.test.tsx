/**
 * The look-preview frame (utils/lookPreview.ts): the first-run step's scaled
 * dashboard is a passive mirror of the parent document -- it opens no first-run
 * chapter and follows the parent's browser-local picks through `storage`.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { LOOK_PREVIEW_PARAM, isLookPreviewFrame, lookPreviewSrc } from '../utils/lookPreview'
import { LIQUID_GLASS_STORAGE_KEY } from '../utils/liquidGlass'
import { useLiquidGlass } from '../hooks/useLiquidGlass'
import { LOOK_PREVIEW_SLOT_KEY, lookPreviewSlotDetail, lookPreviewSlots } from '../utils/lookPreviewFixtures'
import { api } from '../api/client'
import { LOOK_PREVIEW_SCROLL_BACK_PX, followParentLiquidGlass, installLookPreviewNetworkFence, installLookPreviewStorageFence, scrollDemoTranscriptUnderDock } from '../utils/lookPreviewBoot'

function withSearch(search: string) {
  const spy = vi.spyOn(window, 'location', 'get')
  spy.mockReturnValue({ ...window.location, search } as Location)
  return () => spy.mockRestore()
}

afterEach(() => {
  localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
  document.documentElement.removeAttribute('data-reduce-transparency')
})

describe('lookPreview', () => {
  it('the frame src carries the flag the frame reads', () => {
    expect(lookPreviewSrc()).toBe(`/chat?${LOOK_PREVIEW_PARAM}=1`)
    expect(isLookPreviewFrame()).toBe(false)
    const restore = withSearch(`?${LOOK_PREVIEW_PARAM}=1`)
    try {
      expect(isLookPreviewFrame()).toBe(true)
    } finally {
      restore()
    }
  })

  it('inside the frame, followParentLiquidGlass applies the parent switch now and on every storage event', () => {
    const restore = withSearch(`?${LOOK_PREVIEW_PARAM}=1`)
    try {
      localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
      expect(followParentLiquidGlass()).toBe(true)
      expect(document.documentElement.dataset.reduceTransparency).toBe('off')
      localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
      window.dispatchEvent(new StorageEvent('storage', { key: LIQUID_GLASS_STORAGE_KEY, newValue: null }))
      expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    } finally {
      restore()
    }
  })

  it('outside the frame, nothing follows storage (an ordinary tab keeps its own state)', () => {
    expect(followParentLiquidGlass()).toBe(false)
    const { result } = renderHook(() => useLiquidGlass())
    act(() => {
      localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
      window.dispatchEvent(new StorageEvent('storage', { key: LIQUID_GLASS_STORAGE_KEY, newValue: 'on' }))
    })
    expect(result.current.liquidGlass).toBe(false)
  })
})

describe('lookPreview fixtures', () => {
  it('is one demo session whose conversation ends on an assistant turn, all from the catalog', () => {
    const slots = lookPreviewSlots()
    expect(slots).toHaveLength(1)
    expect(slots[0].key).toBe(LOOK_PREVIEW_SLOT_KEY)
    expect(slots[0].title).toBe('Set up the project')
    const detail = lookPreviewSlotDetail()
    expect(detail.messages.length).toBe(slots[0].messages)
    expect(detail.messages[0].role).toBe('user')
    expect(detail.messages.at(-1)?.role).toBe('assistant')
    expect(detail.running).toBe(false)
    for (const m of detail.messages) expect(m.content).not.toMatch(/^components\./)
  })

  it('inside the frame the chat reads answer from the fixtures, not the gateway', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const restore = withSearch(`?${LOOK_PREVIEW_PARAM}=1`)
    try {
      const slots = await api.chatSlots()
      expect(slots[0].key).toBe(LOOK_PREVIEW_SLOT_KEY)
      const detail = await api.chatSlotDetail('anything', 50)
      expect(detail.messages.length).toBeGreaterThan(0)
      expect(fetchSpy).not.toHaveBeenCalled()
    } finally {
      restore()
      fetchSpy.mockRestore()
    }
  })
})

describe('lookPreview fences', () => {
  it('the storage fence reads through to the real store and keeps every write in the frame', () => {
    const restoreSearch = withSearch(`?${LOOK_PREVIEW_PARAM}=1`)
    const win = {} as Window & { localStorage: Storage }
    const real = new Map<string, string>([['mc-theme', 'dark'], ['mc-active-slot-chat', 'chat-real']])
    const realStore = {
      get length() { return real.size },
      key: (i: number) => [...real.keys()][i] ?? null,
      getItem: (k: string) => real.get(k) ?? null,
      setItem: (k: string, v: string) => { real.set(k, v) },
      removeItem: (k: string) => { real.delete(k) },
      clear: () => real.clear(),
    } as Storage
    const listeners: Array<(e: StorageEvent) => void> = []
    const session = new Map<string, string>([['mc-unread-since', '{"chat-1":1}']])
    const realSession = {
      get length() { return session.size },
      key: (i: number) => [...session.keys()][i] ?? null,
      getItem: (k: string) => session.get(k) ?? null,
      setItem: (k: string, v: string) => { session.set(k, v) },
      removeItem: (k: string) => { session.delete(k) },
      clear: () => session.clear(),
    } as Storage
    Object.defineProperty(win, 'localStorage', { configurable: true, get: () => realStore })
    Object.defineProperty(win, 'sessionStorage', { configurable: true, get: () => realSession })
    ;(win as unknown as { addEventListener: unknown }).addEventListener = (_t: string, fn: (e: StorageEvent) => void) => { listeners.push(fn) }
    try {
      expect(installLookPreviewStorageFence(win)).toBe(true)
      const fenced = win.localStorage
      expect(fenced).not.toBe(realStore)
      // Reads fall through.
      expect(fenced.getItem('mc-theme')).toBe('dark')
      // Writes and removals stay in the frame: the real store is untouched.
      fenced.setItem('mc-active-slot-chat', 'look-preview')
      fenced.removeItem('mc-theme')
      expect(fenced.getItem('mc-active-slot-chat')).toBe('look-preview')
      expect(fenced.getItem('mc-theme')).toBeNull()
      expect(real.get('mc-active-slot-chat')).toBe('chat-real')
      expect(real.get('mc-theme')).toBe('dark')
      // The parent writing a key drops the frame's shadow of it: the parent wins.
      real.set('mc-theme', 'light')
      for (const fn of listeners) fn({ key: 'mc-theme' } as StorageEvent)
      expect(fenced.getItem('mc-theme')).toBe('light')
      expect(fenced.length).toBe(2)
      // sessionStorage is shared by same-origin frames too, and fenced the same way.
      const fencedSession = (win as unknown as { sessionStorage: Storage }).sessionStorage
      expect(fencedSession).not.toBe(realSession)
      fencedSession.setItem('mc-unread-since', '{}')
      expect(fencedSession.getItem('mc-unread-since')).toBe('{}')
      expect(session.get('mc-unread-since')).toBe('{"chat-1":1}')
    } finally {
      restoreSearch()
    }
  })

  it('the storage fence installs nothing outside the frame', () => {
    const win = {} as Window
    Object.defineProperty(win, 'localStorage', { configurable: true, get: () => localStorage })
    expect(installLookPreviewStorageFence(win)).toBe(false)
    expect(win.localStorage).toBe(localStorage)
  })

  it('inside the frame no write leaves the document: raw fetch non-GET, BroadcastChannel posts and WebSocket sends are swallowed; reads pass', async () => {
    const realFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    const posted: unknown[] = []
    class FakeChannel { name: string; constructor(name: string) { this.name = name } postMessage(m: unknown) { posted.push(m) } close() {} }
    class FakeSocket { static sent: unknown[] = []; static dialed: string[] = []; static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3; constructor(url: string) { FakeSocket.dialed.push(url) } send(m: unknown) { FakeSocket.sent.push(m) } }
    const beacons: unknown[] = []
    const parentPosts: unknown[] = []
    const xhrSent: string[] = []
    class FakeXhr { m = ''; open(method: string) { this.m = method } send() { xhrSent.push(this.m) } }
    const parent = { postMessage: (m: unknown) => { parentPosts.push(m) } }
    const win = {
      fetch: realFetch, BroadcastChannel: FakeChannel, WebSocket: FakeSocket, XMLHttpRequest: FakeXhr,
      navigator: { sendBeacon: (u: string) => { beacons.push(u); return true } },
      parent, indexedDB: { open: () => ({}), deleteDatabase: () => ({}) }, caches: {},
      location: { href: 'http://127.0.0.1:7788/chat?look-preview=1' },
    } as unknown as Window
    const restore = withSearch(`?${LOOK_PREVIEW_PARAM}=1`)
    try {
      expect(installLookPreviewNetworkFence(win)).toBe(true)
      const g = win as unknown as { fetch: typeof fetch; BroadcastChannel: typeof BroadcastChannel; WebSocket: typeof WebSocket }
      // Writes, in every spelling a caller uses: never sent, answered as an empty JSON success.
      const put = await g.fetch('/api/ui-prefs', { method: 'PUT', body: '{}' })
      expect(await put.json()).toEqual({})
      await g.fetch(new Request('/api/x', { method: 'POST' }))
      await g.fetch('/api/y', { method: 'delete' })
      expect(realFetch).not.toHaveBeenCalled()
      // Reads pass through, GET and HEAD alike.
      await g.fetch('/api/dashboard/config')
      await g.fetch('/api/file-read', { method: 'HEAD' })
      expect(realFetch).toHaveBeenCalledTimes(2)
      // A channel the frame opens posts nothing; a socket it opens sends nothing.
      const ch = new g.BroadcastChannel('popout')
      ch.postMessage({ hello: 1 })
      expect(posted).toEqual([])
      expect(ch).toBeInstanceOf(FakeChannel)
      // The dashboard's own socket may open (and sends nothing); any other -- a
      // docked terminal's -- is an inert closed object that never dials.
      const Sock = g.WebSocket as unknown as new (url: string) => { send(m: unknown): void; readyState: number }
      const own = new Sock('ws://127.0.0.1:7788/api/ws?caps=slot_patch')
      own.send('x')
      expect(own).toBeInstanceOf(FakeSocket)
      expect(FakeSocket.sent).toEqual([])
      const term = new Sock('ws://127.0.0.1:7788/api/ws/terminal/abc')
      expect(term).not.toBeInstanceOf(FakeSocket)
      expect(term.readyState).toBe(3)
      term.send('y')
      expect(FakeSocket.dialed).toEqual(['ws://127.0.0.1:7788/api/ws?caps=slot_patch'])
      // The other roads out: beacon, XHR write, parent postMessage, IndexedDB, Cache API.
      const w = win as unknown as { navigator: Navigator; XMLHttpRequest: typeof XMLHttpRequest; parent: Window; indexedDB: IDBFactory; caches: unknown }
      w.navigator.sendBeacon('/api/metrics', '{}')
      expect(beacons).toEqual([])
      const xPut = new w.XMLHttpRequest(); xPut.open('PUT', '/api/ui-prefs'); xPut.send()
      const xGet = new w.XMLHttpRequest(); xGet.open('GET', '/api/x'); xGet.send()
      expect(xhrSent).toEqual(['GET'])
      w.parent.postMessage({ hello: 1 }, 'http://127.0.0.1:7788')
      expect(parentPosts).toEqual([])
      expect(() => w.indexedDB.open('kc')).toThrow()
      expect(w.caches).toBeUndefined()
    } finally {
      restore()
    }
  })

  it('the network fence installs nothing outside the frame', () => {
    const realFetch = vi.fn()
    const win = { fetch: realFetch } as unknown as Window
    expect(installLookPreviewNetworkFence(win)).toBe(false)
    expect((win as unknown as { fetch: unknown }).fetch).toBe(realFetch)
  })
})

describe('lookPreview scroll-back', () => {
  it('inside the frame, scrolls the transcript up by the scroll-back once it overflows; outside, never', () => {
    vi.useFakeTimers()
    const scroller = document.createElement('div')
    scroller.className = 'chat-container'
    Object.defineProperty(scroller, 'scrollHeight', { value: 1200, configurable: true })
    Object.defineProperty(scroller, 'clientHeight', { value: 500, configurable: true })
    document.body.appendChild(scroller)
    try {
      expect(scrollDemoTranscriptUnderDock()).toBe(false)
      vi.advanceTimersByTime(1000)
      expect(scroller.scrollTop).toBe(0)
      const restore = withSearch(`?${LOOK_PREVIEW_PARAM}=1`)
      try {
        expect(scrollDemoTranscriptUnderDock()).toBe(true)
        vi.advanceTimersByTime(1000)
        expect(scroller.scrollTop).toBe(1200 - 500 - LOOK_PREVIEW_SCROLL_BACK_PX)
      } finally {
        restore()
      }
    } finally {
      scroller.remove()
      vi.useRealTimers()
    }
  })
})
