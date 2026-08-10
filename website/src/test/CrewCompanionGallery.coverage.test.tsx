/**
 * The avatar gallery's own behaviour, end to end through its bridge.
 *
 * `GalleryPanel` is the whole gallery window: the grid, the per-pack detail sheet,
 * the PetDex import dialog, and the editor overlay. Every one of those talks to the
 * desktop app through `galleryApi`, so the bridge is the only thing mocked here —
 * the component, its state machine, and the real i18n strings run for real.
 *
 * Three child surfaces are stubbed deliberately, not incidentally:
 *   - `PackEditor` / `SpriteImporter` are separate multi-step editors with their own
 *     tests; here they stand in as prop harnesses so the gallery's OWN save / cancel
 *     / refused-save wiring is what gets asserted.
 *   - `petdexImport` decodes a sprite sheet through `new Image()` + canvas, which
 *     never settles under happy-dom — an unmocked `firstFramePreview` leaves the
 *     import dialog stuck on "Looking it up…" forever.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'

import type { PackMeta } from '../apps/crew-companion/appearanceTypes'

// ── Bridge double ──────────────────────────────────────────────────────────

type Unsub = () => void

const mocks = vi.hoisted(() => {
  const listeners: {
    active?: (data?: { packId?: string }) => void
    packs?: () => void
    color?: (data: { packId: string; colorMap: Record<string, string> }) => void
    config?: (kiro?: { language?: string }) => void
  } = {}
  const api = {
    galleryListPacks: vi.fn(),
    galleryGetPackDetail: vi.fn(),
    gallerySetActive: vi.fn(),
    galleryDelete: vi.fn(),
    galleryExport: vi.fn(),
    gallerySaveSpritePack: vi.fn(),
    getCrewCompanionConfig: vi.fn(),
    presetsGetColorMap: vi.fn(),
    petdexFetch: vi.fn(),
    updateConfig: vi.fn(),
    openExternal: vi.fn(),
    closeGallery: vi.fn(),
    onGalleryActiveChanged: vi.fn((cb: (d?: { packId?: string }) => void): Unsub => {
      listeners.active = cb
      return () => { listeners.active = undefined }
    }),
    onGalleryPacksChanged: vi.fn((cb: () => void): Unsub => {
      listeners.packs = cb
      return () => { listeners.packs = undefined }
    }),
    onColorMapChanged: vi.fn((cb: (d: { packId: string; colorMap: Record<string, string> }) => void): Unsub => {
      listeners.color = cb
      return () => { listeners.color = undefined }
    }),
    onConfigUpdated: vi.fn((cb: (k?: { language?: string }) => void): Unsub => {
      listeners.config = cb
      return () => { listeners.config = undefined }
    }),
  }
  const firstFramePreview = vi.fn()
  const buildSpritePackData = vi.fn()
  return { api, listeners, firstFramePreview, buildSpritePackData }
})

vi.mock('../apps/crew-companion/petBridge', () => ({
  galleryApi: mocks.api,
  petBridge: mocks.api,
}))

vi.mock('../apps/crew-companion/petdexImport', () => ({
  firstFramePreview: mocks.firstFramePreview,
  buildSpritePackData: mocks.buildSpritePackData,
  STATE_MAP: [],
  RANDOM_MAP: [],
}))

vi.mock('../apps/crew-companion/PackEditor', () => ({
  PackEditor: ({ existingPack, onSave, onCancel }: {
    existingPack?: { name: string }
    onSave: () => void
    onCancel: () => void
  }) => (
    <div>
      <span>editor-for:{existingPack ? existingPack.name : 'new-pack'}</span>
      <button onClick={onSave}>editor-save</button>
      <button onClick={onCancel}>editor-cancel</button>
    </div>
  ),
}))

vi.mock('../apps/crew-companion/SpriteImporter', () => ({
  SpriteImporter: ({ existingPack, onDone, onCancel, saveError }: {
    existingPack?: { id: string }
    onDone: (r: { overwriteId?: string }) => void
    onCancel: () => void
    saveError?: string | null
  }) => (
    <div>
      <span>sprite-importer-for:{existingPack ? existingPack.id : 'new-pack'}</span>
      {saveError ? <span>sprite-save-error:{saveError}</span> : null}
      <button onClick={() => onDone({ overwriteId: existingPack?.id })}>sprite-done</button>
      <button onClick={onCancel}>sprite-cancel</button>
    </div>
  ),
}))

// Imported AFTER the mocks are registered.
const { GalleryPanel } = await import('../apps/crew-companion/GalleryPanel')

// ── Fixtures ───────────────────────────────────────────────────────────────

/** The backend's canonical built-in id (appearances.py `DEFAULT_PACK`). */
const BUILTIN_ID = 'kiro-ghost'

