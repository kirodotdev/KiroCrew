/**
 * `mochiApi` — the WEB TRANSPORTS half of the Mochi seam.
 *
 * `mochiApiSeam.test.ts` already pins the config nesting, the pack-detail
 * inlining and the panel-width arithmetic. What it does not reach is the set of
 * functions that exist only because a browser tab cannot do what the original's
 * Electron main process did: every one of them talks to a Kiro Crew HTTP route,
 * a transient `<input type="file">`, or a blob download.
 *
 * Those are exactly the paths that fail SILENTLY. The vendored call sites are all
 * written `api?.name?.(...)`, so a transport that throws, reads the wrong body
 * shape, or resolves `undefined` renders an empty list or a dead button with no
 * error anywhere. So each case below pins one of three things: the request that
 * actually goes out (route, method, body), the value handed back for the shape
 * the backend really returns, and the degraded answer on failure — a list must
 * come back empty rather than throw, and a `Result` must carry a reason rather
 * than resolve nothing.
 *
 * The harness mocks the three modules the seam composes (`api`, `panelBridge`,
 * `petBridge`) and re-imports the seam per test, because `impl` captures the
 * shell channels and the rail state at module evaluation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// ── the modules the seam composes ───────────────────────────────────────────

const storedSettings: Record<string, unknown> = {
  petName: 'Mochi',
  mode: 'quiet',
  shortcuts: { toggleWindow: 'CommandOrControl+Shift+M' },
  extraMcpServers: [] as unknown[],
  colorMaps: {} as Record<string, unknown>,
  customPresets: [] as unknown[],
  chatAlwaysOnTop: true,
}

const getSettings = vi.fn(async (): Promise<Record<string, unknown>> => storedSettings)
const updateSettings = vi.fn(async (patch: Record<string, unknown>) => ({
  ...storedSettings,
  ...patch,
}))

const panelCalls = {
  deleteHistory: vi.fn(async (): Promise<void> => undefined),
  disableApp: vi.fn(async (): Promise<void> => undefined),
  openDashboard: vi.fn(),
  galleryListPacks: vi.fn(async (): Promise<unknown[]> => []),
  galleryDeletePack: vi.fn(async () => true),
  gallerySetActive: vi.fn(async (): Promise<void> => undefined),
  galleryGetPackDetail: vi.fn(async (): Promise<unknown> => null),
  getWatchlistItems: vi.fn(async (): Promise<unknown[]> => []),
}

const petCalls = {
  openChat: vi.fn(),
  openAvatars: vi.fn(),
  openSettings: vi.fn(),
  openMemories: vi.fn(),
}

vi.mock('../apps/mochi/api', () => ({
  getSettings: () => getSettings(),
  getStats: async () => ({}),
  updateSettings: (patch: Record<string, unknown>) => updateSettings(patch),
}))

vi.mock('../apps/mochi/panel/panelBridge', () => ({
  deleteHistory: () => panelCalls.deleteHistory(),
  disableApp: () => panelCalls.disableApp(),
  openDashboard: () => panelCalls.openDashboard(),
  galleryListPacks: () => panelCalls.galleryListPacks(),
  galleryDeletePack: (id: string) => panelCalls.galleryDeletePack(id),
  gallerySetActive: (id: string) => panelCalls.gallerySetActive(id),
  galleryGetPackDetail: (id: string) => panelCalls.galleryGetPackDetail(id),
  getWatchlistItems: () => panelCalls.getWatchlistItems(),
  localFileUrl: (path: string) => `/api/file-raw?path=${encodeURIComponent(path)}`,
  galleryPackFileUrl: (packId: string, filename: string) => `/packs/${packId}/${filename}`,
}))

vi.mock('../apps/mochi/pet/petBridge', () => ({
  openChat: () => petCalls.openChat(),
  openAvatars: () => petCalls.openAvatars(),
  openSettings: () => petCalls.openSettings(),
  openMemories: () => petCalls.openMemories(),
  hasShell: false,
  machinePrefs: async () => null,
  setPetInstance: vi.fn(async () => true),
  instancesList: vi.fn(async () => null),
}))

// ── fetch double ────────────────────────────────────────────────────────────

interface FetchReply {
  ok?: boolean
  status?: number
  json?: unknown
  /** A body that is not JSON at all — the seam's `.catch(() => ...)` path. */
  jsonThrows?: boolean
  bytes?: number[]
  blob?: Blob
}

