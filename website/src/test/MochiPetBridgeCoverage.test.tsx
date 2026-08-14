/**
 * Mochi petBridge — the pet window's `api` object, split by transport.
 *
 * The module's whole reason for existing is that it has TWO regimes and the
 * difference between them is silent: inside the Electron pet window
 * `window.mochi` (the preload) is present and shell calls really happen, and in
 * a plain browser tab it is absent and every shell call must degrade to a no-op
 * instead of throwing. Which regime you are in is decided ONCE, at module
 * import, by a top-level `const shell = window.mochi` plus an
 * `if (shell !== undefined)` wiring block — so a test that only ever imports the
 * module one way can never see half of it.
 *
 * This file therefore loads the module twice through `loadBridge()`:
 * with a stub preload, and with none. It pins the shell-absent fallbacks
 * (`{}` / `null` / `false` / the origin), the shell-present forwards, the
 * once-per-name missing-method warning that exists because optional chaining
 * makes "never exposed" indistinguishable from "returned undefined", the
 * gateway event-bus handlers for moves and bubbles, the HTTP report/settings
 * reads, and the `instancesList` state mapping.
 *
 * The walk arithmetic itself is already pinned in
 * `src/apps/mochi/test/mochiWalkGeometry.test.ts` and the live appearance
 * payload in `mochiLiveAppearance.test.ts`; this file deliberately does not
 * duplicate either.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type Bridge = typeof import('../apps/mochi/pet/petBridge')
type AppEventListener = (payload: unknown) => void
type SettingsListener = (payload: unknown) => void
type ShellWindow = Window & { mochi?: Record<string, unknown> }

/** Event-bus subscriptions the module makes at import (`mochi:move`, `mochi:notify`). */
const appEvents = new Map<string, Set<AppEventListener>>()
/** The single settings-changed subscription every appearance event is built on. */
let settingsListener: SettingsListener | undefined
const reportStat = vi.fn()

vi.mock('../apps/mochi/panel/panelBridge', () => ({
  subscribeAppEvent: (type: string, cb: AppEventListener) => {
    const set = appEvents.get(type) ?? new Set<AppEventListener>()
    set.add(cb)
    appEvents.set(type, set)
    return () => {
      set.delete(cb)
    }
  },
  onColorMapChanged: (cb: SettingsListener) => {
    settingsListener = cb
    return () => {
      settingsListener = undefined
    }
  },
  reportStat: (...args: unknown[]) => reportStat(...args),
  // Re-exported straight through by petBridge; present so the export list resolves.
  disableApp: vi.fn(),
  getWatchlist: vi.fn(),
  getPinnedFiles: vi.fn(),
  markPinnedSeen: vi.fn(),
  unpinFile: vi.fn(),
  setWatchItemStatus: vi.fn(),
  updateWatchItem: vi.fn(),
  onWatchlistChanged: () => () => {},
  getPetState: vi.fn(),
  getPetStateInfo: vi.fn(),
  onStateChange: () => () => {},
  onMood: () => () => {},
  onGalleryPacksChanged: () => () => {},
  onNotification: () => () => {},
  galleryGetPackDetail: vi.fn(),
  galleryPackFileUrl: () => '',
  presetsGetColorMap: vi.fn(),
}))

let fetchMock: ReturnType<typeof vi.fn>

/** Replace `fetch` wholesale; the module only ever posts/reads same-origin JSON. */
function setFetch(impl: (...args: unknown[]) => unknown): void {
  fetchMock = vi.fn(impl)
  vi.stubGlobal('fetch', fetchMock)
}

/** A settings read the module's `getMochiConfig` will accept. */
function settingsResponse(cfg: unknown): unknown {
  return { ok: true, json: async () => cfg }
}

interface ShellCallbacks {
  displaysInfo?: (displays: unknown[], myId: number, activeId?: number) => void
  setActive?: (active: boolean, x?: number, y?: number) => void
  dragEnded?: (x: number, y: number) => void
}