const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'

const builtin: PackMeta = {
  id: BUILTIN_ID,
  name: 'Kiro',
  author: 'Kiro Crew',
  description: 'the bundled ghost',
  type: 'built-in',
  format: 'svg',
  thumbnail: '',
}

const custom: PackMeta = {
  id: 'boba-pack',
  name: 'Boba',
  author: 'community',
  description: 'a very round cat',
  type: 'custom',
  format: 'svg',
  thumbnail: '',
}

const spritePack: PackMeta = {
  id: 'pixel-pack',
  name: 'Pixel Pal',
  author: 'pixelsmith',
  description: '',
  type: 'custom',
  format: 'sprite',
  thumbnail: '',
}

/** A detail payload shaped the way the bridge returns it. */
const detailFor = (
  meta: PackMeta,
  animations: Record<string, string> = { idle: SVG },
  sprite?: { frameWidth?: number; frameHeight?: number; fps?: number },
) => ({ meta, animations, sprite })

const api = mocks.api

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listeners.active = undefined
  mocks.listeners.packs = undefined
  mocks.listeners.color = undefined
  mocks.listeners.config = undefined

  api.galleryListPacks.mockResolvedValue([builtin, custom])
  api.galleryGetPackDetail.mockImplementation((id: string) => {
    if (id === custom.id) return Promise.resolve(detailFor(custom))
    if (id === spritePack.id) return Promise.resolve(detailFor(spritePack, { idle: SVG }, { fps: 6 }))
    return Promise.resolve(detailFor(builtin, {}))
  })
  api.getCrewCompanionConfig.mockResolvedValue({ activeAppearance: BUILTIN_ID, language: 'en' })
  api.presetsGetColorMap.mockResolvedValue({})
  api.gallerySetActive.mockResolvedValue({ ok: true })
  api.galleryDelete.mockResolvedValue({ ok: true })
  api.galleryExport.mockResolvedValue({ ok: true })
  api.gallerySaveSpritePack.mockResolvedValue({ ok: true, packId: 'saved-pack' })
  api.firstFramePreview?.mockReset?.()
  mocks.firstFramePreview.mockResolvedValue('data:image/png;base64,AAA')
  mocks.buildSpritePackData.mockResolvedValue({ name: 'Boba', assignments: {} })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

/** Mount and wait for the initial two-call load to settle. */
async function mount() {
  const utils = render(<GalleryPanel />)
  await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
  return utils
}

/** The grid card for a pack — a role=button div wrapping the pack's name. */
const cardFor = (name: string): HTMLElement => {
  const grid = document.querySelector('[style*="grid-template-columns"]') as HTMLElement
  const card = within(grid).getByText(name).closest('[role="button"]')
  if (!card) throw new Error(`no card for ${name}`)
  return card as HTMLElement
}

/** The dim layer a dialog is painted on — its click target for "outside". */
const overlayOf = (dialog: HTMLElement) => dialog.parentElement as HTMLElement

/**
 * The Edit label carries an orphaned U+FE0F variation selector in the catalogue
 * (`"\uFE0F Edit"`), a leftover from a stripped emoji — so it is matched loosely.
 */
const EDIT = /Edit/

/** Open a custom pack's detail sheet through its Manage affordance. */
async function openDetail(name: string) {
  const manage = cardFor(name).querySelector('button[aria-label="Manage"]')
  fireEvent.click(manage as HTMLElement)
  await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
}

