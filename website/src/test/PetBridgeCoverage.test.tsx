// The parts of the pet bridge that the existing suites never reach: the browser
// file pickers, the read helpers' failure arms, the coalesced position write, the
// listener fan-out and the context-menu actions.
//
// The bridge is the whole boundary between the Crew Companion pages and the
// gateway, and most of its methods have a refusal arm that is only visible when
// the network says no. Those arms are where the interesting regressions live —
// a refusal read back as a success discards the user's artwork — so they are
// exercised here alongside the happy paths.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { petBridge } from '../apps/crew-companion/petBridge'
import {
  APPEARANCE_COLOURS_PATH,
  APPEARANCE_DELETE_PATH,
  APPEARANCE_DETAIL_PATH,
  APPEARANCE_EXPORT_PATH,
  APPEARANCE_IMPORT_PATH,
  APPEARANCE_SAVE_PATH,
  APPEARANCES_PATH,
  CONFIG_PATH,
  PETDEX_FETCH_PATH,
  REMINDERS_PATH,
  REMOVE_PATH,
} from '../apps/crew-companion/constants'

/** One answer for one request: a status, a body, or a transport failure. */
interface Reply {
  ok?: boolean
  body?: unknown
  /** Reject instead of answering — the "gateway is gone" case. */
  fails?: boolean
  /** Answer with a body that is not JSON, so `r.json()` throws. */
  badJson?: boolean
}

interface Call {
  url: string
  method: string
  body: unknown
}

/**
 * A fetch double that routes by URL.
 *
 * The bridge reads `r.ok` (postJson), `r.json()` (getJson / postForJson) or both,
 * so every reply exposes both and the route decides which arm the caller lands in.
 */
function stubFetch(route: (url: string) => Reply) {
  const calls: Call[] = []
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const raw = init?.body
    calls.push({
      url,
      method: init?.method ?? 'GET',
      body: typeof raw === 'string' ? JSON.parse(raw) : undefined,
    })
    const reply = route(url)
    if (reply.fails) throw new Error('network down')
    return {
      ok: reply.ok ?? true,
      json: async () => {
        if (reply.badJson) throw new Error('not json')
        return reply.body ?? {}
      },
    } as unknown as Response
  })
  vi.stubGlobal('fetch', fn)
  return { fn, calls }
}

/** Every request answers the same way. */
function stubFetchAll(reply: Reply) {
  return stubFetch(() => reply)
}

/**
 * Answer the browser file dialog.
 *
 * `pickJsonFile` / `pickImageFile` / `pickArtFile` build a hidden <input type=file>,
 * attach their listeners and click it. Intercepting the click is what lets a test
 * hand back a chosen file — or a cancel — without a real dialog.
 */
function armPicker(file: File | null) {
  return vi
    .spyOn(HTMLInputElement.prototype, 'click')
    .mockImplementation(function mockClick(this: HTMLInputElement) {
      if (file) {
        Object.defineProperty(this, 'files', { value: [file], configurable: true })
        this.dispatchEvent(new Event('change'))
      } else {
        this.dispatchEvent(new Event('cancel'))
      }
    })
}

interface PreloadStub {
  appearanceChanged: ReturnType<typeof vi.fn>
  onAppearanceChanged: ReturnType<typeof vi.fn>
  contextMenuAction: ReturnType<typeof vi.fn>
  galleryOpen: ReturnType<typeof vi.fn>
  galleryClose: ReturnType<typeof vi.fn>
  openExternal: ReturnType<typeof vi.fn>
  updateHitbox: ReturnType<typeof vi.fn>
  setMenuHitbox: ReturnType<typeof vi.fn>
}

/** Install the preload bridge the desktop windows have and a browser tab does not. */
function stubPreload(offBridge = vi.fn()): PreloadStub {
  const bridge: PreloadStub = {
    appearanceChanged: vi.fn(),
    onAppearanceChanged: vi.fn(() => offBridge),
    contextMenuAction: vi.fn(),
    galleryOpen: vi.fn(),
    galleryClose: vi.fn(),
    openExternal: vi.fn(),
    updateHitbox: vi.fn(),
    setMenuHitbox: vi.fn(),
  }
  ;(window as unknown as { crewCompanion?: unknown }).crewCompanion = bridge
  return bridge
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  delete (window as unknown as { crewCompanion?: unknown }).crewCompanion
})