function makeResponse(r: FetchReply): Response {
  return {
    ok: r.ok ?? true,
    status: r.status ?? (r.ok === false ? 500 : 200),
    json: async () => {
      if (r.jsonThrows === true) throw new SyntaxError('not json')
      return r.json ?? {}
    },
    arrayBuffer: async () => new Uint8Array(r.bytes ?? []).buffer,
    blob: async () => r.blob ?? new Blob(['bundle']),
  } as unknown as Response
}

type Route = FetchReply | 'reject'

function stubFetch(next: Route | ((url: string) => Route)) {
  const calls: { url: string; init?: RequestInit }[] = []
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    const r = typeof next === 'function' ? next(url) : next
    if (r === 'reject') throw new TypeError('network down')
    return makeResponse(r)
  })
  vi.stubGlobal('fetch', fn)
  return { fn, calls }
}

function bodyOf(call: { init?: RequestInit }): Record<string, unknown> {
  return JSON.parse(String(call.init?.body ?? '{}')) as Record<string, unknown>
}

// ── DOM doubles: the transient <input> and the download <a> ─────────────────
//
// jsdom opens no file dialog and performs no navigation, so both elements have
// to be driven from the test. `createElement` is intercepted rather than the
// elements queried afterwards: `importSpriteFile` removes its input again, so by
// the time the promise settles there is nothing left in the DOM to find.

interface DomHarness {
  /** What the "dialog" answers on the next `click()`. */
  gesture: 'change' | 'cancel' | 'none'
  files: File[]
  anchors: HTMLAnchorElement[]
  anchorClicks: string[]
  inputs: HTMLInputElement[]
}

function setFiles(input: HTMLInputElement, files: File[]): void {
  Object.defineProperty(input, 'files', {
    configurable: true,
    value: { length: files.length, 0: files[0], item: (i: number) => files[i] ?? null },
  })
}

function installDomHarness(): DomHarness {
  const h: DomHarness = {
    gesture: 'change',
    files: [],
    anchors: [],
    anchorClicks: [],
    inputs: [],
  }
  const create = document.createElement.bind(document)
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
    const el = create(tag)
    if (tag === 'a') {
      const anchor = el as HTMLAnchorElement
      h.anchors.push(anchor)
      anchor.click = () => {
        h.anchorClicks.push(anchor.download)
      }
    }
    if (tag === 'input') {
      const input = el as HTMLInputElement
      h.inputs.push(input)
      input.click = () => {
        if (h.gesture === 'none') return
        if (h.gesture === 'cancel') {
          input.dispatchEvent(new Event('cancel'))
          return
        }
        setFiles(input, h.files)
        input.dispatchEvent(new Event('change'))
      }
    }
    return el
  }) as typeof document.createElement)
  return h
}

// ── seam loading ────────────────────────────────────────────────────────────

const shell = { setPanelWidth: vi.fn(), hideAll: vi.fn() }