/**
 * A stand-in for the pet preload.
 *
 * `onHide` and `onPlayMotion` are deliberately absent: a shell that is present
 * but missing a method is the real wiring gap the module's dev warning reports,
 * and there has to be one to point at.
 */
function makeShell(extra: Record<string, unknown> = {}): {
  shell: Record<string, unknown>
  cbs: ShellCallbacks
} {
  const cbs: ShellCallbacks = {}
  const shell: Record<string, unknown> = {
    onDisplaysInfo: (cb: ShellCallbacks['displaysInfo']) => {
      cbs.displaysInfo = cb
    },
    onSetActive: (cb: ShellCallbacks['setActive']) => {
      cbs.setActive = cb
    },
    onDragEnded: (cb: ShellCallbacks['dragEnded']) => {
      cbs.dragEnded = cb
    },
    ...extra,
  }
  return { shell, cbs }
}

/**
 * Import the module fresh under a chosen regime.
 *
 * `vi.resetModules()` is what makes the top-level `const shell = window.mochi`
 * re-evaluate — without it the FIRST import in the file would decide the regime
 * for every test in it.
 */
async function loadBridge(shell?: Record<string, unknown>): Promise<Bridge> {
  vi.resetModules()
  appEvents.clear()
  settingsListener = undefined
  const w = window as unknown as ShellWindow
  if (shell === undefined) delete w.mochi
  else w.mochi = shell
  return (await import('../apps/mochi/pet/petBridge')) as Bridge
}

/** Publish one runtime event the way the queue poller does. */
function emitAppEvent(type: string, payload: unknown): void {
  for (const cb of appEvents.get(type) ?? []) cb(payload)
}

/** Let the module's async handlers (settings reads, dynamic imports) settle. */
async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  reportStat.mockClear()
  setFetch(async () => ({ ok: true, json: async () => ({}) }))
})

afterEach(() => {
  delete (window as unknown as ShellWindow).mochi
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('regime detection', () => {
  it('reports no shell in a plain browser tab', async () => {
    const bridge = await loadBridge()
    expect(bridge.hasShell).toBe(false)
  })

  it('reports a shell when the pet preload is present', async () => {
    const bridge = await loadBridge(makeShell().shell)
    expect(bridge.hasShell).toBe(true)
  })
})

describe('shell forwards with no shell at all', () => {
  it('swallows a fire-and-forget call instead of throwing', async () => {
    const bridge = await loadBridge()
    expect(bridge.updateHitbox({ x: 0, y: 0, width: 1, height: 1 })).toBeUndefined()
    expect(bridge.savePosition(1, 2)).toBeUndefined()
    expect(bridge.dragStart()).toBeUndefined()
    expect(bridge.dragEnd()).toBeUndefined()
    expect(bridge.dragMouseup()).toBeUndefined()
    expect(bridge.onHide(() => {})).toBeUndefined()
    expect(bridge.onDragUpdate(() => {})).toBeUndefined()
    expect(bridge.onDragEnded(() => {})).toBeUndefined()
    expect(bridge.onDragListenMouseup(() => {})).toBeUndefined()
    expect(bridge.onPlayMotion(() => {})).toBeUndefined()
    expect(bridge.onBubbleForceDismiss(() => {})).toBeUndefined()
    expect(bridge.openChat()).toBeUndefined()
    expect(bridge.openAvatars()).toBeUndefined()
    expect(bridge.openMemories()).toBeUndefined()
    expect(bridge.openSettings()).toBeUndefined()
    expect(bridge.clearScreenInPanel()).toBeUndefined()
    expect(bridge.deleteHistoryInPanel()).toBeUndefined()
  })

  it('stays silent — a missing shell is normal outside Electron', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bridge = await loadBridge()
    bridge.openChat()
    bridge.openSettings()
    expect(warn).not.toHaveBeenCalled()
  })

  it('falls back to the origin for the window position', async () => {
    const bridge = await loadBridge()
    await expect(bridge.getWindowPosition()).resolves.toEqual({ x: 0, y: 0 })
  })
})

describe('shell forwards with a preload present', () => {
  it('passes the arguments through and returns the shell result', async () => {
    const updateHitbox = vi.fn(() => 'ok')
    const bridge = await loadBridge(makeShell({ updateHitbox }).shell)
    const box = { x: 1, y: 2, width: 3, height: 4 }
    expect(bridge.updateHitbox(box)).toBe('ok')
    expect(updateHitbox).toHaveBeenCalledWith(box)
  })

  it('awaits an async forward', async () => {
    const getWindowPosition = vi.fn(async () => ({ x: 5, y: 6 }))
    const bridge = await loadBridge(makeShell({ getWindowPosition }).shell)
    await expect(bridge.getWindowPosition()).resolves.toEqual({ x: 5, y: 6 })
  })

  it('warns once per missing method — the wiring gap optional chaining hides', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bridge = await loadBridge(makeShell().shell)

    bridge.onHide(() => {})
    bridge.onHide(() => {})
    bridge.onHide(() => {})

    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toContain('onHide')
    expect(warn.mock.calls[0][0]).toContain('pet-preload.js')
  })

  it('warns separately for a second missing method', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bridge = await loadBridge(makeShell().shell)

    bridge.openChat()
    bridge.openAvatars()

    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('warns for a missing async forward and still resolves undefined', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bridge = await loadBridge(makeShell().shell)

    await expect(bridge.getWindowPosition()).resolves.toEqual({ x: 0, y: 0 })
    expect(warn).toHaveBeenCalledTimes(1)
  })
})

