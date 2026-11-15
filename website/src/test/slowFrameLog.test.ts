import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

/**
 * The log's contract has two halves: off, it costs nothing (no observer, nothing
 * kept), and on, it keeps the slowest frames and interactions without holding on
 * to the page. jsdom has no Long Animation Frame or Event Timing entries, so a
 * stand-in observer records how the module registers and lets a test hand it
 * entries.
 *
 * Loaded fresh per test: the module reads the flag once at import and keeps its
 * lists in module scope.
 */
const load = async () => await import('../dev/slowFrameLog')

interface FakeObserver {
  callback: (list: { getEntries: () => unknown[] }) => void
  observed: Array<Record<string, unknown>>
  disconnected: boolean
}
let observers: FakeObserver[] = []
let supported: string[] = []

function installObserver() {
  class Fake {
    static get supportedEntryTypes() { return supported }
    private self: FakeObserver
    constructor(callback: FakeObserver['callback']) {
      this.self = { callback, observed: [], disconnected: false }
      observers.push(this.self)
    }
    observe(init: Record<string, unknown>) { this.self.observed.push(init) }
    disconnect() { this.self.disconnected = true }
  }
  vi.stubGlobal('PerformanceObserver', Fake)
}

const deliver = (...entries: unknown[]) => observers[observers.length - 1].callback({ getEntries: () => entries })

const frame = (startTime: number, duration: number, scripts: unknown[] = [], blockingDuration = 0) =>
  ({ entryType: 'long-animation-frame', name: 'long-animation-frame', startTime, duration, blockingDuration, scripts })

const event = (name: string, interactionId: number, startTime: number, duration: number, target: Node | null = null, processingStart = startTime + 4, processingEnd = startTime + duration - 8) =>
  ({ entryType: 'event', name, interactionId, startTime, duration, processingStart, processingEnd, target })