describe('getWindowPosition', () => {
  it('reads petX/petY off the FLAT snapshot', async () => {
    const { calls } = stubFetchAll({ body: { petX: 120, petY: 340 } })
    expect(await petBridge.getWindowPosition!()).toEqual({ x: 120, y: 340 })
    expect(calls[0].url).toBe(REMINDERS_PATH)
  })

  it('reports "never moved" as null so the renderer keeps its own placement', async () => {
    stubFetchAll({ body: { petX: null, petY: null } })
    expect(await petBridge.getWindowPosition!()).toBeNull()
  })

  it('reports null when only one axis was stored', async () => {
    stubFetchAll({ body: { petX: 10 } })
    expect(await petBridge.getWindowPosition!()).toBeNull()
  })

  it('reports null on a non-2xx snapshot', async () => {
    stubFetchAll({ ok: false })
    expect(await petBridge.getWindowPosition!()).toBeNull()
  })

  it('reports null when the gateway is unreachable', async () => {
    stubFetchAll({ fails: true })
    expect(await petBridge.getWindowPosition!()).toBeNull()
  })
})

describe('savePosition coalescing', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    // Drain the pending write so the module's timer handle is back to null for
    // the next test — otherwise a later savePosition would never schedule.
    vi.advanceTimersByTime(500)
    vi.useRealTimers()
  })

  it('writes ONCE for a burst, keeping the last position and rounding both axes', () => {
    const { fn, calls } = stubFetchAll({ body: { ok: true } })
    petBridge.savePosition!(10.4, 20.6)
    petBridge.savePosition!(30.2, 40.8)
    // Nothing goes out until the coalescing window closes.
    expect(fn).not.toHaveBeenCalled()

    vi.advanceTimersByTime(250)

    expect(fn).toHaveBeenCalledOnce()
    expect(calls[0].url).toBe(CONFIG_PATH)
    expect(calls[0].method).toBe('POST')
    // Both axes together: the backend refuses half a position.
    expect(calls[0].body).toEqual({ petX: 30, petY: 41 })
  })

  it('a failed write does not wedge the coalescing timer', async () => {
    // Renamed and re-pointed. The old body was `expect(true).toBe(true)` behind a
    // comment claiming "no unhandled rejection escapes", which asserted nothing —
    // it passed whether or not `flushSave` kept its `.catch()`. Asserting the
    // rejection directly does not close that gap either: MEASURED, deleting the
    // `.catch()` still leaves all 68 tests green, because the rejection surfaces
    // as a process-level event that vitest attributes to no single test.
    //
    // So this pins the consequence that IS observable from here, and is the one
    // that would actually break the feature: a failed save must leave the module's
    // timer handle clear, or the companion's position silently stops persisting
    // for the rest of the session after one network blip.
    const { fn } = stubFetchAll({ fails: true })
    petBridge.savePosition!(1, 2)
    vi.advanceTimersByTime(250)
    expect(fn).toHaveBeenCalledOnce()
    await Promise.resolve()

    petBridge.savePosition!(3, 4)
    vi.advanceTimersByTime(250)
    expect(fn).toHaveBeenCalledTimes(2)
  })
})