async function loadSeam() {
  vi.resetModules()
  return await import('../apps/mochi/src/mochiApi')
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(window as unknown as { mochi?: unknown }).mochi = shell
  const url = URL as unknown as Record<string, unknown>
  url.createObjectURL = vi.fn(() => 'blob:mochi-export')
  url.revokeObjectURL = vi.fn()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// ── MCP inventory ───────────────────────────────────────────────────────────

describe('mochiApi MCP inventory', () => {
  const inventory = [
    { name: 'plain-name', tools: [{ name: 'a' }, { name: 'b' }] },
    { name: 'slack-mcp', tool_count: 7 },
    { name: 'mochi:tools' },
    { name: 'kirocrew-core' },
    { name: 'unconfigured' },
  ]

  it("merges core's inventory with Mochi's own per-server assignment", async () => {
    getSettings.mockResolvedValueOnce({
      ...storedSettings,
      extraMcpServers: [
        'plain-name',
        { name: 'slack-mcp', agents: ['bg'], autoApprove: ['post'], disabledTools: ['nuke'] },
      ],
    })
    const { calls } = stubFetch({ json: inventory })
    const seam = await loadSeam()

    const rows = await seam.api.getMcpServers()

    // Inventory comes from core, assignment from Mochi's settings. Without the
    // merge every row renders default policy no matter what the user configured.
    expect(calls[0].url).toBe('/api/mcp')
    const plain = rows.find((r) => r.name === 'plain-name')
    expect(plain).toMatchObject({ enabled: true, agents: ['chat'], toolCount: 2 })
    const slack = rows.find((r) => r.name === 'slack-mcp')
    expect(slack).toMatchObject({
      enabled: true,
      agents: ['bg'],
      autoApprove: ['post'],
      disabledTools: ['nuke'],
      toolCount: 7,
    })
    expect(rows.find((r) => r.name === 'unconfigured')).toMatchObject({
      enabled: false,
      agents: ['chat'],
      toolCount: undefined,
    })
  })

  it('marks already-granted servers core so they are not offered as addable', async () => {
    stubFetch({ json: inventory })
    const seam = await loadSeam()

    const rows = await seam.api.getMcpServers()

    // The app's OWN server and the host-managed ones are already in the
    // baseline grant; listing them as addable was the bug.
    expect(rows.find((r) => r.name === 'mochi:tools')?.core).toBe(true)
    expect(rows.find((r) => r.name === 'kirocrew-core')?.core).toBe(true)
    expect(rows.find((r) => r.name === 'plain-name')?.core).toBe(false)
  })

  it('reads a wrapped body as well as the bare array core actually returns', async () => {
    stubFetch({ json: { servers: [{ name: 'wrapped' }] } })
    const seam = await loadSeam()

    expect((await seam.api.getMcpServers()).map((r) => r.name)).toEqual(['wrapped'])
  })

  it('yields an empty list when the route fails rather than throwing at the panel', async () => {
    stubFetch({ ok: false, status: 500 })
    const seam = await loadSeam()

    expect(await seam.api.getMcpServers()).toEqual([])
  })

  it('yields an empty list when the request throws', async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    expect(await seam.api.getMcpServers()).toEqual([])
  })

  it('still lists the inventory when the settings read fails', async () => {
    getSettings.mockRejectedValueOnce(new Error('settings down'))
    stubFetch({ json: inventory })
    const seam = await loadSeam()

    const rows = await seam.api.getMcpServers()

    // Assignment is unknown, so every row reports unconfigured — but the
    // section must still render its servers.
    expect(rows).toHaveLength(5)
    expect(rows.every((r) => r.enabled === false)).toBe(true)
  })
})

// ── binary reads ────────────────────────────────────────────────────────────

describe('mochiApi binary reads', () => {
  it('returns base64 for a readable local file', async () => {
    // The caller wraps the result in a `data:image/png;base64,` URL, so a plain
    // URL cannot be substituted for the encoded bytes.
    const { calls } = stubFetch({ bytes: [72, 105] })
    const seam = await loadSeam()

    expect(await seam.api.readLocalImage('/tmp/a.png')).toBe('SGk=')
    expect(calls[0].url).toContain('/api/file-raw?path=')
  })

  it('returns null when the local file cannot be read', async () => {
    stubFetch({ ok: false, status: 404 })
    const seam = await loadSeam()

    expect(await seam.api.readLocalImage('/tmp/missing.png')).toBeNull()
  })

  it('returns null when the local read throws', async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    expect(await seam.api.readLocalImage('/tmp/a.png')).toBeNull()
  })

  it('returns base64 for one file inside a pack', async () => {
    const { calls } = stubFetch({ bytes: [72, 105] })
    const seam = await loadSeam()

    expect(await seam.api.galleryReadPackFile('p1', 'idle.png')).toBe('SGk=')
    expect(calls[0].url).toBe('/packs/p1/idle.png')
  })

  it('returns null for a missing pack file', async () => {
    stubFetch({ ok: false, status: 404 })
    const seam = await loadSeam()

    expect(await seam.api.galleryReadPackFile('p1', 'nope.png')).toBeNull()
  })

  it('returns null when the pack file read throws', async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    expect(await seam.api.galleryReadPackFile('p1', 'idle.png')).toBeNull()
  })
})

