import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import {
  BUZZ_DEBOUNCE_MS,
  DETECTION_RETRY_MS,
  HAPTICS_PLUGIN_NAME,
  HAPTICS_PLUGIN_ORIGIN,
  MC_MOUSE_HAPTICS_RECHECK_EVENT,
  MOUSE_HAPTICS_ENABLED_KEY,
  createHapticsBridge,
  identifyHapticsPlugin,
  isLoopbackHostname,
  loadMouseHapticsEnabled,
  pluginEventForChimeKind,
  probeHapticsPlugin,
  saveMouseHapticsEnabled,
  useMouseHaptics,
  type HapticsBridgeDeps,
} from '../hooks/useMouseHaptics'
import { MC_NOTIFICATION_EVENT } from '../hooks/notificationEvent'
import { __resetErrorJournalForTests, recentErrors } from '../utils/errorReport'

const SOUND_SETTINGS_KEY = 'mc-notification-sound'
const STATUS_URL = `${HAPTICS_PLUGIN_ORIGIN}/v1/status`
const KIROCREW_STATUS = { plugin: HAPTICS_PLUGIN_NAME, api: 1, events: ['turn_done', 'needs_input', 'notification'] }

type PluginAnswer = { ok: boolean; body?: unknown } | Error

interface FakePlugin {
  fetch: HapticsBridgeDeps['fetch']
  calls: Array<{ url: string; init: RequestInit | undefined }>
  answerStatus: (answer: PluginAnswer) => void
  answerEvents: (answer: PluginAnswer) => void
}

function fakePlugin(): FakePlugin {
  let statusAnswer: PluginAnswer = { ok: true, body: KIROCREW_STATUS }
  let eventAnswer: PluginAnswer = { ok: true, body: { raised: 'ok' } }
  const calls: FakePlugin['calls'] = []
  const respond = (answer: PluginAnswer): Promise<Response> => {
    if (answer instanceof Error) return Promise.reject(answer)
    return Promise.resolve({ ok: answer.ok, json: () => Promise.resolve(answer.body) } as Response)
  }
  return {
    calls,
    fetch: (input, init) => {
      const url = String(input)
      calls.push({ url, init })
      return respond(url === STATUS_URL ? statusAnswer : eventAnswer)
    },
    answerStatus: (answer) => { statusAnswer = answer },
    answerEvents: (answer) => { eventAnswer = answer },
  }
}

// An exclusive Web Lock: each request waits for the one before it, then runs its callback.
function makeLockManager(): LockManager {
  let previous: Promise<unknown> = Promise.resolve()
  const request = (name: string, callback: LockGrantedCallback) => {
    const granted = previous.then(() => callback({ name, mode: 'exclusive' } as Lock))
    previous = granted.catch(() => undefined)
    return granted
  }
  return { request, query: () => Promise.resolve({}) } as unknown as LockManager
}

const fetchAnswering = (body: string) => (() => Promise.resolve(new Response(body, { status: 200 }))) as typeof fetch

// The plugin answered 200, then its reply broke off before the body arrived, as when it unloads mid-reply.
const bodyFailsToArriveFetch = (() => Promise.resolve(new Response(new ReadableStream<Uint8Array>({
  start(controller) {
    controller.error(new TypeError('response body disconnected'))
  },
}), { status: 200 }))) as typeof fetch

describe('identifyHapticsPlugin', () => {
  it('throws when the plugin request is rejected', async () => {
    const rejectedFetch = (() => Promise.reject(new TypeError('Failed to fetch'))) as typeof fetch
    await expect(identifyHapticsPlugin(rejectedFetch)).rejects.toThrow('Failed to fetch')
  })

  it('throws when the plugin answers with a non-2xx status', async () => {
    const refusedFetch = (() => Promise.resolve({ ok: false, status: 503 } as Response)) as typeof fetch
    await expect(identifyHapticsPlugin(refusedFetch)).rejects.toThrow('status 503')
  })

  it('returns false when a successful answer is not JSON', async () => {
    await expect(identifyHapticsPlugin(fetchAnswering('<!doctype html><title>Another server</title>'))).resolves.toBe(false)
  })

  it('returns false when a successful answer is JSON but not an object', async () => {
    await expect(identifyHapticsPlugin(fetchAnswering('null'))).resolves.toBe(false)
  })

  it('throws when a successful response body fails to arrive', async () => {
    await expect(identifyHapticsPlugin(bodyFailsToArriveFetch)).rejects.toThrow('response body disconnected')
  })
})