describe('config and export reads', () => {
  it('getCrewCompanionConfig hands back the snapshot verbatim', async () => {
    stubFetchAll({ body: { activeAppearance: 'ghost', petX: 4 } })
    expect(await petBridge.getCrewCompanionConfig!()).toEqual({ activeAppearance: 'ghost', petX: 4 })
  })

  it('getCrewCompanionConfig reports null when the body is not JSON', async () => {
    stubFetchAll({ badJson: true })
    expect(await petBridge.getCrewCompanionConfig!()).toBeNull()
  })

  it('galleryExport encodes the pack id into the query', async () => {
    const { calls } = stubFetchAll({ body: { bundle: 1 } })
    expect(await petBridge.galleryExport!('a b/c')).toEqual({ bundle: 1 })
    expect(calls[0].url).toBe(`${APPEARANCE_EXPORT_PATH}?id=a%20b%2Fc`)
  })

  it('galleryGetPackDetail asks for the detail route with the id as a query param', async () => {
    const { calls } = stubFetchAll({ body: { animations: {} } })
    await petBridge.galleryGetPackDetail!('pack/one')
    expect(calls[0].url).toBe(`${APPEARANCE_DETAIL_PATH}?id=pack%2Fone`)
  })

  it('presetsLoadCustom returns the stored presets, and [] when they are not a list', async () => {
    stubFetchAll({ body: { customPresets: [{ name: 'dusk' }] } })
    expect(await petBridge.presetsLoadCustom!()).toEqual([{ name: 'dusk' }])

    vi.unstubAllGlobals()
    stubFetchAll({ body: { customPresets: 'nope' } })
    expect(await petBridge.presetsLoadCustom!()).toEqual([])
  })

  it('presetsSaveCustom posts the list to the config route', async () => {
    const { calls } = stubFetchAll({ ok: true })
    expect(await petBridge.presetsSaveCustom!([{ name: 'dusk' }])).toBe(true)
    expect(calls[0].url).toBe(CONFIG_PATH)
    expect(calls[0].body).toEqual({ customPresets: [{ name: 'dusk' }] })
  })

  it('remindersRemove posts the id and reports the HTTP outcome', async () => {
    const { calls } = stubFetchAll({ ok: true })
    expect(await petBridge.remindersRemove!('r-1')).toBe(true)
    expect(calls[0].url).toBe(REMOVE_PATH)
    expect(calls[0].body).toEqual({ id: 'r-1' })

    vi.unstubAllGlobals()
    stubFetchAll({ fails: true })
    expect(await petBridge.remindersRemove!('r-1')).toBe(false)
  })

  it('galleryListPacks asks the appearances route', async () => {
    const { calls } = stubFetchAll({ body: { packs: [] } })
    await petBridge.galleryListPacks!()
    expect(calls[0].url).toBe(APPEARANCES_PATH)
  })

  it('presetsGetColorMap reads the map off the detail payload, null when absent', async () => {
    stubFetchAll({ body: { colorMap: { '#fff': '#f0f' } } })
    expect(await petBridge.presetsGetColorMap!('p')).toEqual({ '#fff': '#f0f' })

    vi.unstubAllGlobals()
    stubFetchAll({ body: {} })
    expect(await petBridge.presetsGetColorMap!('p')).toBeNull()
  })
})

describe('galleryReadPackFile', () => {
  it('returns the ORIGINAL sheet when the requested name is the sprite source', async () => {
    stubFetchAll({ body: { sprite: { source: 'source.png' }, sourceImage: 'QkFTRTY0' } })
    expect(await petBridge.galleryReadPackFile!('p', 'source.png')).toBe('QkFTRTY0')
  })

  it('falls back to the animation slot when the name is not the sprite source', async () => {
    stubFetchAll({ body: { sprite: { source: 'source.png' }, animations: { idle: '<svg/>' } } })
    expect(await petBridge.galleryReadPackFile!('p', 'idle.svg')).toBe('<svg/>')
  })

  it('unwraps an animation slot stored as an object with content', async () => {
    stubFetchAll({ body: { animations: { idle: { content: '<svg>obj</svg>', format: 'svg' } } } })
    expect(await petBridge.galleryReadPackFile!('p', 'idle.svg')).toBe('<svg>obj</svg>')
  })

  it('returns null for a filename the pack does not have', async () => {
    stubFetchAll({ body: { animations: {} } })
    expect(await petBridge.galleryReadPackFile!('p', 'missing.svg')).toBeNull()
  })

  it('returns null when the detail read itself fails', async () => {
    stubFetchAll({ ok: false })
    expect(await petBridge.galleryReadPackFile!('p', 'idle.svg')).toBeNull()
  })
})