// ── voice config ────────────────────────────────────────────────────────────

describe('mochiApi speech-to-text config', () => {
  it('posts the patch to the core route as JSON', async () => {
    const { calls } = stubFetch({ json: { ok: true } })
    const seam = await loadSeam()

    await seam.api.updateSttConfig({ provider: 'whisper' })

    expect(calls[0].url).toBe('/api/config/stt')
    expect(calls[0].init?.method).toBe('POST')
    expect(bodyOf(calls[0])).toEqual({ provider: 'whisper' })
  })

  it('swallows a failed write — voice must not take the settings window down', async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    await expect(seam.api.updateSttConfig({ provider: 'whisper' })).resolves.toBeUndefined()
  })
})

// ── petdex.dev ──────────────────────────────────────────────────────────────

describe('mochiApi petdex import', () => {
  it('lists the pets the CLI already installed', async () => {
    const { calls } = stubFetch({ json: { pets: [{ slug: 'fox' }] } })
    const seam = await loadSeam()

    expect(await seam.api.petdexListInstalled()).toEqual([{ slug: 'fox' }])
    expect(calls[0].url).toBe('/api/apps/mochi/petdex/installed')
  })

  it('drives the list with an empty array when the body carries no pets', async () => {
    stubFetch({ json: { pets: 'not-an-array' } })
    const seam = await loadSeam()

    expect(await seam.api.petdexListInstalled()).toEqual([])
  })

  it('never throws out of the installed list', async () => {
    stubFetch((url) => (url.includes('installed') ? 'reject' : {}))
    const seam = await loadSeam()

    expect(await seam.api.petdexListInstalled()).toEqual([])
  })

  it('drives the list with an empty array when the route fails', async () => {
    stubFetch({ ok: false, status: 403 })
    const seam = await loadSeam()

    expect(await seam.api.petdexListInstalled()).toEqual([])
  })

  it('returns the obtained pet on success', async () => {
    const { calls } = stubFetch({ json: { ok: true, slug: 'fox', frames: 9 } })
    const seam = await loadSeam()

    const result = await seam.api.petdexImport('fox', 'remote')

    expect(calls[0].url).toBe('/api/apps/mochi/petdex/import')
    expect(bodyOf(calls[0])).toEqual({ slug: 'fox', source: 'remote' })
    expect(result).toMatchObject({ ok: true, value: { slug: 'fox' } })
  })

  it("reports the backend's own reason when the import is refused", async () => {
    stubFetch({ ok: false, status: 502, json: { error: 'petdex unreachable' } })
    const seam = await loadSeam()

    expect(await seam.api.petdexImport('fox', 'remote')).toEqual({
      ok: false,
      error: 'petdex unreachable',
    })
  })

  it('falls back to the status when a refusal carries no reason', async () => {
    // A 200 whose body never says ok is a failure too, not a silent success.
    stubFetch({ ok: false, status: 418, json: {} })
    const seam = await loadSeam()

    expect(await seam.api.petdexImport('fox', 'local')).toEqual({
      ok: false,
      error: 'Import failed (HTTP 418)',
    })
  })

  it('treats a 200 that never confirms ok as a failure', async () => {
    stubFetch({ json: { slug: 'fox' } })
    const seam = await loadSeam()

    expect(await seam.api.petdexImport('fox', 'local')).toMatchObject({ ok: false })
  })

  it("reports a thrown error's message rather than escaping the button", async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    expect(await seam.api.petdexImport('fox', 'remote')).toEqual({
      ok: false,
      error: 'network down',
    })
  })

  it('survives a body that is not JSON at all', async () => {
    // An HTML error page from a proxy is not a parse the caller should die on.
    stubFetch({ ok: false, status: 502, jsonThrows: true })
    const seam = await loadSeam()

    expect(await seam.api.petdexImport('fox', 'remote')).toEqual({
      ok: false,
      error: 'Import failed (HTTP 502)',
    })
  })
})