describe('import-time shell wiring', () => {
  it('adopts the work area of the display it is on and reports the list', async () => {
    const { shell, cbs } = makeShell()
    const bridge = await loadBridge(shell)
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setLastPosForTest({ x: 0, y: 0 })

    cbs.displaysInfo?.(
      [
        { id: 1, workArea: { width: 900, height: 700 } },
        { id: 2, workArea: { width: 100, height: 100 } },
      ],
      1,
      2,
    )

    // Every overlay posts the SHELL's active id, never its own — a last-writer
    // -wins cache is only correct because all of them post the same answer.
    const [url, init] = fetchMock.mock.calls[0] as [string, { body: string }]
    expect(url).toBe('/api/apps/mochi/displays')
    expect(JSON.parse(init.body).activeId).toBe(2)

    // The adopted work area is what the clamp now uses.
    bridge.handleMove({ x: 99999, y: 99999 })
    expect(walks).toEqual([[900 - 128, 700 - 128 - 140]])
  })

  it('falls back to its own display id when the shell names no active one', async () => {
    const { shell, cbs } = makeShell()
    await loadBridge(shell)

    cbs.displaysInfo?.([{ id: 7, workArea: { width: 800, height: 600 } }], 7)

    const [, init] = fetchMock.mock.calls[0] as [string, { body: string }]
    expect(JSON.parse(init.body).activeId).toBe(7)
  })

  it('keeps the previous work area when its own display is not in the list', async () => {
    const { shell, cbs } = makeShell()
    const bridge = await loadBridge(shell)
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest({ width: 500, height: 500 })
    bridge._setLastPosForTest({ x: 0, y: 0 })

    cbs.displaysInfo?.([{ id: 9 }], 3)

    bridge.handleMove({ x: 99999, y: 99999 })
    expect(walks).toEqual([[500 - 128, 500 - 128 - 140]])
  })

  it('survives a failed displays report — the pet must keep rendering', async () => {
    setFetch(async () => {
      throw new Error('offline')
    })
    const { shell, cbs } = makeShell()
    await loadBridge(shell)

    cbs.displaysInfo?.([{ id: 1, workArea: { width: 800, height: 600 } }], 1)
    await flush()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('records the position the shell reports when the pet is activated', async () => {
    const { shell, cbs } = makeShell()
    const bridge = await loadBridge(shell)
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })

    cbs.setActive?.(true, 400, 300)

    // Inside the 20px dead-band of the position just reported.
    bridge.handleMove({ x: 405, y: 305 })
    expect(walks).toEqual([])
  })

  it('ignores a deactivation and a position-less activation', async () => {
    const { shell, cbs } = makeShell()
    const bridge = await loadBridge(shell)
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })
    bridge._setLastPosForTest({ x: 0, y: 0 })

    cbs.setActive?.(false, 400, 300)
    cbs.setActive?.(true)

    // Still at the origin, so a move to 400/300 is well outside the dead-band.
    bridge.handleMove({ x: 400, y: 300 })
    expect(walks).toEqual([[400, 300]])
  })

  it('adopts the drop position and counts the drag as a stat', async () => {
    const { shell, cbs } = makeShell()
    const bridge = await loadBridge(shell)
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })

    cbs.dragEnded?.(200, 100)

    expect(reportStat).toHaveBeenCalledWith('drag')
    bridge.handleMove({ x: 205, y: 105 })
    expect(walks).toEqual([])
  })
})