describe('galleryImportBundle — the browser file dialog', () => {
  it('installs the parsed bundle and tells the packs listeners', async () => {
    const { calls } = stubFetchAll({ body: { ok: true, packId: 'imported' } })
    armPicker(new File(['{"meta":{"id":"imported"}}'], 'pack.json', { type: 'application/json' }))
    const heard = vi.fn()
    const off = petBridge.onGalleryPacksChanged!(heard)

    const result = await petBridge.galleryImportBundle!()
    off()

    expect(result).toEqual({ ok: true, packId: 'imported' })
    expect(calls[0].url).toBe(APPEARANCE_IMPORT_PATH)
    expect(calls[0].body).toEqual({ bundle: { meta: { id: 'imported' } } })
    expect(heard).toHaveBeenCalledOnce()
  })

  it('treats a cancelled dialog as a silent refusal with no message and no request', async () => {
    const { fn } = stubFetchAll({ body: { ok: true } })
    armPicker(null)
    expect(await petBridge.galleryImportBundle!()).toEqual({ ok: false, error: '' })
    expect(fn).not.toHaveBeenCalled()
  })

  it('refuses a file that is not valid JSON before touching the network', async () => {
    const { fn } = stubFetchAll({ body: { ok: true } })
    armPicker(new File(['not json at all'], 'pack.json', { type: 'application/json' }))
    expect(await petBridge.galleryImportBundle!()).toEqual({
      ok: false,
      error: 'That file is not valid JSON',
    })
    expect(fn).not.toHaveBeenCalled()
  })

  it('reports a generic failure when the import request never answers', async () => {
    stubFetchAll({ fails: true })
    armPicker(new File(['{}'], 'pack.json', { type: 'application/json' }))
    expect(await petBridge.galleryImportBundle!()).toEqual({ ok: false, error: 'Import failed' })
  })

  it('does not notify the packs listeners when the import is refused', async () => {
    stubFetchAll({ ok: false, body: { error: 'already installed' } })
    armPicker(new File(['{}'], 'pack.json', { type: 'application/json' }))
    const heard = vi.fn()
    const off = petBridge.onGalleryPacksChanged!(heard)

    const result = await petBridge.galleryImportBundle!()
    off()

    // postForJson stamps ok:false on a non-2xx body while keeping its reason.
    expect(result).toEqual({ ok: false, error: 'already installed' })
    expect(heard).not.toHaveBeenCalled()
  })
})

describe('galleryImportFile — one art file for one editor slot', () => {
  it('reads an SVG as markup', async () => {
    armPicker(new File(['<svg>art</svg>'], 'Idle.SVG', { type: 'image/svg+xml' }))
    expect(await petBridge.galleryImportFile!()).toEqual({
      ok: true,
      value: { content: '<svg>art</svg>', filename: 'Idle.SVG', format: 'svg' },
    })
  })

  it('reads a valid JSON file as lottie', async () => {
    armPicker(new File(['{"v":"5.7"}'], 'wiggle.json', { type: 'application/json' }))
    expect(await petBridge.galleryImportFile!()).toEqual({
      ok: true,
      value: { content: '{"v":"5.7"}', filename: 'wiggle.json', format: 'lottie' },
    })
  })

  it('refuses broken lottie now rather than at render time', async () => {
    armPicker(new File(['{oops'], 'wiggle.json', { type: 'application/json' }))
    expect(await petBridge.galleryImportFile!()).toEqual({
      ok: false,
      error: 'Could not read wiggle.json',
    })
  })

  it('base64-encodes a PNG into a data URI', async () => {
    armPicker(new File([new Uint8Array([104, 105])], 'sheet.png', { type: 'image/png' }))
    expect(await petBridge.galleryImportFile!()).toEqual({
      ok: true,
      value: { content: `data:image/png;base64,${btoa('hi')}`, filename: 'sheet.png', format: 'sprite' },
    })
  })

  it('names the unsupported type in its refusal', async () => {
    armPicker(new File(['x'], 'notes.txt', { type: 'text/plain' }))
    expect(await petBridge.galleryImportFile!()).toEqual({
      ok: false,
      error: 'Unsupported file type: notes.txt',
    })
  })

  it('answers null on cancel, so the caller shows no message', async () => {
    armPicker(null)
    expect(await petBridge.galleryImportFile!()).toBeNull()
  })

  it('settles once when the dialog reports both a choice and a cancel', async () => {
    const file = new File(['<svg/>'], 'idle.svg', { type: 'image/svg+xml' })
    // Some browsers fire `cancel` after `change`; the second event must not
    // overwrite the chosen file with a null.
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function mockClick(
      this: HTMLInputElement,
    ) {
      Object.defineProperty(this, 'files', { value: [file], configurable: true })
      this.dispatchEvent(new Event('change'))
      this.dispatchEvent(new Event('cancel'))
    })

    const result = await petBridge.galleryImportFile!()
    expect(result).toEqual({
      ok: true,
      value: { content: '<svg/>', filename: 'idle.svg', format: 'svg' },
    })
  })
})

