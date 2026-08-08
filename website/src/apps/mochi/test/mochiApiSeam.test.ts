/**
 * Tests for `src/mochiApi.ts` — the ONE hand-written layer between the vendored
 * original Mochi renderer and this builtin.
 *
 * Everything else under `apps/mochi/src/` is upstream code copied verbatim, so
 * this seam is where a migration bug can still hide. Each case below pins a
 * behaviour that fails SILENTLY if it regresses: a wrong config nesting renders
 * an empty settings form, a missing `animations` inline renders blank pack
 * thumbnails, and a width computed from the current window accumulates.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const settings = {
  petInstance: 'self',
  avatar: 'mochi',
  mode: 'quiet',
  catPreset: null,
  allowMcpServers: false,
  petName: 'Mochi',
  language: 'en',
  silentSubagents: false,
  activeAppearance: '',
  shortcuts: { toggleWindow: 'CommandOrControl+Shift+M', hideAll: 'CommandOrControl+Shift+H' },
  extraMcpServers: [] as unknown[],
  colorMaps: {} as Record<string, unknown>,
  customPresets: [] as unknown[],
  chatAlwaysOnTop: true,
}

const updateSettings = vi.fn(async (patch: Record<string, unknown>) => ({ ...settings, ...patch }))
const galleryGetPackDetail = vi.fn()
const setPanelWidth = vi.fn()

vi.mock('../api', () => ({
  getSettings: async () => settings,
  getStats: async () => ({}),
  updateSettings: (patch: Record<string, unknown>) => updateSettings(patch),
}))

vi.mock('../panel/panelBridge', () => ({
  galleryGetPackDetail: (id: string) => galleryGetPackDetail(id),
  galleryPackFileUrl: (packId: string, file: string) => `/packs/${packId}/${file}`,
  galleryListPacks: async () => [],
  galleryDeletePack: async () => true,
  gallerySetActive: async () => undefined,
  getWatchlistItems: async () => [],
  localFileUrl: (p: string) => `/api/file-raw?path=${p}`,
  openExternal: vi.fn(),
}))

vi.mock('../pet/petBridge', () => ({
  openAvatars: vi.fn(),
  openChat: vi.fn(),
  openSettings: vi.fn(),
  openMemories: vi.fn(),
  getMochiConfig: async () => settings,
  // The per-MACHINE prefs come from the SHELL, not from this gateway. `null` is
  // the no-shell answer (a plain browser tab), which is what keeps these config
  // assertions about the GATEWAY's copy — see the dedicated overlay tests below.
  machinePrefs: async () => null,
  // Mirrors `machinePrefs: null` above: these cases describe the no-shell tab.
  // The api re-exports this so the Settings pane can gate shell-only controls on
  // it instead of on a method's presence, which is always truthy here.
  hasShell: false,
  setPetInstance: vi.fn(async () => true),
  instancesList: vi.fn(async () => null),
}))

beforeEach(() => {
  vi.clearAllMocks()
  ;(window as unknown as { mochi?: unknown }).mochi = { setPanelWidth }
})

async function loadApi() {
  vi.resetModules()
  return (await import('../src/mochiApi')).api
}

describe('mochiApi config shape', () => {
  it('nests the flat store into the tree the vendored panel reads', async () => {
    const api = await loadApi()
    const cfg = (await api.getConfig()) as unknown as {
      mochi: Record<string, unknown>
      shortcuts: Record<string, string>
      window: { chatAlwaysOnTop: boolean }
    }
    // The vendored SettingsPanel reads exactly these paths; a flat payload would
    // render every field blank without throwing.
    expect(cfg.mochi.petName).toBe('Mochi')
    expect(cfg.shortcuts.toggleWindow).toBe('CommandOrControl+Shift+M')
    expect(cfg.window.chatAlwaysOnTop).toBe(true)
  })

  it('publishes the behaviour mode under BOTH spellings', async () => {
    const api = await loadApi()
    const cfg = (await api.getConfig()) as unknown as { mochi: Record<string, unknown> }
    // Upstream calls it activityMode; the builtin stores `mode`.
    expect(cfg.mochi.activityMode).toBe('quiet')
    expect(cfg.mochi.mode).toBe('quiet')
  })

  it('maps activityMode back onto mode when writing', async () => {
    const api = await loadApi()
    await api.updateConfig({ mochi: { activityMode: 'active' } })
    expect(updateSettings).toHaveBeenCalledWith({ mode: 'active' })
  })

  it('drops keys the builtin does not own instead of posting them', async () => {
    const api = await loadApi()
    await api.updateConfig({ mochi: { petName: 'Kiro', theme: 'mocha', soul: 'x' } })
    // The write route rejects unknown keys, so one unowned key must not cost the
    // user the rest of the save.
    expect(updateSettings).toHaveBeenCalledWith({ petName: 'Kiro' })
  })

  it('makes no request when a partial reduces to nothing owned', async () => {
    const api = await loadApi()
    await api.updateConfig({ mochi: { theme: 'sakura' } })
    expect(updateSettings).not.toHaveBeenCalled()
  })

  it('round-trips the background-activity keys (owned, not stripped)', async () => {
    // activityTier/bgModel are the SPEND axis — if either lands in the
    // UNOWNED strip list by accident, the tier picker silently stops saving.
    const api = await loadApi()
    await api.updateConfig({ mochi: { activityTier: 'economy', bgModel: 'small-1' } })
    expect(updateSettings).toHaveBeenCalledWith({ activityTier: 'economy', bgModel: 'small-1' })
  })

  it('never posts the per-MACHINE prefs to the gateway that served the window', async () => {
    // THE ONE-WAY DOOR. Every Mochi window loads FROM the gateway it shows and
    // this seam is same-origin, so posting `petInstance` here wrote it onto
    // whichever gateway served the window — while the shell read this machine's
    // copy. The choice looked saved (the UI read its own write back) and nothing
    // acted on it, leaving no surface that could move the pet home. Same argument
    // for `shortcuts`: one keyboard belongs to one computer.
    const api = await loadApi()
    await api.updateConfig({
      mochi: { petName: 'Kiro', petInstance: 'crew-remote' },
      shortcuts: { toggleWindow: 'Alt+Shift+M' },
    })
    expect(updateSettings).toHaveBeenCalledWith({ petName: 'Kiro' })
  })

  it('makes no request when a partial holds ONLY shell-owned prefs', async () => {
    // The corollary: a save that changed nothing else must not fire a write whose
    // entire payload the gateway does not own.
    const api = await loadApi()
    await api.updateConfig({ mochi: { petInstance: 'crew-remote' } })
    expect(updateSettings).not.toHaveBeenCalled()
  })
})

describe('mochiApi per-machine prefs overlay', () => {
  it("prefers the SHELL's pointer over the serving gateway's copy", async () => {
    // The read side of the same defect: a Settings window opened from a pet that
    // is showing a remote must not display that remote's idea of where the pet
    // belongs. Without the overlay the switcher highlights the wrong row and the
    // user's real choice is invisible.
    const bridge = await import('../pet/petBridge')
    vi.spyOn(bridge, 'machinePrefs').mockResolvedValue({
      petInstance: 'crew-shell',
      shortcuts: { toggleWindow: 'Alt+Shift+K' },
    })

    const api = await loadApi()
    const cfg = (await api.getConfig()) as unknown as {
      mochi: Record<string, unknown>
      shortcuts: Record<string, string>
    }
    expect(cfg.mochi.petInstance).toBe('crew-shell')
    expect(cfg.shortcuts.toggleWindow).toBe('Alt+Shift+K')
  })

  it('fills every accelerator even when the shell stores only some', async () => {
    // The shell's store deliberately OMITS an action it has no usable value for,
    // so `machinePrefs` legitimately returns a PARTIAL set. The renderer has no
    // fallback — displayAccelerator() splits the string — so a missing key throws
    // while rendering the Shortcuts pane and takes the whole panel down. Assigning
    // the partial object wholesale is what made that reachable.
    const bridge = await import('../pet/petBridge')
    vi.spyOn(bridge, 'machinePrefs').mockResolvedValue({
      petInstance: 'self',
      shortcuts: { toggleWindow: 'Alt+Shift+K' },
    })

    const api = await loadApi()
    const cfg = (await api.getConfig()) as unknown as { shortcuts: Record<string, string> }
    expect(cfg.shortcuts.toggleWindow).toBe('Alt+Shift+K')
    for (const action of ['screenCapture', 'hideAll']) {
      expect(typeof cfg.shortcuts[action]).toBe('string')
      expect(cfg.shortcuts[action].length).toBeGreaterThan(0)
    }
  })

  it('falls back to the gateway copy when there is no shell', async () => {
    // A plain browser tab IS the host gateway, so its own copy is the best
    // available answer — the pre-shell behaviour, preserved rather than replaced
    // by invented defaults. Set explicitly because the spy above outlives
    // clearAllMocks, and a fallback test that silently ran against a stubbed
    // pointer would prove nothing.
    const bridge = await import('../pet/petBridge')
    vi.spyOn(bridge, 'machinePrefs').mockResolvedValue(null)

    const api = await loadApi()
    const cfg = (await api.getConfig()) as unknown as {
      mochi: Record<string, unknown>
      shortcuts: Record<string, string>
    }
    expect(cfg.shortcuts.toggleWindow).toBe('CommandOrControl+Shift+M')
    expect(cfg.mochi.petInstance).toBe(settings.petInstance)
  })
})

describe('mochiApi pack detail', () => {
  it('inlines animation CONTENT, not filenames', async () => {
    galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'p1' },
      states: { idle: 'idle.svg' },
      moods: { happy: 'happy.json' },
    })
    const fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      text: async () => `<svg data-from="${url}" />`,
    }))
    vi.stubGlobal('fetch', fetchMock)

    const api = await loadApi()
    const detail = await api.galleryGetPackDetail('p1')

    // The vendored Avatars window reads `.content` directly; handing it the
    // manifest's FILENAME renders a blank thumbnail with no error.
    expect(detail?.animations.idle.content).toContain('/packs/p1/idle.svg')
    expect(detail?.animations.idle.format).toBe('svg')
    // Format is derived from the extension, so a Lottie is not fed to an <img>.
    expect(detail?.animations.happy.format).toBe('lottie')
  })

  it('keeps the rest of the pack when one slot is unreadable', async () => {
    galleryGetPackDetail.mockResolvedValue({
      meta: { id: 'p1' },
      states: { idle: 'idle.svg', walking: 'missing.svg' },
      moods: {},
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url.includes('missing') ? { ok: false } : { ok: true, text: async () => '<svg/>' },
      ),
    )
    const api = await loadApi()
    const detail = await api.galleryGetPackDetail('p1')
    expect(detail?.animations.idle).toBeDefined()
    expect(detail?.animations.walking).toBeUndefined()
  })

  it('returns null for a missing pack rather than an empty pack', async () => {
    galleryGetPackDetail.mockResolvedValue(null)
    const api = await loadApi()
    expect(await api.galleryGetPackDetail('nope')).toBeNull()
  })
})

describe('mochiApi side-panel width', () => {
  it('computes from a BASE plus visible rails, never cumulatively', async () => {
    const api = await loadApi()
    await api.toggleWatchPanel(true)
    expect(setPanelWidth).toHaveBeenLastCalledWith(320 + 280)
    await api.togglePinnedPanel(true)
    expect(setPanelWidth).toHaveBeenLastCalledWith(320 + 280 + 180)
    // Toggling back must return to the base, not to some accumulated value —
    // this is the width-corruption bug the original's main process guarded.
    await api.toggleWatchPanel(false)
    expect(setPanelWidth).toHaveBeenLastCalledWith(320 + 180)
    await api.togglePinnedPanel(false)
    expect(setPanelWidth).toHaveBeenLastCalledWith(320)
  })
})

describe('mochiApi gallery mutations', () => {
  it('reports a Result so the vendored UI can show a reason', async () => {
    const api = await loadApi()
    expect(await api.gallerySetActive('p1')).toEqual({ ok: true, value: null })
    expect(await api.galleryDelete('p1')).toEqual({ ok: true, value: null })
    // Unimplemented pack-authoring paths report a reason instead of silently
    // doing nothing.
    const exported = await api.galleryExport?.('p1')
    expect(exported).toMatchObject({ ok: false })
  })
})

describe('built-in pack', () => {
  it('is always first in the pack list', async () => {
    const api = await loadApi()
    const packs = await api.galleryListPacks()
    // Upstream registered default-mochi in memory at startup, so the gallery was
    // never empty. The vendored GalleryPanel renders an empty state otherwise.
    expect(packs[0].id).toBe('default-mochi')
    expect(packs[0].type).toBe('built-in')
  })

  it('serves its detail from the bundle, with content inlined', async () => {
    const api = await loadApi()
    const detail = await api.galleryGetPackDetail('default-mochi')
    // Every slot the pet resolver asks for must be present, or that state
    // renders blank with no error.
    for (const slot of ['idle', 'walking', 'thinking', 'working', 'error', 'offline']) {
      expect(detail?.animations[slot]?.content).toContain('<svg')
    }
    // Peek art is what makes edge-tuck work without a rotation hack.
    expect(detail?.animations.peeking).toBeDefined()
    expect(detail?.animations.peekThinking).toBeDefined()
  })

  it('needs no network for the built-in detail', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const api = await loadApi()
    await api.galleryGetPackDetail('default-mochi')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('openWidgetExternal sandboxing', () => {
  it('hosts untrusted widget HTML inside a sandboxed iframe, not as a top-level doc', async () => {
    const api = await loadApi()
    let captured = ''
    const openSpy = vi.fn()
    vi.stubGlobal('open', openSpy)
    vi.stubGlobal('URL', {
      createObjectURL: (b: Blob) => {
        // Blob text is sync-readable in jsdom via the FileReader-less shim; fall
        // back to capturing the parts through a Response for portability.
        ;(b as Blob)
          .text()
          .then((t) => {
            captured = t
          })
          .catch(() => undefined)
        return 'blob:mock'
      },
      revokeObjectURL: vi.fn(),
    })
    const widget = '<script>localStorage.clear()</script><img src="x" onerror="boom()">'
    api.openWidgetExternal(widget, 'My Widget')
    // let the blob.text() microtask resolve
    await Promise.resolve()
    await new Promise((r) => setTimeout(r, 0))
    expect(openSpy).toHaveBeenCalledWith('blob:mock', '_blank', 'noopener')
    // The widget is carried inside a sandbox="allow-scripts" (null-origin) iframe's
    // srcdoc attribute, NOT as the top-level document (which would inherit the
    // dashboard origin).
    expect(captured).toContain('<iframe sandbox="allow-scripts" srcdoc="')
    expect(captured).toContain('<title>My Widget</title>')
    // The widget's double-quotes are entity-escaped, so it cannot terminate
    // srcdoc="..." and escape into live top-level host markup — this is the
    // origin-isolation guarantee, and it comes from DOM serialization, not a
    // hand-rolled escaper.
    expect(captured).not.toContain('onerror="boom()"')
    expect(captured).toContain('onerror=&quot;boom()&quot;')
  })
})