// ── pack export / import ────────────────────────────────────────────────────

describe('mochiApi pack export', () => {
  it('downloads the bundle under its pack id and revokes the blob URL after', async () => {
    vi.useFakeTimers()
    const dom = installDomHarness()
    const { calls } = stubFetch({ blob: new Blob(['zip-bytes']) })
    const seam = await loadSeam()

    const result = await seam.api.galleryExport('my pack')

    expect(result).toEqual({ ok: true, value: null })
    expect(calls[0].url).toBe('/api/apps/mochi/packs/my%20pack/export')
    // The name is what makes a bundle exported here importable upstream.
    expect(dom.anchorClicks).toEqual(['my pack.mochipack.zip'])
    const url = URL as unknown as { revokeObjectURL: ReturnType<typeof vi.fn> }
    expect(url.revokeObjectURL).not.toHaveBeenCalled()
    vi.advanceTimersByTime(60_000)
    expect(url.revokeObjectURL).toHaveBeenCalledWith('blob:mochi-export')
  })

  it("reports the backend's reason when the export is refused", async () => {
    installDomHarness()
    stubFetch({ ok: false, status: 500, json: { error: 'pack is locked' } })
    const seam = await loadSeam()

    expect(await seam.api.galleryExport('p1')).toEqual({ ok: false, error: 'pack is locked' })
  })

  it('falls back to the status when the refusal body is not JSON', async () => {
    installDomHarness()
    stubFetch({ ok: false, status: 503, jsonThrows: true })
    const seam = await loadSeam()

    expect(await seam.api.galleryExport('p1')).toEqual({ ok: false, error: 'Export failed (503)' })
  })

  it('reports a thrown error rather than resolving nothing', async () => {
    installDomHarness()
    stubFetch('reject')
    const seam = await loadSeam()

    expect(await seam.api.galleryExport('p1')).toEqual({ ok: false, error: 'network down' })
  })
})

describe('mochiApi bundle import', () => {
  it('posts the picked file as a zip body and returns the stored meta', async () => {
    const dom = installDomHarness()
    dom.files = [new File(['zip'], 'pack.mochipack.zip', { type: 'application/zip' })]
    const { calls } = stubFetch({ json: { meta: { id: 'imported', name: 'Imported' } } })
    const seam = await loadSeam()

    const result = await seam.api.galleryImportBundle()

    expect(calls[0].url).toBe('/api/apps/mochi/packs/import')
    expect(calls[0].init?.method).toBe('POST')
    expect(calls[0].init?.body).toBe(dom.files[0])
    expect(result).toEqual({ ok: true, value: { id: 'imported', name: 'Imported' } })
  })

  it('resolves null when the user picks nothing, so the button un-busies', async () => {
    const dom = installDomHarness()
    dom.files = []
    const { fn } = stubFetch({})
    const seam = await loadSeam()

    expect(await seam.api.galleryImportBundle()).toBeNull()
    expect(fn).not.toHaveBeenCalled()
  })

  it('reports a reason when the bundle is rejected', async () => {
    const dom = installDomHarness()
    dom.files = [new File(['zip'], 'pack.zip', { type: 'application/zip' })]
    stubFetch({ ok: false, status: 400, json: { error: 'not a pack' } })
    const seam = await loadSeam()

    expect(await seam.api.galleryImportBundle()).toEqual({ ok: false, error: 'not a pack' })
  })

  it('reports a reason when a 200 comes back with no meta', async () => {
    const dom = installDomHarness()
    dom.files = [new File(['zip'], 'pack.zip', { type: 'application/zip' })]
    stubFetch({ json: {} })
    const seam = await loadSeam()

    expect(await seam.api.galleryImportBundle()).toMatchObject({ ok: false })
  })

  it('falls back to the status when the refusal body is not JSON', async () => {
    const dom = installDomHarness()
    dom.files = [new File(['zip'], 'pack.zip', { type: 'application/zip' })]
    stubFetch({ ok: false, status: 413, jsonThrows: true })
    const seam = await loadSeam()

    expect(await seam.api.galleryImportBundle()).toEqual({
      ok: false,
      error: 'Import failed (413)',
    })
  })
})