describe('importSpriteFile', () => {
  it('hands back the sheet bytes with the data-URL prefix stripped', async () => {
    armPicker(new File([new Uint8Array([104, 105])], 'sheet.png', { type: 'image/png' }))
    const result = await petBridge.importSpriteFile!()
    expect(result.ok).toBe(true)
    expect(result.value?.content).toBe(btoa('hi'))
  })

  it('answers with an empty message on cancel', async () => {
    armPicker(null)
    expect(await petBridge.importSpriteFile!()).toEqual({ ok: false, error: '' })
  })

  it('reports an unreadable image rather than saving nothing', async () => {
    armPicker(new File(['x'], 'sheet.png', { type: 'image/png' }))
    // A reader that fails is the only way to reach the "could not read" arm; a
    // real FileReader always produces a data URL for a Blob it was handed.
    vi.stubGlobal(
      'FileReader',
      class {
        onload: (() => void) | null = null
        onerror: (() => void) | null = null
        result: string | null = null
        readAsDataURL() {
          this.onerror?.()
        }
      },
    )
    expect(await petBridge.importSpriteFile!()).toEqual({
      ok: false,
      error: 'That image could not be read',
    })
  })
})

describe('gallerySetActive', () => {
  it('notifies in-page listeners AND broadcasts to the overlay window', async () => {
    const { calls } = stubFetchAll({ ok: true })
    const bridge = stubPreload()
    const heard = vi.fn()
    const off = petBridge.onGalleryActiveChanged!(heard)

    const result = await petBridge.gallerySetActive!('ghost')
    off()

    expect(result).toEqual({ ok: true, packId: 'ghost' })
    expect(calls[0].url).toBe(CONFIG_PATH)
    expect(calls[0].body).toEqual({ activeAppearance: 'ghost' })
    expect(heard).toHaveBeenCalledOnce()
    expect(bridge.appearanceChanged).toHaveBeenCalledOnce()
  })

  it('reports a refusal with a reason and repaints nothing', async () => {
    stubFetchAll({ ok: false })
    const bridge = stubPreload()
    const heard = vi.fn()
    const off = petBridge.onGalleryActiveChanged!(heard)

    expect(await petBridge.gallerySetActive!('ghost')).toEqual({
      ok: false,
      error: 'Could not switch avatar',
    })
    off()
    expect(heard).not.toHaveBeenCalled()
    expect(bridge.appearanceChanged).not.toHaveBeenCalled()
  })
})

describe('listener registration', () => {
  it('onGalleryActiveChanged also subscribes to the cross-window broadcast and drops both on unsubscribe', async () => {
    const offBridge = vi.fn()
    const bridge = stubPreload(offBridge)
    const heard = vi.fn()

    const off = petBridge.onGalleryActiveChanged!(heard)
    expect(bridge.onAppearanceChanged).toHaveBeenCalledWith(heard)
    off()
    expect(offBridge).toHaveBeenCalledOnce()

    // Unsubscribed: a later switch reaches nobody.
    stubFetchAll({ ok: true })
    await petBridge.gallerySetActive!('ghost')
    expect(heard).not.toHaveBeenCalled()
  })

  it('onGalleryActiveChanged works in a plain browser tab with no preload bridge', async () => {
    const heard = vi.fn()
    const off = petBridge.onGalleryActiveChanged!(heard)
    stubFetchAll({ ok: true })
    await petBridge.gallerySetActive!('ghost')
    expect(heard).toHaveBeenCalledOnce()
    // Dropping the subscription must not throw when there was no bridge to drop.
    expect(() => off()).not.toThrow()
  })

  it('onColorMapChanged receives the pack id and map, and stops after unsubscribe', async () => {
    stubFetchAll({ ok: true })
    const heard = vi.fn()
    const off = petBridge.onColorMapChanged!(heard)

    await petBridge.gallerySetColorMap!('ghost', { '#fff': '#f0f' })
    expect(heard).toHaveBeenCalledWith({ packId: 'ghost', colorMap: { '#fff': '#f0f' } })

    off()
    await petBridge.gallerySetColorMap!('ghost', { '#fff': '#000' })
    expect(heard).toHaveBeenCalledOnce()
  })

  it('gallerySetColorMap reports a refusal with a reason', async () => {
    stubFetchAll({ ok: false })
    const { calls } = stubFetch(() => ({ ok: false }))
    expect(await petBridge.gallerySetColorMap!('ghost', { '#fff': '#f0f' })).toEqual({
      ok: false,
      error: 'Could not save those colours',
    })
    expect(calls[0].url).toBe(APPEARANCE_COLOURS_PATH)
  })

  it('onConfigUpdated fires on a successful save and stops after unsubscribe', async () => {
    stubFetchAll({ body: { ok: true } })
    const heard = vi.fn()
    const off = petBridge.onConfigUpdated!(heard)

    await petBridge.updateConfig!({ activeAppearance: 'ghost' })
    expect(heard).toHaveBeenCalledOnce()

    off()
    await petBridge.updateConfig!({ activeAppearance: 'other' })
    expect(heard).toHaveBeenCalledOnce()
  })

  it('an activeAppearance patch repaints the overlay window', async () => {
    stubFetchAll({ body: { ok: true } })
    const bridge = stubPreload()
    expect(await petBridge.updateConfig!({ activeAppearance: 'ghost' })).toBe(true)
    expect(bridge.appearanceChanged).toHaveBeenCalledOnce()
  })

  it('updateConfig reports false when the gateway never answers', async () => {
    stubFetchAll({ fails: true })
    expect(await petBridge.updateConfig!({ activeAppearance: 'ghost' })).toBe(false)
  })
})