/**
 * The built-in ghost's body is a bundled asset the thumbnail re-fetches in order to
 * recolour it. Serve a known one-colour body so the recolour is observable.
 */
const GHOST_BODY = '<svg xmlns="http://www.w3.org/2000/svg"><path fill="#FFFFFF"/></svg>'
const stubGhostBody = () =>
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(GHOST_BODY, { status: 200 }))

/** The built-in card's art, decoded back out of its data URI. */
const builtinArt = () =>
  decodeURIComponent((cardFor('Kiro').querySelector('img') as HTMLImageElement).getAttribute('src') ?? '')

// ── The grid ───────────────────────────────────────────────────────────────

describe('gallery grid', () => {
  it('shows a loading line until the packs and the active id have both landed', async () => {
    let release: (packs: PackMeta[]) => void = () => {}
    api.galleryListPacks.mockReturnValue(new Promise<PackMeta[]>((res) => { release = res }))

    render(<GalleryPanel />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText('Boba')).not.toBeInTheDocument()

    release([builtin, custom])
    await waitFor(() => expect(screen.getByText('Boba')).toBeInTheDocument())
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('renders one card per pack with its name and author', async () => {
    await mount()
    expect(screen.getByText('Kiro')).toBeInTheDocument()
    expect(screen.getByText('Kiro Crew')).toBeInTheDocument()
    expect(screen.getByText('Boba')).toBeInTheDocument()
    expect(screen.getByText('community')).toBeInTheDocument()
  })

  it('badges only the active pack, and invites a tap on the others', async () => {
    await mount()
    expect(cardFor('Kiro')).toHaveTextContent('Active')
    expect(cardFor('Kiro')).toHaveAttribute('aria-pressed', 'true')
    expect(cardFor('Boba')).toHaveTextContent('Tap to use')
    expect(cardFor('Boba')).toHaveAttribute('aria-pressed', 'false')
  })

  it('offers Manage on custom packs only — a built-in has nothing to manage', async () => {
    await mount()
    expect(cardFor('Boba').querySelector('button[aria-label="Manage"]')).not.toBeNull()
    expect(cardFor('Kiro').querySelector('button[aria-label="Manage"]')).toBeNull()
  })

  it('surfaces a failed pack list as a dismissable banner', async () => {
    api.galleryListPacks.mockRejectedValue(new Error('the pack directory went away'))
    await mount()

    expect(screen.getByText('the pack directory went away')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByText('the pack directory went away')).not.toBeInTheDocument()
  })

  it('falls back to the built-in as active when the config read fails', async () => {
    api.getCrewCompanionConfig.mockRejectedValue(new Error('no config'))
    await mount()
    expect(cardFor('Kiro')).toHaveTextContent('Active')
  })

  it('recolours the built-in thumbnail when a colour map is stored for it', async () => {
    stubGhostBody()
    api.presetsGetColorMap.mockResolvedValue({ '#FFFFFF': '#00ff00' })
    await mount()
    // The bundled body is fetched and rewritten, so the card's art is the recoloured
    // SVG rather than the asset PetAvatar would otherwise point straight at.
    await waitFor(() => expect(builtinArt()).toContain('fill="#00ff00"'))
  })
})

// ── Switching avatar straight from a card ───────────────────────────────────

describe('applying a pack from its card', () => {
  it('switches to the tapped pack and says so', async () => {
    await mount()
    fireEvent.click(cardFor('Boba'))

    await waitFor(() => expect(api.gallerySetActive).toHaveBeenCalledWith('boba-pack'))
    expect(await screen.findByText('Avatar switched')).toBeInTheDocument()
    expect(cardFor('Boba')).toHaveTextContent('Active')
  })

  it('applies on Enter, so the grid is reachable without a mouse', async () => {
    await mount()
    fireEvent.keyDown(cardFor('Boba'), { key: 'Enter' })
    await waitFor(() => expect(api.gallerySetActive).toHaveBeenCalledWith('boba-pack'))
  })

  it('ignores a tap on the pack that is already active', async () => {
    await mount()
    fireEvent.click(cardFor('Kiro'))
    fireEvent.keyDown(cardFor('Kiro'), { key: ' ' })
    await waitFor(() => expect(screen.getByText('Kiro')).toBeInTheDocument())
    expect(api.gallerySetActive).not.toHaveBeenCalled()
  })

  it('reports a refused switch and leaves the old pack active', async () => {
    api.gallerySetActive.mockResolvedValue({ ok: false, error: 'that pack is broken' })
    await mount()
    fireEvent.click(cardFor('Boba'))

    expect(await screen.findByText('that pack is broken')).toBeInTheDocument()
    expect(cardFor('Kiro')).toHaveTextContent('Active')
  })

  it('reports a thrown switch through the shared error text', async () => {
    api.gallerySetActive.mockRejectedValue(new Error('bridge is down'))
    await mount()
    fireEvent.click(cardFor('Boba'))
    expect(await screen.findByText('bridge is down')).toBeInTheDocument()
  })
})

// ── Broadcasts from the other windows ──────────────────────────────────────

describe('gallery broadcasts', () => {
  it('moves the Active badge when another window switches the avatar', async () => {
    await mount()
    expect(cardFor('Kiro')).toHaveTextContent('Active')

    await act(async () => { mocks.listeners.active?.({ packId: custom.id }) })
    expect(cardFor('Boba')).toHaveTextContent('Active')
    expect(cardFor('Kiro')).toHaveTextContent('Tap to use')
  })

  it('re-reads the config when the broadcast carries no pack id', async () => {
    await mount()
    api.getCrewCompanionConfig.mockResolvedValue({ activeAppearance: custom.id })

    await act(async () => { mocks.listeners.active?.({}) })
    await waitFor(() => expect(cardFor('Boba')).toHaveTextContent('Active'))
  })

  it('refetches the grid when the pack set changes elsewhere', async () => {
    await mount()
    api.galleryListPacks.mockResolvedValue([builtin, custom, spritePack])

    await act(async () => { mocks.listeners.packs?.() })
    await waitFor(() => expect(screen.getByText('Pixel Pal')).toBeInTheDocument())
  })

  it('picks up a colour-map change for the built-in and ignores one for another pack', async () => {
    stubGhostBody()
    await mount()
    await act(async () => {
      mocks.listeners.color?.({ packId: 'some-other-pack', colorMap: { '#FFFFFF': '#00ff00' } })
    })
    expect(builtinArt()).not.toContain('#00ff00')

    await act(async () => {
      mocks.listeners.color?.({ packId: BUILTIN_ID, colorMap: { '#FFFFFF': '#00ff00' } })
    })
    await waitFor(() => expect(builtinArt()).toContain('fill="#00ff00"'))
  })

  it('survives a language broadcast without losing the grid', async () => {
    await mount()
    await act(async () => { mocks.listeners.config?.({ language: 'ja' }) })
    expect(screen.getByText('Boba')).toBeInTheDocument()
  })
})

// ── The detail sheet ───────────────────────────────────────────────────────

describe('pack detail sheet', () => {
  it('shows the pack identity, its slots, and the custom-pack actions', async () => {
    await mount()
    await openDetail('Boba')

    const sheet = screen.getByRole('dialog')
    expect(sheet).toHaveTextContent('community · SVG')
    expect(sheet).toHaveTextContent('a very round cat')
    expect(sheet).toHaveTextContent('State Animations')
    expect(sheet).toHaveTextContent('Idle')
    expect(screen.getByRole('button', { name: 'Apply' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('lists only the optional groups the pack actually provides', async () => {
    api.galleryGetPackDetail.mockResolvedValue(
      detailFor(custom, { idle: SVG, done: SVG, walking: SVG }),
    )
    await mount()
    await openDetail('Boba')

    const sheet = screen.getByRole('dialog')
    expect(sheet).toHaveTextContent('Status')
    expect(sheet).toHaveTextContent('Done')
    expect(sheet).toHaveTextContent('Random')
    expect(sheet).toHaveTextContent('Walking')
    // Nothing legacy in this pack, so that heading must not appear at all.
    expect(sheet).not.toHaveTextContent('Legacy')
  })

  it('renders the legacy group for a pack authored before the split', async () => {
    api.galleryGetPackDetail.mockResolvedValue(
      detailFor(custom, { idle: SVG, thinking: SVG }),
    )
    await mount()
    await openDetail('Boba')
    expect(screen.getByRole('dialog')).toHaveTextContent('Legacy')
    expect(screen.getByRole('dialog')).toHaveTextContent('Thinking')
  })

  it('toggles closed when Manage is pressed a second time', async () => {
    await mount()
    const manage = cardFor('Boba').querySelector('button[aria-label="Manage"]') as HTMLElement
    await openDetail('Boba')
    fireEvent.click(manage)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('closes on the ✕ and on a click outside the sheet', async () => {
    await mount()
    await openDetail('Boba')
    // The window's own ✕ carries the same label, so this is scoped to the sheet.
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close (Esc)' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.closeGallery).not.toHaveBeenCalled()

    await openDetail('Boba')
    fireEvent.click(overlayOf(screen.getByRole('dialog')))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('reports a detail fetch that fails and opens no sheet', async () => {
    api.galleryGetPackDetail.mockImplementation((id: string) =>
      id === custom.id
        ? Promise.reject(new Error('that manifest is corrupt'))
        : Promise.resolve(detailFor(builtin, {})),
    )
    await mount()
    fireEvent.click(cardFor('Boba').querySelector('button[aria-label="Manage"]') as HTMLElement)

    expect(await screen.findByText('that manifest is corrupt')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens no sheet, and blames nothing, when the pack has no detail at all', async () => {
    await mount()
    api.galleryGetPackDetail.mockResolvedValue(null)
    fireEvent.click(cardFor('Boba').querySelector('button[aria-label="Manage"]') as HTMLElement)

    await waitFor(() => expect(api.galleryGetPackDetail).toHaveBeenLastCalledWith('boba-pack'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Could not load that avatar')).not.toBeInTheDocument()
  })

  it('draws a neutral thumbnail and an empty slot for a pack with no idle art', async () => {
    api.galleryGetPackDetail.mockResolvedValue(detailFor(custom, {}))
    await mount()
    // No art to draw: a blank box, never an emoji stand-in.
    expect(cardFor('Boba').querySelector('img')).toBeNull()

    await openDetail('Boba')
    const sheet = screen.getByRole('dialog')
    expect(sheet).toHaveTextContent('Idle')
    expect(sheet).toHaveTextContent('—')
  })

  it('applies from the sheet and then drops the Apply button', async () => {
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(api.gallerySetActive).toHaveBeenCalledWith('boba-pack'))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument())
    expect(screen.getByRole('dialog')).toHaveTextContent('Active')
  })

  it('reports a refused apply from the sheet', async () => {
    api.gallerySetActive.mockResolvedValue({ ok: false, error: 'refused by the app' })
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByText('refused by the app')).toBeInTheDocument()
  })

  it('confirms a successful export and reports a refused one', async () => {
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Export' }))
    expect(await screen.findByText('Exported successfully')).toBeInTheDocument()

    api.galleryExport.mockResolvedValue({ ok: false, error: 'no write permission' })
    fireEvent.click(screen.getByRole('button', { name: 'Export' }))
    expect(await screen.findByText('no write permission')).toBeInTheDocument()
  })

  it('stays quiet when the export dialog is cancelled', async () => {
    api.galleryExport.mockResolvedValue(null)
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Export' }))

    await waitFor(() => expect(api.galleryExport).toHaveBeenCalled())
    expect(screen.queryByText('Exported successfully')).not.toBeInTheDocument()
    expect(screen.queryByText('Export failed')).not.toBeInTheDocument()
  })
})

// ── Deleting a pack ────────────────────────────────────────────────────────

describe('deleting a pack', () => {
  it('asks first, and does nothing when the user declines', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    expect(confirmSpy).toHaveBeenCalledWith('Delete appearance pack "Boba"? This cannot be undone.')
    expect(api.galleryDelete).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('deletes an inactive pack and closes the sheet', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(api.galleryDelete).toHaveBeenCalledWith('boba-pack'))
    // Inactive pack: no need to repoint the active reference.
    expect(api.gallerySetActive).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.galleryListPacks).toHaveBeenCalledTimes(2)
  })

  it('repoints the active reference to the built-in BEFORE deleting the active pack', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.getCrewCompanionConfig.mockResolvedValue({ activeAppearance: custom.id })
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(api.galleryDelete).toHaveBeenCalledWith('boba-pack'))
    expect(api.gallerySetActive).toHaveBeenCalledWith(BUILTIN_ID)
    // Order is the whole point: a failed delete must never leave the config
    // pointing at a pack that is already gone.
    const switched = api.gallerySetActive.mock.invocationCallOrder[0]
    const deleted = api.galleryDelete.mock.invocationCallOrder[0]
    expect(switched).toBeLessThan(deleted)
  })

  it('aborts the delete when the pre-delete switch fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.getCrewCompanionConfig.mockResolvedValue({ activeAppearance: custom.id })
    api.gallerySetActive.mockResolvedValue({ ok: false })
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText('Delete failed')).toBeInTheDocument()
    expect(api.galleryDelete).not.toHaveBeenCalled()
  })

  it('reports a refused delete', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.galleryDelete.mockResolvedValue(null)
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText('Delete failed')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('reports a thrown delete', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.galleryDelete.mockRejectedValue(new Error('the file is locked'))
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByText('the file is locked')).toBeInTheDocument()
  })
})

// ── The editor overlay ─────────────────────────────────────────────────────

describe('editor overlay', () => {
  it('opens a blank editor from "Make your own" and cancels back to the grid', async () => {
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Make your own' }))

    expect(screen.getByText('editor-for:new-pack')).toBeInTheDocument()
    // The gallery stays mounted behind the overlay.
    expect(screen.getByText('Boba')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'editor-cancel' }))
    expect(screen.queryByText('editor-for:new-pack')).not.toBeInTheDocument()
  })

  it('confirms a brand-new pack with the create toast and refetches', async () => {
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Make your own' }))
    fireEvent.click(screen.getByRole('button', { name: 'editor-save' }))

    expect(await screen.findByText('Pack created')).toBeInTheDocument()
    await waitFor(() => expect(api.galleryListPacks).toHaveBeenCalledTimes(2))
  })

  it('edits an existing SVG pack and reports the save as an update', async () => {
    await mount()
    await openDetail('Boba')
    fireEvent.click(screen.getByRole('button', { name: EDIT }))

    // The sheet gives way to the editor, carrying the pack being edited.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByText('editor-for:Boba')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'editor-save' }))
    expect(await screen.findByText('Pack saved')).toBeInTheDocument()
  })

  it('routes a sprite pack to the sprite importer instead of the editor', async () => {
    api.galleryListPacks.mockResolvedValue([builtin, spritePack])
    await mount()
    await openDetail('Pixel Pal')
    fireEvent.click(screen.getByRole('button', { name: EDIT }))

    expect(await screen.findByText('sprite-importer-for:pixel-pack')).toBeInTheDocument()
    // Sprite mode REPLACES the gallery, so the grid is gone.
    expect(screen.queryByText('Pixel Pal')).not.toBeInTheDocument()
  })

  it('keeps the sprite importer open, with its message, when the save is refused', async () => {
    api.galleryListPacks.mockResolvedValue([builtin, spritePack])
    api.gallerySaveSpritePack.mockResolvedValue({ ok: false, error: 'that sheet is not a grid' })
    await mount()
    await openDetail('Pixel Pal')
    fireEvent.click(screen.getByRole('button', { name: EDIT }))
    fireEvent.click(await screen.findByRole('button', { name: 'sprite-done' }))

    // The importer must NOT unmount: the gallery's error banner cannot be seen
    // from sprite mode, so a refusal there would otherwise vanish with the edits.
    expect(await screen.findByText('sprite-save-error:that sheet is not a grid')).toBeInTheDocument()
    expect(screen.getByText('sprite-importer-for:pixel-pack')).toBeInTheDocument()
  })

  it('returns to the grid on a successful sprite save and re-applies the active pack', async () => {
    api.galleryListPacks.mockResolvedValue([builtin, spritePack])
    api.getCrewCompanionConfig.mockResolvedValue({ activeAppearance: spritePack.id })
    await mount()
    await openDetail('Pixel Pal')
    fireEvent.click(screen.getByRole('button', { name: EDIT }))
    fireEvent.click(await screen.findByRole('button', { name: 'sprite-done' }))

    expect(await screen.findByText('Pack saved')).toBeInTheDocument()
    expect(screen.getByText('Pixel Pal')).toBeInTheDocument()
    // Overwriting the pack that is currently worn re-applies it.
    expect(api.gallerySetActive).toHaveBeenCalledWith(spritePack.id)
  })

  it('leaves the sprite importer on cancel without saving', async () => {
    api.galleryListPacks.mockResolvedValue([builtin, spritePack])
    await mount()
    await openDetail('Pixel Pal')
    fireEvent.click(screen.getByRole('button', { name: EDIT }))
    fireEvent.click(await screen.findByRole('button', { name: 'sprite-cancel' }))

    expect(await screen.findByText('Pixel Pal')).toBeInTheDocument()
    expect(api.gallerySaveSpritePack).not.toHaveBeenCalled()
  })
})

// ── Import from PetDex ─────────────────────────────────────────────────────

describe('importing a pet from PetDex', () => {
  /** Drive the debounce deterministically: no wall clock, no arbitrary sleeps. */
  const tick = async (ms: number) => {
    await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
  }

  /** Mount + open the import dialog under fake timers (no waitFor: see below). */
  async function openImport() {
    // RTL's waitFor cannot drive vitest's fake timers, so this path settles the
    // component with explicit act() flushes instead.
    render(<GalleryPanel />)
    await tick(0)
    await tick(0)
    fireEvent.click(screen.getByRole('button', { name: 'Import from PetDex' }))
    expect(screen.getByText('Import a pet')).toBeInTheDocument()
  }

  beforeEach(() => {
    vi.useFakeTimers()
    api.petdexFetch.mockResolvedValue({
      ok: true,
      slug: 'boba',
      displayName: 'Boba',
      author: 'someone',
      description: 'round cat',
      spriteBase64: 'AAAA',
    })
  })

  it('looks the pet up only after typing settles, then shows what it found', async () => {
    await openImport()
    const input = screen.getByRole('textbox')

    fireEvent.change(input, { target: { value: 'bo' } })
    fireEvent.change(input, { target: { value: 'boba' } })
    // Before the debounce elapses there must be no lookup at all.
    await tick(400)
    expect(api.petdexFetch).not.toHaveBeenCalled()

    await tick(100)
    // One call for the settled value, not one per keystroke.
    expect(api.petdexFetch).toHaveBeenCalledTimes(1)
    expect(api.petdexFetch).toHaveBeenCalledWith('boba')
    expect(screen.getByText('Found · by someone')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use this pet' })).toBeEnabled()
  })

  it('does not look up a value too short to be a slug', async () => {
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'b' } })
    await tick(1000)
    expect(api.petdexFetch).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Use this pet' })).toBeDisabled()
  })

  it('shows the miss message when the pet is unknown, and keeps Use disabled', async () => {
    api.petdexFetch.mockResolvedValue({ ok: false })
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'nope' } })
    await tick(500)

    expect(screen.getByText('Could not find that pet')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use this pet' })).toBeDisabled()
  })

  it('reports a lookup that threw', async () => {
    api.petdexFetch.mockRejectedValue(new Error('petdex is unreachable'))
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'boba' } })
    await tick(500)
    expect(screen.getByText('petdex is unreachable')).toBeInTheDocument()
  })

  it('still resolves the pet when its preview frame cannot be decoded', async () => {
    mocks.firstFramePreview.mockRejectedValue(new Error('bad sheet'))
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'boba' } })
    await tick(500)
    // The preview is optional; the identity card still appears.
    expect(screen.getByText('Found · by someone')).toBeInTheDocument()
  })

  it('adds the pet, wears it, and closes the dialog', async () => {
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'boba' } })
    await tick(500)

    fireEvent.click(screen.getByRole('button', { name: 'Use this pet' }))
    await tick(0)

    expect(mocks.buildSpritePackData).toHaveBeenCalledWith('AAAA', {
      displayName: 'Boba', author: 'someone', description: 'round cat',
    })
    expect(api.gallerySetActive).toHaveBeenCalledWith('saved-pack')
    expect(screen.getByText('Boba is now your companion')).toBeInTheDocument()
    expect(screen.queryByText('Import a pet')).not.toBeInTheDocument()
  })

  it('confirms with Enter once a pet has resolved', async () => {
    await openImport()
    const input = screen.getByRole('textbox')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(api.gallerySaveSpritePack).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'boba' } })
    await tick(500)
    fireEvent.keyDown(input, { key: 'Enter' })
    await tick(0)
    expect(api.gallerySaveSpritePack).toHaveBeenCalled()
  })

  it('keeps the dialog open with the reason when the save is refused', async () => {
    api.gallerySaveSpritePack.mockResolvedValue({ ok: false, error: 'disk is full' })
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'boba' } })
    await tick(500)
    fireEvent.click(screen.getByRole('button', { name: 'Use this pet' }))
    await tick(0)

    expect(screen.getByText('disk is full')).toBeInTheDocument()
    expect(screen.getByText('Import a pet')).toBeInTheDocument()
  })

  it('reports a save that threw', async () => {
    mocks.buildSpritePackData.mockRejectedValue(new Error('could not build the pack'))
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'boba' } })
    await tick(500)
    fireEvent.click(screen.getByRole('button', { name: 'Use this pet' }))
    await tick(0)
    expect(screen.getByText('could not build the pack')).toBeInTheDocument()
  })

  it('closes on Cancel and clears what was typed', async () => {
    await openImport()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'boba' } })
    await tick(500)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByText('Import a pet')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Import from PetDex' }))
    expect(screen.getByRole('textbox')).toHaveValue('')
  })

  it('closes on a click outside the dialog', async () => {
    await openImport()
    fireEvent.click(overlayOf(screen.getByRole('dialog')))
    expect(screen.queryByText('Import a pet')).not.toBeInTheDocument()
  })

  it('backs out on Escape — which today also closes the whole window', async () => {
    await openImport()
    fireEvent.keyDown(window, { key: 'Escape' })
    await tick(0)
    expect(screen.queryByText('Import a pet')).not.toBeInTheDocument()
    // Pinning current behaviour, not endorsing it: the dialog and the gallery
    // window BOTH listen for Escape on `window` and neither stops propagation,
    // so backing out of the dialog tears down the window under it.
    expect(api.closeGallery).toHaveBeenCalled()
  })

  it('offers the PetDex gallery from inside the dialog', async () => {
    await openImport()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'petdex.dev' }))
    expect(api.openExternal).toHaveBeenCalledWith('https://petdex.dev')
  })
})

// ── Window chrome ──────────────────────────────────────────────────────────

describe('the toast', () => {
  it('fades itself out and then leaves, without a click', async () => {
    vi.useFakeTimers()
    const tick = async (ms: number) => {
      await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
    }
    render(<GalleryPanel />)
    await tick(0)
    await tick(0)
    fireEvent.click(cardFor('Boba'))
    await tick(0)

    const toast = screen.getByText('Avatar switched')
    expect(toast.style.animation).toContain('toastIn')

    await tick(2000)
    expect(screen.getByText('Avatar switched').style.animation).toContain('toastOut')

    await tick(500)
    expect(screen.queryByText('Avatar switched')).not.toBeInTheDocument()
  })
})

describe('gallery window chrome', () => {
  it('closes the window from its ✕', async () => {
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Close (Esc)' }))
    expect(api.closeGallery).toHaveBeenCalled()
  })

  it('closes the window on Escape — a frameless window has no OS button', async () => {
    await mount()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(api.closeGallery).toHaveBeenCalled()
  })

  it('opens the PetDex gallery from the footer', async () => {
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'petdex.dev' }))
    expect(api.openExternal).toHaveBeenCalledWith('https://petdex.dev')
  })
})