describe('probeHapticsPlugin', () => {
  it('resolves false instead of throwing when a successful response body fails to arrive', async () => {
    await expect(probeHapticsPlugin(bodyFailsToArriveFetch)).resolves.toBe(false)
  })
})
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

const posted = (plugin: FakePlugin) =>
  plugin.calls.filter((call) => call.init?.method === 'POST').map((call) => call.url.replace(`${HAPTICS_PLUGIN_ORIGIN}/v1/events/`, ''))

const refuseStorageWrites = () => vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
  throw new DOMException('storage is disabled', 'SecurityError')
})

describe('createHapticsBridge', () => {
  let clock: number
  let plugin: FakePlugin
  let deps: HapticsBridgeDeps

  beforeEach(() => {
    localStorage.clear()
    __resetErrorJournalForTests()
    // The switch is the opt-in and starts off; these tests exercise a bridge the user turned on.
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '1')
    clock = 10_000
    plugin = fakePlugin()
    deps = { fetch: plugin.fetch, now: () => clock, wallClock: () => clock }
  })

  const chime = async (bridge: ReturnType<typeof createHapticsBridge>, kind: string | undefined) => {
    clock += 1000
    bridge.onChime(kind)
    await flush()
  }

  it('maps a finished turn, a question or approval, and everything else to the plugin events', async () => {
    const bridge = createHapticsBridge(deps)
    for (const kind of ['turn', 'approval', 'cron', 'agent', undefined]) await chime(bridge, kind)
    expect(posted(plugin)).toEqual(['turn_done', 'needs_input', 'notification', 'notification', 'notification'])
    expect(pluginEventForChimeKind('bogus')).toBe('notification')
  })

  it('stays silent wherever the sound settings silence the chime, and ignores the volume slider', async () => {
    const bridge = createHapticsBridge(deps)
    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ enabled: false }))
    await chime(bridge, 'turn')
    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ perCategory: { cron: 'none' } }))
    await chime(bridge, 'cron')
    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ perCategory: { all: 'none' } }))
    await chime(bridge, 'agent')
    expect(posted(plugin)).toEqual([])

    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ volume: 0, perCategory: { all: 'none', approval: 'pulse' } }))
    await chime(bridge, 'approval')
    expect(posted(plugin)).toEqual(['needs_input'])
  })

  it('probes the plugin once, then sends each alert straight away', async () => {
    const bridge = createHapticsBridge(deps)
    await chime(bridge, 'turn')
    await chime(bridge, 'approval')
    expect(plugin.calls.map((call) => call.url)).toEqual([
      STATUS_URL,
      `${HAPTICS_PLUGIN_ORIGIN}/v1/events/turn_done`,
      `${HAPTICS_PLUGIN_ORIGIN}/v1/events/needs_input`,
    ])
  })

  it('shares one probe between alerts that arrive while it is out', async () => {
    const bridge = createHapticsBridge(deps)
    bridge.onChime('turn')
    clock += BUZZ_DEBOUNCE_MS
    bridge.onChime('approval')
    await flush()
    expect(plugin.calls.filter((call) => call.url === STATUS_URL)).toHaveLength(1)
    expect(posted(plugin)).toEqual(['turn_done', 'needs_input'])
  })

  it('sends nothing to a port that is not the Kiro Crew plugin', async () => {
    plugin.answerStatus({ ok: true, body: { plugin: 'SomethingElse', api: 1 } })
    await chime(createHapticsBridge(deps), 'turn')
    plugin.answerStatus({ ok: true, body: { ...KIROCREW_STATUS, api: 2 } })
    await chime(createHapticsBridge(deps), 'turn')
    plugin.answerStatus({ ok: false })
    await chime(createHapticsBridge(deps), 'turn')
    plugin.answerStatus({ ok: true, body: 'not json' })
    await chime(createHapticsBridge(deps), 'turn')
    expect(posted(plugin)).toEqual([])
    expect(recentErrors()).toEqual([])
  })

  it('waits out the retry window after a failed probe, then finds a newly installed plugin', async () => {
    plugin.answerStatus(new TypeError('Failed to fetch'))
    const bridge = createHapticsBridge(deps)
    await chime(bridge, 'turn')
    await chime(bridge, 'turn')
    expect(plugin.calls.filter((call) => call.url === STATUS_URL)).toHaveLength(1)
    expect(recentErrors()).toEqual([])

    plugin.answerStatus({ ok: true, body: KIROCREW_STATUS })
    clock += DETECTION_RETRY_MS
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('forgets a plugin that stops answering, and probes again only after the retry window', async () => {
    const bridge = createHapticsBridge(deps)
    await chime(bridge, 'turn')
    plugin.answerEvents(new TypeError('Failed to fetch'))
    await chime(bridge, 'turn')
    plugin.answerEvents({ ok: true, body: { raised: 'turn_done' } })
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual(['turn_done', 'turn_done'])

    clock += DETECTION_RETRY_MS
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual(['turn_done', 'turn_done', 'turn_done'])
    expect(plugin.calls.filter((call) => call.url === STATUS_URL)).toHaveLength(2)
  })

  it('starts a full retry wait at a failed alert, however long ago the plugin was found', async () => {
    const bridge = createHapticsBridge(deps)
    await chime(bridge, 'turn')
    clock += DETECTION_RETRY_MS
    plugin.answerEvents(new TypeError('Failed to fetch'))
    await chime(bridge, 'turn')
    await chime(bridge, 'turn')
    expect(plugin.calls.filter((call) => call.url === STATUS_URL)).toHaveLength(1)
  })

  it('treats a refused alert like a missing plugin', async () => {
    const bridge = createHapticsBridge(deps)
    plugin.answerEvents({ ok: false })
    await chime(bridge, 'turn')
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('sends no credentials, no referrer and no body, and bypasses the HTTP cache', async () => {
    await chime(createHapticsBridge(deps), 'turn')
    for (const { init } of plugin.calls) {
      expect(init?.credentials).toBe('omit')
      expect(init?.referrerPolicy).toBe('no-referrer')
      expect(init?.body).toBeUndefined()
      expect(init?.cache).toBe('no-store')
    }
  })

  it('debounces alerts that arrive together, like the chime does', async () => {
    const bridge = createHapticsBridge(deps)
    bridge.onChime('turn')
    clock += BUZZ_DEBOUNCE_MS - 1
    bridge.onChime('approval')
    await flush()
    clock += 1
    bridge.onChime('approval')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done', 'needs_input'])
  })

  it('debounces alerts that arrive together even when storage refuses the shared record', async () => {
    const bridge = createHapticsBridge(deps)
    const denied = refuseStorageWrites()
    try {
      bridge.onChime('turn')
      clock += BUZZ_DEBOUNCE_MS - 1
      bridge.onChime('approval')
      await flush()
      expect(posted(plugin)).toEqual(['turn_done'])
    } finally {
      denied.mockRestore()
    }
  })

  it('a silenced chime does not hold back the next audible one', async () => {
    localStorage.setItem(SOUND_SETTINGS_KEY, JSON.stringify({ perCategory: { cron: 'none' } }))
    const bridge = createHapticsBridge(deps)
    bridge.onChime('cron')
    clock += 1
    bridge.onChime('turn')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('buzzes once per alert across open windows, and again for the next alert', async () => {
    const locks = makeLockManager()
    const windows = [createHapticsBridge({ ...deps, locks }), createHapticsBridge({ ...deps, locks })]
    windows[0].onChime('turn')
    await flush()
    clock += 50
    // The same alert, reaching a slower window later.
    windows[1].onChime('turn')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done'])

    clock += BUZZ_DEBOUNCE_MS
    for (const bridge of windows) bridge.onChime('approval')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done', 'needs_input'])
  })

  it('buzzes once for windows opened at different times, because the claim uses the wall clock they share', async () => {
    const locks = makeLockManager()
    // A window's monotonic clock starts at its own page load, so two windows can read it an hour apart.
    const olderWindow = createHapticsBridge({ ...deps, locks, now: () => clock + 3_600_000 })
    const newerWindow = createHapticsBridge({ ...deps, locks })
    olderWindow.onChime('turn')
    newerWindow.onChime('turn')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('still buzzes when the lock cannot be requested', async () => {
    const locks = { request: () => Promise.reject(new DOMException('denied', 'SecurityError')) } as unknown as LockManager
    await chime(createHapticsBridge({ ...deps, locks }), 'turn')
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('buzzes the next alert as soon as the debounce ends, because nothing but the claim holds the lock', async () => {
    // A throttled background window runs timers late, so anything a timer released could block every window.
    const locks = makeLockManager()
    const windows = [createHapticsBridge({ ...deps, locks }), createHapticsBridge({ ...deps, locks })]
    windows[0].onChime('turn')
    await flush()
    clock += BUZZ_DEBOUNCE_MS
    windows[1].onChime('approval')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done', 'needs_input'])
  })

  it('a buzz recorded by a clock ahead of this one does not silence the mouse', async () => {
    localStorage.setItem('mc-mouse-haptics-last-buzz-at', String(clock + 60_000))
    await chime(createHapticsBridge(deps), 'turn')
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('skips an alert this window heard just before another window recorded its buzz', async () => {
    const bridge = createHapticsBridge(deps)
    localStorage.setItem('mc-mouse-haptics-last-buzz-at', String(clock + 1000 + 100))
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual([])
  })

  it('still buzzes when storage refuses the shared record', async () => {
    const bridge = createHapticsBridge(deps)
    const denied = refuseStorageWrites()
    try {
      await chime(bridge, 'turn')
      await chime(bridge, 'approval')
      expect(denied).toHaveBeenCalled()
      expect(posted(plugin)).toEqual(['turn_done', 'needs_input'])
    } finally {
      denied.mockRestore()
    }
  })

  it('a window that cannot reach the plugin leaves the alert to one that can', async () => {
    const locks = makeLockManager()
    const unreachable = fakePlugin()
    unreachable.answerStatus(new TypeError('Failed to fetch'))
    const parkedWindow = createHapticsBridge({ ...deps, fetch: unreachable.fetch, locks })
    const readyWindow = createHapticsBridge({ ...deps, locks })
    parkedWindow.onChime('turn')
    readyWindow.onChime('turn')
    await flush()
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('makes no request at all while the Settings switch is off', async () => {
    const bridge = createHapticsBridge(deps)
    expect(saveMouseHapticsEnabled(false)).toBe(true)
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBe('0')
    await chime(bridge, 'turn')
    expect(plugin.calls).toEqual([])

    expect(saveMouseHapticsEnabled(true)).toBe(true)
    expect(localStorage.getItem(MOUSE_HAPTICS_ENABLED_KEY)).toBe('1')
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('makes no request at all until the switch has been turned on', async () => {
    localStorage.removeItem(MOUSE_HAPTICS_ENABLED_KEY)
    const bridge = createHapticsBridge(deps)
    await chime(bridge, 'turn')
    await chime(bridge, 'approval')
    expect(plugin.calls).toEqual([])
  })

  it('is off until the switch is turned on, and treats an unreadable value as off', () => {
    localStorage.removeItem(MOUSE_HAPTICS_ENABLED_KEY)
    expect(loadMouseHapticsEnabled()).toBe(false)
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, 'garbage')
    expect(loadMouseHapticsEnabled()).toBe(false)
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '0')
    expect(loadMouseHapticsEnabled()).toBe(false)
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '1')
    expect(loadMouseHapticsEnabled()).toBe(true)
  })

  it('asks the bridge to look again when a save of the switch lands, not when it is refused', () => {
    const heard = vi.fn()
    window.addEventListener(MC_MOUSE_HAPTICS_RECHECK_EVENT, heard)
    const denied = refuseStorageWrites()
    try {
      expect(saveMouseHapticsEnabled(true)).toBe(false)
      expect(heard).not.toHaveBeenCalled()
      denied.mockRestore()
      expect(saveMouseHapticsEnabled(true)).toBe(true)
      expect(heard).toHaveBeenCalledTimes(1)
    } finally {
      denied.mockRestore()
      window.removeEventListener(MC_MOUSE_HAPTICS_RECHECK_EVENT, heard)
    }
  })

  it('a recheck ends the retry wait, so a plugin installed meanwhile gets the next alert', async () => {
    plugin.answerStatus(new TypeError('Failed to fetch'))
    const bridge = createHapticsBridge(deps)
    await chime(bridge, 'turn')
    plugin.answerStatus({ ok: true, body: KIROCREW_STATUS })
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual([])

    bridge.recheck()
    await chime(bridge, 'turn')
    expect(posted(plugin)).toEqual(['turn_done'])
  })
})

describe('useMouseHaptics', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('accepts only loopback hostnames', () => {
    expect(['localhost', '127.0.0.1', '[::1]'].map(isLoopbackHostname)).toEqual([true, true, true])
    expect(['my-mac.tail1234.ts.net', '192.168.1.20', 'localhost.example'].map(isLoopbackHostname)).toEqual([false, false, false])
  })

  it('forwards a chime from a loopback page and stops when unmounted', async () => {
    localStorage.clear()
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '1')
    const plugin = fakePlugin()
    vi.stubGlobal('fetch', vi.fn(plugin.fetch))
    const { unmount } = renderHook(() => useMouseHaptics())
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'turn' } }))
    await flush()
    expect(posted(plugin)).toEqual(['turn_done'])

    unmount()
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'approval' } }))
    await flush()
    expect(posted(plugin)).toEqual(['turn_done'])
  })

  it('looks for the plugin again on the next alert when Settings asks it to', async () => {
    localStorage.clear()
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '1')
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const plugin = fakePlugin()
    plugin.answerStatus(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', vi.fn(plugin.fetch))
    const { unmount } = renderHook(() => useMouseHaptics())
    const alert = async () => {
      clock += 1000
      window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'turn' } }))
      await flush()
    }
    await alert()
    plugin.answerStatus({ ok: true, body: KIROCREW_STATUS })
    await alert()
    expect(posted(plugin)).toEqual([])

    window.dispatchEvent(new Event(MC_MOUSE_HAPTICS_RECHECK_EVENT))
    await alert()
    expect(posted(plugin)).toEqual(['turn_done'])
    unmount()
  })

  it('makes no request at all from a loopback page until the switch has been turned on', async () => {
    localStorage.clear()
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const { unmount } = renderHook(() => useMouseHaptics())
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'turn' } }))
    await flush()
    expect(fetchSpy).not.toHaveBeenCalled()
    unmount()
  })

  it('makes no request at all from a page that is not on loopback', async () => {
    localStorage.setItem(MOUSE_HAPTICS_ENABLED_KEY, '1')
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    const happyDom = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM
    const testPageUrl = window.location.href
    happyDom.setURL('http://my-mac.tail1234.ts.net:5476/')
    try {
      renderHook(() => useMouseHaptics())
      window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail: { kind: 'turn' } }))
      await flush()
      expect(fetchSpy).not.toHaveBeenCalled()
    } finally {
      happyDom.setURL(testPageUrl)
    }
  })
})