describe('galleryDelete', () => {
  it('deletes the pack and tells the packs listeners', async () => {
    const { calls } = stubFetchAll({ ok: true })
    const heard = vi.fn()
    const off = petBridge.onGalleryPacksChanged!(heard)

    expect(await petBridge.galleryDelete!('ghost')).toBe(true)
    off()

    expect(calls[0].url).toBe(APPEARANCE_DELETE_PATH)
    expect(calls[0].body).toEqual({ id: 'ghost' })
    expect(heard).toHaveBeenCalledOnce()
  })

  it('a refused delete notifies nobody', async () => {
    stubFetchAll({ ok: false })
    const heard = vi.fn()
    const off = petBridge.onGalleryPacksChanged!(heard)
    expect(await petBridge.galleryDelete!('ghost')).toBe(false)
    off()
    expect(heard).not.toHaveBeenCalled()
  })
})

describe('gallerySaveSpritePack — slot classification and fallbacks', () => {
  const sheet = 'data:image/png;base64,QQ=='

  it('files an unknown slot as a MOOD and keeps the source sheet for re-editing', async () => {
    const { calls } = stubFetchAll({ body: { ok: true } })
    await petBridge.gallerySaveSpritePack!({
      name: 'Ghost',
      author: 'me',
      description: '',
      frameWidth: 32,
      frameHeight: 32,
      fps: 8,
      flipX: false,
      offsetY: 0,
      assignments: { idle: sheet, celebrating: sheet, blank: '' },
      randomAssignments: {},
      rowAssignments: {},
      sourceImage: sheet,
      overwriteId: 'ghost',
    })

    const payload = calls[0].body as {
      id: string
      manifest: {
        states: Record<string, string>
        moods: Record<string, string>
        sprite: { source?: string }
      }
      files: Record<string, string>
    }
    expect(payload.id).toBe('ghost')
    expect(payload.manifest.states).toHaveProperty('idle')
    // `celebrating` is not a known state, so it must land in moods.
    expect(payload.manifest.moods).toHaveProperty('celebrating')
    expect(payload.manifest.states).not.toHaveProperty('celebrating')
    // An empty strip is skipped rather than written as an empty file.
    expect(payload.files).not.toHaveProperty('blank.png')
    expect(payload.manifest.sprite.source).toBe('source.png')
    expect(payload.files['source.png']).toBe('QQ==')
  })

  it('omits the sprite source when the pack kept no sheet', async () => {
    const { calls } = stubFetchAll({ body: { ok: true } })
    await petBridge.gallerySaveSpritePack!({
      name: 'Ghost',
      author: '',
      description: '',
      frameWidth: 32,
      frameHeight: 32,
      fps: 8,
      flipX: false,
      offsetY: 0,
      assignments: { idle: sheet },
      randomAssignments: {},
      rowAssignments: {},
    })
    const payload = calls[0].body as { manifest: { sprite: { source?: string } }; files: Record<string, string> }
    expect(payload.manifest.sprite.source).toBeUndefined()
    expect(payload.files).not.toHaveProperty('source.png')
  })

  it('refuses a sprite pack with no art and does not hit the network', async () => {
    const { fn } = stubFetchAll({ body: { ok: true } })
    const result = await petBridge.gallerySaveSpritePack!({
      name: 'Ghost',
      author: '',
      description: '',
      frameWidth: 32,
      frameHeight: 32,
      fps: 8,
      flipX: false,
      offsetY: 0,
      assignments: {},
      randomAssignments: {},
      rowAssignments: {},
    })
    expect(result.ok).toBe(false)
    expect(typeof result.error).toBe('string')
    expect(result.error).not.toBe('')
    expect(fn).not.toHaveBeenCalled()
  })

  it('reports a failure when the save request never answers', async () => {
    stubFetchAll({ fails: true })
    const result = await petBridge.gallerySaveSpritePack!({
      name: 'Ghost',
      author: '',
      description: '',
      frameWidth: 32,
      frameHeight: 32,
      fps: 8,
      flipX: false,
      offsetY: 0,
      assignments: { idle: sheet },
      randomAssignments: {},
      rowAssignments: {},
    })
    expect(result.ok).toBe(false)
    expect(result.error).not.toBe('')
  })
})