describe('moving to another display', () => {
  it('centres the pet on the target monitor when no point is given', async () => {
    const transferToDisplay = vi.fn()
    const bridge = await loadBridge(makeShell({ transferToDisplay }).shell)
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })

    bridge.handleMove({ display: 3 })

    expect(transferToDisplay).toHaveBeenCalledWith(3, 1000 / 2 - 64, 800 / 2 - 64)
  })

  it('honours an explicit point on the target monitor and walks no further', async () => {
    const transferToDisplay = vi.fn()
    const bridge = await loadBridge(makeShell({ transferToDisplay }).shell)
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })
    bridge._setLastPosForTest({ x: 0, y: 0 })

    bridge.handleMove({ display: 3, x: 10, y: 20 })

    expect(transferToDisplay).toHaveBeenCalledWith(3, 10, 20)
    // The transfer already placed it; a same-frame walk would fight the move.
    expect(walks).toEqual([])
  })

  it('degrades to a no-op in a browser tab, which has no second monitor', async () => {
    const bridge = await loadBridge()
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })
    expect(() => bridge.handleMove({ display: 3 })).not.toThrow()
  })
})

describe('the runtime event bus', () => {
  it('walks on a published move', async () => {
    const bridge = await loadBridge()
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })
    bridge._setLastPosForTest({ x: 0, y: 0 })

    emitAppEvent('mochi:move', { x: 300, y: 200 })

    expect(walks).toEqual([[300, 200]])
  })

  it('treats a payload-less move frame as a query', async () => {
    const bridge = await loadBridge()
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))

    emitAppEvent('mochi:move', undefined)

    expect(walks).toEqual([])
  })

  it('shows a bubble for a notify frame, carrying the sticky flag', async () => {
    const bridge = await loadBridge()
    const bubbles: [string, boolean][] = []
    bridge.onBubble((text, sticky) => bubbles.push([text, sticky]))

    emitAppEvent('mochi:notify', { summary: 'build is green', sticky: true })
    emitAppEvent('mochi:notify', { summary: 'and again' })

    expect(bubbles).toEqual([
      ['build is green', true],
      ['and again', false],
    ])
  })

  it('does not flash an empty bubble', async () => {
    const bridge = await loadBridge()
    const bubbles: string[] = []
    bridge.onBubble((text) => bubbles.push(text))

    emitAppEvent('mochi:notify', undefined)
    emitAppEvent('mochi:notify', { summary: '' })
    emitAppEvent('mochi:notify', { summary: 42 })

    expect(bubbles).toEqual([])
  })

  it('stops delivering to an unsubscribed listener', async () => {
    const bridge = await loadBridge()
    const bubbles: string[] = []
    const off = bridge.onBubble((text) => bubbles.push(text))

    off()
    emitAppEvent('mochi:notify', { summary: 'nobody home' })

    expect(bubbles).toEqual([])
  })

  it('dismissing a bubble is renderer-local, so it posts nothing', async () => {
    const bridge = await loadBridge()
    bridge.dismissBubble()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('backend reports', () => {
  it('posts a finished walk', async () => {
    const bridge = await loadBridge()
    bridge.walkDone()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/apps/mochi/walk-done',
      expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
    )
  })

  it('posts the peeking state both ways', async () => {
    const bridge = await loadBridge()
    bridge.setPeeking(true)
    bridge.setPeeking(false)
    const bodies = fetchMock.mock.calls.map((c) => JSON.parse((c[1] as { body: string }).body))
    expect(bodies).toEqual([{ peeking: true }, { peeking: false }])
  })

  it('swallows a failed report rather than surfacing a rejection', async () => {
    setFetch(async () => {
      throw new Error('gateway down')
    })
    const bridge = await loadBridge()

    bridge.walkDone()
    await flush()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('settings and pack reads', () => {
  it('returns the parsed settings', async () => {
    setFetch(async () => settingsResponse({ activeAppearance: 'p1', catPreset: null }))
    const bridge = await loadBridge()
    await expect(bridge.getMochiConfig()).resolves.toEqual({
      activeAppearance: 'p1',
      catPreset: null,
    })
  })

  it('returns undefined on a non-OK settings read', async () => {
    setFetch(async () => ({ ok: false, json: async () => ({}) }))
    const bridge = await loadBridge()
    await expect(bridge.getMochiConfig()).resolves.toBeUndefined()
  })

  it('returns undefined when the settings read throws', async () => {
    setFetch(async () => {
      throw new Error('no network')
    })
    const bridge = await loadBridge()
    await expect(bridge.getMochiConfig()).resolves.toBeUndefined()
  })

  it('reads a pack detail from the packs route, encoding the id', async () => {
    setFetch(async () => ({ ok: true, json: async () => ({ meta: { id: 'a b' } }) }))
    const bridge = await loadBridge()

    await expect(bridge.galleryGetPackDetail('a b')).resolves.toEqual({ meta: { id: 'a b' } })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/apps/mochi/packs/a%20b',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
  })

  it('returns undefined for a pack the route does not have', async () => {
    setFetch(async () => ({ ok: false, json: async () => ({}) }))
    const bridge = await loadBridge()
    await expect(bridge.galleryGetPackDetail('gone')).resolves.toBeUndefined()
  })

  it('returns undefined when the pack read throws', async () => {
    setFetch(async () => {
      throw new Error('no network')
    })
    const bridge = await loadBridge()
    await expect(bridge.galleryGetPackDetail('p1')).resolves.toBeUndefined()
  })

  it('builds a pack file URL that is usable as an image source', async () => {
    const bridge = await loadBridge()
    expect(bridge.galleryPackFileUrl('my pack', 'idle frame.png')).toBe(
      '/api/apps/mochi/packs/my%20pack/file/idle%20frame.png',
    )
  })
})

describe('appearance subscriptions', () => {
  it('hands the new settings to a config listener', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    const off = bridge.onConfigUpdated((cfg) => seen.push(cfg))

    settingsListener?.({ activeAppearance: 'p1' })
    settingsListener?.(undefined)
    off()

    expect(seen).toEqual([{ activeAppearance: 'p1' }, {}])
  })

  it('is a real no-op subscription for the retired theme event', async () => {
    const bridge = await loadBridge()
    const off = bridge.onThemeChanged(() => {})
    expect(() => off()).not.toThrow()
  })

  it('reshapes a settings frame into the packId plus colour map the renderer wants', async () => {
    setFetch(async () =>
      settingsResponse({
        activeAppearance: 'p9',
        catPreset: null,
        colorMaps: { p9: { '#F9A85F': '#123456' } },
      }),
    )
    const bridge = await loadBridge()
    const seen: { packId: string; colorMap: Record<string, string> }[] = []
    const off = bridge.onColorMapChanged((data) => seen.push(data))

    settingsListener?.({ activeAppearance: 'p9' })
    await flush()
    off()

    expect(seen).toEqual([{ packId: 'p9', colorMap: { '#F9A85F': '#123456' } }])
  })

  it('reports an empty colour map rather than undefined when there is none', async () => {
    setFetch(async () => ({ ok: false, json: async () => ({}) }))
    const bridge = await loadBridge()
    const seen: { packId: string; colorMap: Record<string, string> }[] = []
    const off = bridge.onColorMapChanged((data) => seen.push(data))

    settingsListener?.({})
    await flush()
    off()

    // An empty appearance resolves to the built-in cat, not to a blank id.
    expect(seen).toEqual([{ packId: 'default-mochi', colorMap: {} }])
  })

  it('treats a recolour as no pack switch at all', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    const off = bridge.onGalleryActiveChanged((data) => seen.push(data))

    settingsListener?.({ activeAppearance: 'default-mochi' })
    await flush()
    settingsListener?.({ activeAppearance: 'default-mochi' })
    await flush()
    off()

    expect(seen).toEqual([{ packId: 'default-mochi' }])
  })

  it('drops a settings frame that arrives after teardown', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    const off = bridge.onGalleryActiveChanged((data) => seen.push(data))
    // Hold the socket callback so a frame already in flight when the pet tore
    // down can still be delivered — that race is what the `stopped` flag is for.
    const inFlight = settingsListener
    off()

    inFlight?.({ activeAppearance: 'default-mochi' })
    await flush()

    expect(seen).toEqual([])
  })
})

describe('the cat colourway', () => {
  it('prefers the per-pack map the colour customiser wrote', async () => {
    setFetch(async () =>
      settingsResponse({
        activeAppearance: 'default-mochi',
        catPreset: 'tuxedo',
        colorMaps: { p3: { '#F9A85F': '#AAAAAA' } },
      }),
    )
    const bridge = await loadBridge()
    await expect(bridge.presetsGetColorMap('p3')).resolves.toEqual({ '#F9A85F': '#AAAAAA' })
  })

  it('falls back to the active pack when no id is named', async () => {
    setFetch(async () =>
      settingsResponse({
        activeAppearance: 'p4',
        catPreset: null,
        colorMaps: { p4: { '#F9A85F': '#BBBBBB' } },
      }),
    )
    const bridge = await loadBridge()
    await expect(bridge.presetsGetColorMap()).resolves.toEqual({ '#F9A85F': '#BBBBBB' })
  })

  it('resolves the built-in coat the user picked when no map is stored', async () => {
    setFetch(async () =>
      settingsResponse({ activeAppearance: 'default-mochi', catPreset: 'tuxedo', colorMaps: {} }),
    )
    const bridge = await loadBridge()

    const map = await bridge.presetsGetColorMap('default-mochi')

    expect(map?.['#F9A85F']).toBe('#2C2C2C')
  })

  it('returns undefined when no coat is picked', async () => {
    setFetch(async () =>
      settingsResponse({ activeAppearance: 'default-mochi', catPreset: null, colorMaps: {} }),
    )
    const bridge = await loadBridge()
    await expect(bridge.presetsGetColorMap('default-mochi')).resolves.toBeUndefined()
  })

  it('returns undefined for a coat id that no longer exists', async () => {
    setFetch(async () =>
      settingsResponse({ activeAppearance: 'default-mochi', catPreset: 'legacy-default' }),
    )
    const bridge = await loadBridge()
    await expect(bridge.presetsGetColorMap('default-mochi')).resolves.toBeUndefined()
  })

  it('returns undefined when the settings read fails entirely', async () => {
    setFetch(async () => ({ ok: false, json: async () => ({}) }))
    const bridge = await loadBridge()
    await expect(bridge.presetsGetColorMap()).resolves.toBeUndefined()
  })
})

describe('global accelerators', () => {
  it('reports nothing accepted in a browser tab, where they are not editable', async () => {
    const bridge = await loadBridge()
    await expect(bridge.applyShortcuts({ toggle: 'Alt+M' })).resolves.toEqual({})
  })

  it('returns what the OS accepted, per accelerator', async () => {
    const applyShortcuts = vi.fn(async () => ({ toggle: true, chat: false }))
    const bridge = await loadBridge(makeShell({ applyShortcuts }).shell)

    await expect(bridge.applyShortcuts({ toggle: 'Alt+M', chat: 'Alt+C' })).resolves.toEqual({
      toggle: true,
      chat: false,
    })
    expect(applyShortcuts).toHaveBeenCalledWith({ toggle: 'Alt+M', chat: 'Alt+C' })
  })

  it('treats a non-answer from the shell as nothing accepted', async () => {
    const bridge = await loadBridge(makeShell({ applyShortcuts: async () => undefined }).shell)
    await expect(bridge.applyShortcuts({ toggle: 'Alt+M' })).resolves.toEqual({})
  })

  it('treats a failed register as nothing accepted', async () => {
    const bridge = await loadBridge(
      makeShell({
        applyShortcuts: async () => {
          throw new Error('registration refused')
        },
      }).shell,
    )
    await expect(bridge.applyShortcuts({ toggle: 'Alt+M' })).resolves.toEqual({})
  })
})

describe('per-machine preferences', () => {
  it('returns null with no shell, telling the caller to use the gateway copy', async () => {
    const bridge = await loadBridge()
    await expect(bridge.machinePrefs()).resolves.toBeNull()
  })

  it('returns this computer\u2019s own copy, not the gateway\u2019s', async () => {
    const prefs = { petInstance: 'host', shortcuts: { toggle: 'Alt+M' } }
    const bridge = await loadBridge(makeShell({ machinePrefs: async () => prefs }).shell)
    await expect(bridge.machinePrefs()).resolves.toEqual(prefs)
  })

  it('returns null for an empty answer', async () => {
    const bridge = await loadBridge(makeShell({ machinePrefs: async () => undefined }).shell)
    await expect(bridge.machinePrefs()).resolves.toBeNull()
  })

  it('returns null when the shell read throws', async () => {
    const bridge = await loadBridge(
      makeShell({
        machinePrefs: async () => {
          throw new Error('store unreadable')
        },
      }).shell,
    )
    await expect(bridge.machinePrefs()).resolves.toBeNull()
  })
})

describe('pointing the pet at an instance', () => {
  it('cannot repoint from a browser tab', async () => {
    const bridge = await loadBridge()
    await expect(bridge.setPetInstance('other')).resolves.toBe(false)
  })

  it('is true only when the shell confirms it', async () => {
    const setPetInstance = vi.fn(async () => ({ ok: true }))
    const bridge = await loadBridge(makeShell({ setPetInstance }).shell)

    await expect(bridge.setPetInstance('other')).resolves.toBe(true)
    expect(setPetInstance).toHaveBeenCalledWith('other')
  })

  it('is false when the shell declines or answers nothing', async () => {
    const bridge = await loadBridge(makeShell({ setPetInstance: async () => ({ ok: false }) }).shell)
    await expect(bridge.setPetInstance('other')).resolves.toBe(false)

    const bridge2 = await loadBridge(makeShell({ setPetInstance: async () => undefined }).shell)
    await expect(bridge2.setPetInstance('other')).resolves.toBe(false)
  })

  it('is false when the shell call throws', async () => {
    const bridge = await loadBridge(
      makeShell({
        setPetInstance: async () => {
          throw new Error('no such instance')
        },
      }).shell,
    )
    await expect(bridge.setPetInstance('other')).resolves.toBe(false)
  })
})

describe('the instance list', () => {
  const instances = [{ id: 'host', label: 'This computer' }]

  it('returns null with no shell so the caller falls back to same-origin', async () => {
    const bridge = await loadBridge()
    await expect(bridge.instancesList()).resolves.toBeNull()
  })

  it('keeps the disabled state, which used to be erased into an empty list', async () => {
    const bridge = await loadBridge(
      makeShell({ instancesList: async () => ({ state: 'disabled', instances }) }).shell,
    )
    await expect(bridge.instancesList()).resolves.toEqual({ state: 'disabled' })
  })

  it('keeps the inactive state and its instances', async () => {
    const bridge = await loadBridge(
      makeShell({ instancesList: async () => ({ state: 'inactive', instances }) }).shell,
    )
    await expect(bridge.instancesList()).resolves.toEqual({ state: 'inactive', instances })
  })

  it('passes a ready list straight through', async () => {
    const bridge = await loadBridge(
      makeShell({ instancesList: async () => ({ state: 'ready', instances }) }).shell,
    )
    await expect(bridge.instancesList()).resolves.toEqual({ state: 'ready', instances })
  })

  it('defaults a ready list with no instances to an empty array', async () => {
    const bridge = await loadBridge(
      makeShell({ instancesList: async () => ({ state: 'ready' }) }).shell,
    )
    await expect(bridge.instancesList()).resolves.toEqual({ state: 'ready', instances: [] })
  })

  it('calls an unrecognised state an error, not an empty list', async () => {
    const bridge = await loadBridge(
      makeShell({ instancesList: async () => ({ state: 'something-new' }) }).shell,
    )
    await expect(bridge.instancesList()).resolves.toEqual({ state: 'error' })
  })

  it('returns null for an empty answer', async () => {
    const bridge = await loadBridge(
      makeShell({ instancesList: async () => undefined }).shell,
    )
    await expect(bridge.instancesList()).resolves.toBeNull()
  })

  it('returns null when the shell call throws', async () => {
    const bridge = await loadBridge(
      makeShell({
        instancesList: async () => {
          throw new Error('core unreachable')
        },
      }).shell,
    )
    await expect(bridge.instancesList()).resolves.toBeNull()
  })
})

describe('walk listener bookkeeping', () => {
  it('unsubscribes every walk channel', async () => {
    const bridge = await loadBridge()
    const seen: string[] = []
    const offWalk = bridge.onWalk(() => seen.push('walk'))
    const offPath = bridge.onWalkPath(() => seen.push('path'))
    const offAppend = bridge.onWalkAppend(() => seen.push('append'))
    const offCancel = bridge.onWalkCancel(() => seen.push('cancel'))
    bridge._setWorkAreaForTest({ width: 1000, height: 800 })
    bridge._setLastPosForTest({ x: 0, y: 0 })

    offWalk()
    offPath()
    offAppend()
    offCancel()

    bridge.handleMove({ x: 400, y: 300 })
    bridge.handleMove({ waypoints: [{ x: 10, y: 10 }] })
    bridge.handleMove({ waypoints: [{ x: 20, y: 20 }], interrupt: false })

    expect(seen).toEqual([])
  })

  it('clears an injected work area, falling back to the viewport', async () => {
    const bridge = await loadBridge()
    const walks: [number, number][] = []
    bridge.onWalk((x, y) => walks.push([x, y]))
    bridge._setWorkAreaForTest(null)
    bridge._setLastPosForTest({ x: 0, y: 0 })

    bridge.handleMove({ x: 99999, y: 99999 })

    expect(walks).toEqual([
      [window.innerWidth - 128, Math.max(0, window.innerHeight - 128 - 140)],
    ])
  })
})