describe('mochiApi sprite import', () => {
  it("carries the file's REAL mime, not a hard-coded png", async () => {
    const dom = installDomHarness()
    // petdex ships WebP; hard-coding png left the decode to browser sniffing.
    dom.files = [new File([new Uint8Array([1, 2, 3])], 'sheet.webp', { type: 'image/webp' })]
    const seam = await loadSeam()

    expect(await seam.api.importSpriteFile()).toEqual({
      ok: true,
      value: { content: 'AQID', mime: 'image/webp' },
    })
  })

  it('resolves on cancel, because the importer awaits it', async () => {
    const dom = installDomHarness()
    dom.gesture = 'cancel'
    const seam = await loadSeam()

    expect(await seam.api.importSpriteFile()).toBeNull()
  })

  it('resolves null when the dialog closes with no file selected', async () => {
    const dom = installDomHarness()
    dom.files = []
    const seam = await loadSeam()

    expect(await seam.api.importSpriteFile()).toBeNull()
  })

  it('leaves no transient input behind in the document', async () => {
    const dom = installDomHarness()
    dom.files = [new File([new Uint8Array([1])], 'sheet.png', { type: 'image/png' })]
    const seam = await loadSeam()

    await seam.api.importSpriteFile()

    expect(document.querySelectorAll('input[type="file"]')).toHaveLength(0)
  })

  it('settles once — a late cancel after a pick cannot re-resolve', async () => {
    const dom = installDomHarness()
    dom.files = [new File([new Uint8Array([9])], 'sheet.png', { type: 'image/png' })]
    const seam = await loadSeam()

    const first = await seam.api.importSpriteFile()
    // The listeners outlive the promise, so the guard is what keeps a late
    // dialog event from settling an already-settled import.
    expect(() => dom.inputs[0].dispatchEvent(new Event('cancel'))).not.toThrow()
    expect(first).toMatchObject({ ok: true })
  })
})

// ── capture handshake ───────────────────────────────────────────────────────

describe('mochiApi capture handshake', () => {
  it('routes a request to the host and the PNG back to the chat panel', async () => {
    const seam = await loadSeam()
    const requests = vi.fn()
    const delivered = vi.fn()
    const stopHost = seam.onCaptureRequested(requests)
    const stopPanel = seam.api.onCaptureDone(delivered)

    // The flow is split because the seam is not a React tree: startCapture only
    // RAISES the request; the snip host performs it and delivers the bytes.
    seam.api.startCapture()
    expect(requests).toHaveBeenCalledTimes(1)
    seam.deliverCapture('iVBORw0=')
    expect(delivered).toHaveBeenCalledWith('iVBORw0=')

    stopHost()
    stopPanel()
    seam.api.startCapture()
    seam.deliverCapture('second')
    expect(requests).toHaveBeenCalledTimes(1)
    expect(delivered).toHaveBeenCalledTimes(1)
  })

  it('delivers one capture to every subscriber', async () => {
    const seam = await loadSeam()
    const first = vi.fn()
    const second = vi.fn()
    seam.api.onCaptureDone(first)
    seam.api.onCaptureDone(second)

    seam.deliverCapture('png')

    expect(first).toHaveBeenCalledWith('png')
    expect(second).toHaveBeenCalledWith('png')
  })
})

// ── reset ───────────────────────────────────────────────────────────────────

describe('mochiApi reset', () => {
  it('clears the chat slot BEFORE rewriting settings', async () => {
    const order: string[] = []
    panelCalls.deleteHistory.mockImplementationOnce(async () => {
      order.push('history')
    })
    const { calls } = stubFetch({ json: { ok: true } })
    const seam = await loadSeam()

    await seam.api.resetMochi()

    order.push(calls[0].url)
    // History first: if it fails the user still has their settings, which is
    // the recoverable half.
    expect(order).toEqual(['history', '/api/apps/mochi/reset'])
    expect(calls[0].init?.method).toBe('POST')
  })

  it('throws with the status when the reset route fails', async () => {
    stubFetch({ ok: false, status: 503 })
    const seam = await loadSeam()

    await expect(seam.api.resetMochi()).rejects.toThrow('reset failed: 503')
  })
})