describe('gallerySavePack and petdexFetch fallbacks', () => {
  it('gallerySavePack notifies both packs and active listeners on success', async () => {
    stubFetchAll({ body: { ok: true } })
    const packs = vi.fn()
    const active = vi.fn()
    const offPacks = petBridge.onGalleryPacksChanged!(packs)
    const offActive = petBridge.onGalleryActiveChanged!(active)

    await petBridge.gallerySavePack!({ meta: { id: 'p' }, states: { idle: '<svg/>' } })
    offPacks()
    offActive()

    expect(packs).toHaveBeenCalledOnce()
    expect(active).toHaveBeenCalledOnce()
  })

  it('gallerySavePack reports a generic failure when the request never answers', async () => {
    stubFetchAll({ fails: true })
    expect(await petBridge.gallerySavePack!({ meta: { id: 'p' }, states: { idle: '<svg/>' } })).toEqual({
      ok: false,
      error: 'Save failed',
    })
  })

  it('gallerySavePack files a lottie random clip under an indexed json name', async () => {
    const { calls } = stubFetchAll({ body: { ok: true } })
    await petBridge.gallerySavePack!({
      meta: { id: 'p' },
      states: { idle: '<svg/>' },
      random: { 'Look Up': '{"v":"5.7"}', skipped: '   ' },
    })
    const payload = calls[0].body as {
      manifest: { meta: { format: string }; random: Record<string, string> }
    }
    expect(payload.manifest.random['Look Up']).toBe('random-0.json')
    expect(payload.manifest.random).not.toHaveProperty('skipped')
    expect(payload.manifest.meta.format).toBe('lottie')
    expect(calls[0].url).toBe(APPEARANCE_SAVE_PATH)
  })

  it('petdexFetch passes the input through and falls back to a message on failure', async () => {
    const { calls } = stubFetchAll({ body: { ok: true, name: 'Ghost' } })
    expect(await petBridge.petdexFetch!('ghost')).toEqual({ ok: true, name: 'Ghost' })
    expect(calls[0].url).toBe(PETDEX_FETCH_PATH)
    expect(calls[0].body).toEqual({ input: 'ghost' })

    vi.unstubAllGlobals()
    stubFetchAll({ fails: true })
    expect(await petBridge.petdexFetch!('ghost')).toEqual({
      ok: false,
      error: 'PetDex import failed',
    })
  })

  it('a non-2xx body with no ok field is still reported as a refusal', async () => {
    stubFetchAll({ ok: false, body: { error: 'too large', code: 'payload_too_big' } })
    expect(await petBridge.petdexFetch!('ghost')).toEqual({
      ok: false,
      error: 'too large',
      code: 'payload_too_big',
    })
  })

  it('a non-2xx body that is not an object still reports a refusal', async () => {
    stubFetchAll({ ok: false, body: 'plain text' })
    expect(await petBridge.petdexFetch!('ghost')).toEqual({ ok: false })
  })
})

