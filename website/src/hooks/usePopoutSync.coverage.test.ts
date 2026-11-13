import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePopoutSync } from './usePopoutSync'
import { SCENE_STORAGE_KEY, POPOUT_CHANNEL } from '../pages/scenes/config'

class FakeChannel {
  static instances: FakeChannel[] = []
  name: string
  posted: Array<{ type: string; scene?: string }> = []
  onmessage: ((e: MessageEvent) => void) | null = null
  closed = false
  constructor(name: string) {
    this.name = name
    FakeChannel.instances.push(this)
  }
  postMessage(data: { type: string; scene?: string }) { this.posted.push(data) }
  close() { this.closed = true }
  receive(data: { type: string; scene?: string }) {
    this.onmessage?.({ data } as MessageEvent)
  }
}

function lastChannel(): FakeChannel {
  const ch = FakeChannel.instances.at(-1)
  if (!ch) throw new Error('no channel was opened')
  return ch
}

beforeEach(() => {
  FakeChannel.instances = []
  localStorage.clear()
  vi.stubGlobal('BroadcastChannel', FakeChannel)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('usePopoutSync main window', () => {
  it('opens the shared channel and pings for a popout on mount', () => {
    renderHook(() => usePopoutSync(false))
    const ch = lastChannel()
    expect(ch.name).toBe(POPOUT_CHANNEL)
    expect(ch.posted).toEqual([{ type: 'ping' }])
  })

  it('marks the popout active on popout-open and inactive on popout-close', () => {
    const { result } = renderHook(() => usePopoutSync(false))
    expect(result.current.popoutActive).toBe(false)
    act(() => lastChannel().receive({ type: 'popout-open' }))
    expect(result.current.popoutActive).toBe(true)
    act(() => lastChannel().receive({ type: 'popout-close' }))
    expect(result.current.popoutActive).toBe(false)
  })

  it('marks the popout active on a pong reply', () => {
    const { result } = renderHook(() => usePopoutSync(false))
    act(() => lastChannel().receive({ type: 'pong' }))
    expect(result.current.popoutActive).toBe(true)
  })

  it('saves an incoming scene and forwards it to onSceneChange', () => {
    const onSceneChange = vi.fn()
    renderHook(() => usePopoutSync(false, onSceneChange))
    act(() => lastChannel().receive({ type: 'scene', scene: 'wizard' }))
    expect(localStorage.getItem(SCENE_STORAGE_KEY)).toBe('wizard')
    expect(onSceneChange).toHaveBeenCalledWith('wizard')
  })

  it('ignores a ping sent by another window', () => {
    renderHook(() => usePopoutSync(false))
    const ch = lastChannel()
    const before = ch.posted.length
    act(() => ch.receive({ type: 'ping' }))
    expect(ch.posted.length).toBe(before)
  })

  it('drops the popout after a heartbeat with no pong, and pings again', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => usePopoutSync(false))
    act(() => lastChannel().receive({ type: 'pong' }))
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(result.current.popoutActive).toBe(true)
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(result.current.popoutActive).toBe(false)
    expect(lastChannel().posted.filter(m => m.type === 'ping').length).toBe(3)
  })

  it('stops the heartbeat and closes the channel on unmount', () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() => usePopoutSync(false))
    const ch = lastChannel()
    unmount()
    expect(ch.closed).toBe(true)
    const pings = ch.posted.length
    act(() => { vi.advanceTimersByTime(20_000) })
    expect(ch.posted.length).toBe(pings)
  })

  it('broadcastScene posts the scene on the channel', () => {
    const { result } = renderHook(() => usePopoutSync(false))
    act(() => result.current.broadcastScene('office'))
    expect(lastChannel().posted.at(-1)).toEqual({ type: 'scene', scene: 'office' })
  })
})

describe('usePopoutSync popout window', () => {
  it('announces itself on mount and never runs a heartbeat', () => {
    vi.useFakeTimers()
    renderHook(() => usePopoutSync(true))
    const ch = lastChannel()
    expect(ch.posted).toEqual([{ type: 'popout-open' }])
    act(() => { vi.advanceTimersByTime(20_000) })
    expect(ch.posted).toEqual([{ type: 'popout-open' }])
  })

  it('answers a ping with a pong', () => {
    renderHook(() => usePopoutSync(true))
    const ch = lastChannel()
    act(() => ch.receive({ type: 'ping' }))
    expect(ch.posted.at(-1)).toEqual({ type: 'pong' })
  })

  it('applies a scene from the main window', () => {
    const onSceneChange = vi.fn()
    renderHook(() => usePopoutSync(true, onSceneChange))
    act(() => lastChannel().receive({ type: 'scene', scene: 'underwater' }))
    expect(localStorage.getItem(SCENE_STORAGE_KEY)).toBe('underwater')
    expect(onSceneChange).toHaveBeenCalledWith('underwater')
  })

  it('posts popout-close on unmount and closes the channel', () => {
    const { unmount } = renderHook(() => usePopoutSync(true))
    const ch = lastChannel()
    unmount()
    expect(ch.posted.at(-1)).toEqual({ type: 'popout-close' })
    expect(ch.closed).toBe(true)
  })

  it('posts popout-close when the window unloads', () => {
    renderHook(() => usePopoutSync(true))
    const ch = lastChannel()
    window.dispatchEvent(new Event('beforeunload'))
    expect(ch.posted.at(-1)).toEqual({ type: 'popout-close' })
  })
})

describe('usePopoutSync without BroadcastChannel', () => {
  it('opens no channel and broadcastScene is a silent no-op', () => {
    vi.stubGlobal('BroadcastChannel', undefined)
    const { result } = renderHook(() => usePopoutSync(false))
    expect(FakeChannel.instances.length).toBe(0)
    expect(() => act(() => result.current.broadcastScene('office'))).not.toThrow()
  })
})

describe('usePopoutSync openPopout', () => {
  it('opens the worlds popout window with the sized, named feature string', () => {
    const win = { focus: vi.fn(), closed: false } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValue(win)
    const { result } = renderHook(() => usePopoutSync(false))
    act(() => result.current.openPopout())
    expect(open).toHaveBeenCalledTimes(1)
    const [url, name, features] = open.mock.calls[0]
    expect(url).toBe('/worlds-popout')
    expect(name).toBe('kirocrew-worlds')
    expect(features).toMatch(/^width=[\d.]+,height=[\d.]+,left=-?[\d.]+,top=-?[\d.]+,resizable=yes$/)
  })

  it('focuses the existing popout instead of opening a second one', () => {
    const win = { focus: vi.fn(), closed: false } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValue(win)
    const { result } = renderHook(() => usePopoutSync(false))
    act(() => result.current.openPopout())
    act(() => result.current.openPopout())
    expect(open).toHaveBeenCalledTimes(1)
    expect(win.focus).toHaveBeenCalledTimes(1)
  })

  it('opens a new popout once the previous one has been closed', () => {
    const first = { focus: vi.fn(), closed: false } as unknown as Window
    const second = { focus: vi.fn(), closed: false } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValueOnce(first).mockReturnValueOnce(second)
    const { result } = renderHook(() => usePopoutSync(false))
    act(() => result.current.openPopout())
    ;(first as { closed: boolean }).closed = true
    act(() => result.current.openPopout())
    expect(open).toHaveBeenCalledTimes(2)
    expect(first.focus).not.toHaveBeenCalled()
  })
})