describe('slow frame log', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    observers = []
    supported = ['long-animation-frame', 'event']
    installObserver()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('creates no observer and keeps nothing while off', async () => {
    const log = await load()
    expect(log.slowFrameSnapshot().enabled).toBe(false)
    expect(observers).toHaveLength(0)
    expect(log.slowFrameSnapshot()).toMatchObject({ enabled: false, frames: [], interactions: [] })
  })

  it('reads the stored flag at load and registers ONE observer for both entry types, asking for buffered entries', async () => {
    localStorage.setItem('mc-slow-frame-log', '1')
    const log = await load()
    expect(log.slowFrameSnapshot().enabled).toBe(true)
    expect(observers).toHaveLength(1)
    expect(observers[0].observed).toEqual([
      { type: 'long-animation-frame', buffered: true },
      { type: 'event', buffered: true, durationThreshold: 40 },
    ])
  })

  it('stays off in a build that locks Debug tools, whatever the stored flag says', async () => {
    localStorage.setItem('mc-slow-frame-log', '1')
    localStorage.setItem('kc-locked-controls', JSON.stringify(['debug_tools']))
    const log = await load()
    expect(log.slowFrameSnapshot().enabled).toBe(false)
    expect(observers).toHaveLength(0)
  })

  it('stays off when storage cannot be read', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('private mode') })
    const log = await load()
    expect(log.slowFrameSnapshot().enabled).toBe(false)
    expect(observers).toHaveLength(0)
  })

  it('registers only the entry types the browser has', async () => {
    supported = ['event']
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    expect(observers[0].observed).toEqual([{ type: 'event', buffered: true, durationThreshold: 40 }])
    expect(log.slowFrameSnapshot().framesSupported).toBe(false)
  })

  it('keeps the slowest frames, slowest first, with each frame\'s three longest scripts', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    const scripts = [
      { duration: 12.4, invoker: 'TimerHandler:setTimeout', sourceFunctionName: '', sourceURL: 'http://h/assets/a-1.js' },
      { duration: 150.6, invoker: 'Response.json.then', sourceFunctionName: 'load', sourceURL: 'http://h/assets/first-load-shared-Cm.js?v=1', forcedStyleAndLayoutDuration: 26.2 },
      { duration: 40, invoker: 'DIV.onclick', sourceFunctionName: 'go', sourceURL: '' },
      { duration: 3, invoker: 'x', sourceFunctionName: 'y', sourceURL: 'z.js' },
    ]
    deliver(frame(100, 60), frame(900, 181.4, scripts, 116.2), frame(2000, 75))
    const { frames } = log.slowFrameSnapshot()
    expect(frames.map(f => f.duration)).toEqual([181, 75, 60])
    expect(frames[0]).toEqual({
      at: 900, duration: 181, blocking: 116,
      scripts: [
        { duration: 151, invoker: 'Response.json.then', source: 'load first-load-shared-Cm.js', forcedLayout: 26 },
        { duration: 40, invoker: 'DIV.onclick', source: 'go', forcedLayout: 0 },
        { duration: 12, invoker: 'TimerHandler:setTimeout', source: 'a-1.js', forcedLayout: 0 },
      ],
    })
  })

  it('caps each list at 20 and drops the fastest, not the oldest', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    deliver(frame(1, 500))
    deliver(...Array.from({ length: 30 }, (_, i) => frame(10 + i, 60 + i)))
    const { frames } = log.slowFrameSnapshot()
    expect(frames).toHaveLength(20)
    expect(frames[0].duration).toBe(500)
    expect(frames[19].duration).toBe(71)
  })

  it('folds the entries of one interaction into its longest, and skips entries that are not interactions', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    const button = document.createElement('button')
    button.setAttribute('aria-label', 'Session options')
    deliver(
      event('pointerdown', 7, 1000, 136, button, 1003, 1090),
      event('pointerup', 7, 1100, 48, button),
      event('click', 7, 1100, 56, button),
      event('mouseover', 0, 990, 200, button),
      event('keydown', 8, 3000, 64, document.createElement('textarea')),
    )
    const { interactions } = log.slowFrameSnapshot()
    expect(interactions).toEqual([
      { at: 1000, duration: 136, name: 'pointerdown', target: 'button [Session options]', inputDelay: 3, processing: 87, presentation: 46 },
      { at: 3000, duration: 64, name: 'keydown', target: 'textarea', inputDelay: 4, processing: 52, presentation: 8 },
    ])
  })

  it('keeps a description of the target, never the element', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    const el = document.createElement('div')
    el.setAttribute('data-testid', 'row')
    deliver(event('click', 1, 10, 80, el))
    expect(log.slowFrameSnapshot().interactions[0].target).toBe('div [row]')
    expect(Object.values(log.slowFrameSnapshot().interactions[0]).some(v => v instanceof Node)).toBe(false)
  })

  it('names a press on an icon by the button around it', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    const button = document.createElement('button')
    button.setAttribute('aria-label', 'Session options')
    const icon = button.appendChild(document.createElementNS('http://www.w3.org/2000/svg', 'svg'))
    deliver(event('pointerdown', 1, 10, 120, icon))
    expect(log.slowFrameSnapshot().interactions[0].target).toBe('button [Session options]')
  })

  it('shortens a module script\'s invoker from its URL to its file name', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    deliver(frame(1, 160, [{ duration: 159, invoker: 'http://127.0.0.1:7776/assets/main-BTI9u8ny.js?x=1', sourceURL: 'http://127.0.0.1:7776/assets/main-BTI9u8ny.js' }]))
    expect(log.slowFrameSnapshot().frames[0].scripts[0]).toMatchObject({ invoker: 'main-BTI9u8ny.js', source: 'main-BTI9u8ny.js' })
    // An https URL is shortened the same way; an invoker that only mentions one is left whole.
    deliver(frame(2, 170, [
      { duration: 90, invoker: 'https://crew.example/assets/vendor-react-Zx.js', sourceURL: '' },
      { duration: 60, invoker: 'A.onclick (https://crew.example/x.js)', sourceURL: '' },
    ]))
    expect(log.slowFrameSnapshot().frames[0].scripts.map(s => s.invoker)).toEqual(['vendor-react-Zx.js', 'A.onclick (https://crew.example/x.js)'])
  })

  it('never reports a paint time below zero', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    // The browser rounds the duration to 8 ms: handlers done at 1042, painted at
    // 1043, reported as 40. The difference is -2.
    deliver(event('click', 1, 1000, 40, null, 1004, 1042))
    expect(log.slowFrameSnapshot().interactions[0]).toMatchObject({ duration: 40, processing: 38, presentation: 0 })
  })

  it('tells subscribers on each delivery and hands out a stable snapshot in between', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    const seen = vi.fn()
    const off = log.subscribeSlowFrameLog(seen)
    const before = log.slowFrameSnapshot()
    expect(log.slowFrameSnapshot()).toBe(before)
    deliver(frame(1, 90))
    expect(seen).toHaveBeenCalledTimes(1)
    expect(log.slowFrameSnapshot()).not.toBe(before)
    off()
    deliver(frame(2, 95))
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('turning it off disconnects the observer and forgets the readings', async () => {
    const log = await load()
    log.setSlowFrameLogEnabled(true)
    deliver(frame(1, 90), event('click', 1, 10, 80))
    log.setSlowFrameLogEnabled(false)
    expect(observers[0].disconnected).toBe(true)
    expect(log.slowFrameSnapshot()).toMatchObject({ enabled: false, frames: [], interactions: [] })
  })

  it('follows the toggle event and another tab\'s storage write', async () => {
    const log = await load()
    window.dispatchEvent(new CustomEvent(log.SLOW_FRAME_LOG_KEYS.ENABLED_EVENT, { detail: true }))
    expect(log.slowFrameSnapshot().enabled).toBe(true)
    localStorage.setItem(log.SLOW_FRAME_LOG_KEYS.ENABLED_KEY, '0')
    window.dispatchEvent(new StorageEvent('storage', { key: log.SLOW_FRAME_LOG_KEYS.ENABLED_KEY }))
    expect(log.slowFrameSnapshot().enabled).toBe(false)
    // Another tab clearing all of storage takes the flag with it.
    localStorage.setItem(log.SLOW_FRAME_LOG_KEYS.ENABLED_KEY, '1')
    window.dispatchEvent(new StorageEvent('storage', { key: log.SLOW_FRAME_LOG_KEYS.ENABLED_KEY }))
    expect(log.slowFrameSnapshot().enabled).toBe(true)
    localStorage.clear()
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    expect(log.slowFrameSnapshot().enabled).toBe(false)
  })
})