describe('openExternal', () => {
  it('hands an https link to the main process when the bridge is there', () => {
    const bridge = stubPreload()
    petBridge.openExternal!('https://petdex.dev/ghost')
    expect(bridge.openExternal).toHaveBeenCalledWith('https://petdex.dev/ghost')
  })

  it('opens a new tab when there is no bridge', () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    petBridge.openExternal!('https://petdex.dev/ghost')
    expect(open).toHaveBeenCalledWith('https://petdex.dev/ghost', '_blank', 'noopener,noreferrer')
  })

  it('refuses a non-https scheme outright', () => {
    const bridge = stubPreload()
    const open = vi.fn()
    vi.stubGlobal('open', open)
    petBridge.openExternal!('file:///etc/hosts')
    petBridge.openExternal!('http://petdex.dev')
    expect(bridge.openExternal).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
  })
})

describe('window-level bridge calls', () => {
  it('closeGallery asks the main process to close its window', () => {
    const bridge = stubPreload()
    petBridge.closeGallery!()
    expect(bridge.galleryClose).toHaveBeenCalledOnce()
  })

  it('updateHitbox and setMenuHitbox forward their rects', () => {
    const bridge = stubPreload()
    const pet = { x: 1, y: 2, w: 3, h: 4 }
    petBridge.updateHitbox!(pet, null)
    petBridge.setMenuHitbox!(pet)
    petBridge.setMenuHitbox!(null)
    expect(bridge.updateHitbox).toHaveBeenCalledWith(pet, null)
    expect(bridge.setMenuHitbox).toHaveBeenNthCalledWith(1, pet)
    expect(bridge.setMenuHitbox).toHaveBeenNthCalledWith(2, null)
  })

  it('every window-level call is a no-op in a plain browser tab', () => {
    expect(() => {
      petBridge.closeGallery!()
      petBridge.updateHitbox!(null, null)
      petBridge.setMenuHitbox!(null)
      petBridge.contextMenuAction!('gallery')
    }).not.toThrow()
  })
})

describe('contextMenuAction', () => {
  it('"quit" disables the app instead of quitting Kiro Crew itself', async () => {
    const { calls } = stubFetchAll({ ok: true })
    const bridge = stubPreload()
    petBridge.contextMenuAction!('quit')
    await Promise.resolve()
    expect(calls[0].url).toBe('/api/apps/crew-companion/disable')
    expect(calls[0].method).toBe('POST')
    // Disabling is a gateway call, not a window-level one.
    expect(bridge.contextMenuAction).not.toHaveBeenCalled()
  })

  it('a failed disable request is swallowed', async () => {
    stubFetchAll({ fails: true })
    expect(() => petBridge.contextMenuAction!('quit')).not.toThrow()
    await Promise.resolve()
    await Promise.resolve()
  })

  it('"gallery" opens the gallery window', () => {
    const bridge = stubPreload()
    petBridge.contextMenuAction!('gallery')
    expect(bridge.galleryOpen).toHaveBeenCalledOnce()
    expect(bridge.contextMenuAction).not.toHaveBeenCalled()
  })

  it('anything else is forwarded to the main process verbatim', () => {
    const bridge = stubPreload()
    petBridge.contextMenuAction!('panel')
    expect(bridge.contextMenuAction).toHaveBeenCalledWith('panel')
  })
})

describe('cross-display hooks are deliberately absent', () => {
  // Left undefined and documented rather than deleted, so the ported hooks'
  // `api?.on*?.()` guards resolve to no-ops on a single display.
  it('drag and walk hooks are undefined so the hooks degrade to no-ops', () => {
    expect(petBridge.dragStart).toBeUndefined()
    expect(petBridge.dragEnd).toBeUndefined()
    expect(petBridge.onDragUpdate).toBeUndefined()
    expect(petBridge.onDragEnded).toBeUndefined()
    expect(petBridge.onWalk).toBeUndefined()
    expect(petBridge.onWalkPath).toBeUndefined()
    expect(petBridge.onWalkCancel).toBeUndefined()
    expect(petBridge.onWalkAppend).toBeUndefined()
    expect(petBridge.onHide).toBeUndefined()
    expect(petBridge.walkDone).toBeUndefined()
  })
})