// ── pack authoring ──────────────────────────────────────────────────────────

describe('mochiApi pack authoring', () => {
  it('saves per-slot content and returns the stored meta', async () => {
    const { calls } = stubFetch({ json: { meta: { id: 'edited' } } })
    const seam = await loadSeam()

    const result = await seam.api.gallerySavePack({ id: 'edited', states: {} })

    expect(calls[0].url).toBe('/api/apps/mochi/packs/content')
    expect(bodyOf(calls[0])).toMatchObject({ id: 'edited' })
    expect(result).toEqual({ ok: true, value: { id: 'edited' } })
  })

  it('reports a reason when the save is refused', async () => {
    stubFetch({ ok: false, status: 409, json: { error: 'name taken' } })
    const seam = await loadSeam()

    expect(await seam.api.gallerySavePack({})).toEqual({ ok: false, error: 'name taken' })
  })

  it('falls back to the status when a save answers 200 with no meta', async () => {
    stubFetch({ json: {} })
    const seam = await loadSeam()

    expect(await seam.api.gallerySavePack({})).toEqual({ ok: false, error: 'Save failed (200)' })
  })

  it('reports a thrown save error', async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    expect(await seam.api.gallerySavePack({})).toEqual({ ok: false, error: 'network down' })
  })

  it('falls back to the status when the refusal body is not JSON', async () => {
    stubFetch({ ok: false, status: 507, jsonThrows: true })
    const seam = await loadSeam()

    expect(await seam.api.gallerySavePack({})).toEqual({ ok: false, error: 'Save failed (507)' })
  })

  it("reports a reason for the authoring paths a page cannot do at all", async () => {
    const seam = await loadSeam()

    // Absent would be a no-op button; the vendored window renders `error`.
    expect(await seam.api.galleryImportFile()).toEqual({
      ok: false,
      error: 'Not available in this build',
    })
  })
})

describe('mochiApi colour maps and presets', () => {
  it("persists one pack's colour map without clobbering the others", async () => {
    getSettings.mockResolvedValueOnce({
      ...storedSettings,
      colorMaps: { other: { body: '#111' } },
    })
    const seam = await loadSeam()

    await seam.api.gallerySetColorMap('p1', { body: '#fff' })

    expect(updateSettings).toHaveBeenCalledWith({
      colorMaps: { other: { body: '#111' }, p1: { body: '#fff' } },
    })
  })

  it('still writes the one map when the current settings cannot be read', async () => {
    getSettings.mockRejectedValueOnce(new Error('settings down'))
    const seam = await loadSeam()

    await seam.api.gallerySetColorMap('p1', { body: '#fff' })

    expect(updateSettings).toHaveBeenCalledWith({ colorMaps: { p1: { body: '#fff' } } })
  })

  it('round-trips the custom cat presets', async () => {
    getSettings.mockResolvedValueOnce({ ...storedSettings, customPresets: [{ id: 'dusk' }] })
    const seam = await loadSeam()

    expect(await seam.api.presetsLoadCustom()).toEqual([{ id: 'dusk' }])
    await seam.api.presetsSaveCustom([{ id: 'dawn' }] as never)
    expect(updateSettings).toHaveBeenCalledWith({ customPresets: [{ id: 'dawn' }] })
  })

  it('reports no presets when the settings read fails', async () => {
    getSettings.mockRejectedValueOnce(new Error('settings down'))
    const seam = await loadSeam()

    expect(await seam.api.presetsLoadCustom()).toEqual([])
  })
})

// ── quiet mode ──────────────────────────────────────────────────────────────

describe('mochiApi quiet mode', () => {
  it('reads the quiet expiry off the pet-state pull', async () => {
    const { calls } = stubFetch({ json: { silentUntil: 1770000000000 } })
    const seam = await loadSeam()

    expect(await seam.api.getQuietUntil()).toBe(1770000000000)
    expect(calls[0].url).toBe('/api/apps/mochi/pet-state')
  })

  it('treats a non-numeric expiry as not quiet', async () => {
    stubFetch({ json: { silentUntil: 'soon' } })
    const seam = await loadSeam()

    expect(await seam.api.getQuietUntil()).toBe(0)
  })

  it('reports not quiet when the pet-state route fails', async () => {
    stubFetch({ ok: false, status: 500 })
    const seam = await loadSeam()

    expect(await seam.api.getQuietUntil()).toBe(0)
  })

  it('reports not quiet when the pet-state read throws', async () => {
    stubFetch('reject')
    const seam = await loadSeam()

    // A menu that cannot reach the gateway shows the normal item, not a throw.
    expect(await seam.api.getQuietUntil()).toBe(0)
  })

  it('enters quiet mode by posting minutes and returns the new expiry', async () => {
    const { calls } = stubFetch({ json: { silentUntil: 1770000060000 } })
    const seam = await loadSeam()

    expect(await seam.api.setQuiet(60)).toBe(1770000060000)
    expect(calls[0].url).toBe('/api/apps/mochi/quiet')
    expect(bodyOf(calls[0])).toEqual({ minutes: 60 })
  })

  it('leaves quiet mode by posting zero', async () => {
    const { calls } = stubFetch({ json: { silentUntil: 0 } })
    const seam = await loadSeam()

    expect(await seam.api.setQuiet(0)).toBe(0)
    expect(bodyOf(calls[0])).toEqual({ minutes: 0 })
  })

  it('reports not quiet when the write fails or throws', async () => {
    stubFetch({ ok: false, status: 500 })
    const failing = await loadSeam()
    expect(await failing.api.setQuiet(60)).toBe(0)

    stubFetch('reject')
    const throwing = await loadSeam()
    expect(await throwing.api.setQuiet(60)).toBe(0)
  })

  it('reports not quiet when the write answers with no expiry', async () => {
    stubFetch({ json: {} })
    const seam = await loadSeam()

    expect(await seam.api.setQuiet(60)).toBe(0)
  })
})

// ── pet context menu ────────────────────────────────────────────────────────

describe('mochiApi context-menu dispatch', () => {
  it('maps every menu id onto the bridge call behind it', async () => {
    const seam = await loadSeam()

    seam.api.contextMenuAction('chat')
    seam.api.contextMenuAction('openChat')
    expect(petCalls.openChat).toHaveBeenCalledTimes(2)

    seam.api.contextMenuAction('avatars')
    seam.api.contextMenuAction('gallery')
    expect(petCalls.openAvatars).toHaveBeenCalledTimes(2)

    seam.api.contextMenuAction('settings')
    expect(petCalls.openSettings).toHaveBeenCalledTimes(1)
    seam.api.contextMenuAction('memories')
    expect(petCalls.openMemories).toHaveBeenCalledTimes(1)
    seam.api.contextMenuAction('dashboard')
    expect(panelCalls.openDashboard).toHaveBeenCalledTimes(1)
  })

  it('separates hide from disable — they are different user intents', async () => {
    const seam = await loadSeam()

    seam.api.contextMenuAction('hide')
    expect(shell.hideAll).toHaveBeenCalledTimes(1)
    expect(panelCalls.disableApp).not.toHaveBeenCalled()

    seam.api.contextMenuAction('disable')
    expect(panelCalls.disableApp).toHaveBeenCalledTimes(1)
  })

  it('ignores an unknown id instead of throwing', async () => {
    const seam = await loadSeam()

    // A menu entry that outlives its handler should be inert, not fatal.
    expect(() => seam.api.contextMenuAction('no-such-action')).not.toThrow()
  })

  it('is inert for hide when there is no shell to hide into', async () => {
    ;(window as unknown as { mochi?: unknown }).mochi = {}
    const seam = await loadSeam()

    expect(() => seam.api.contextMenuAction('hide')).not.toThrow()
    expect(shell.hideAll).not.toHaveBeenCalled()
  })
})
