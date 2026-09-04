/**
 * Mochi `GalleryPanel` — first tests for
 * `apps/mochi/src/renderer/GalleryPanel.tsx`.
 *
 * The gallery window is the whole appearance-pack surface: the card grid with
 * its per-format thumbnails, the detail sheet (state / mood grids, apply,
 * export, edit, delete), the header's four entry points into the editors, the
 * toast + error banners, and the three main-process broadcasts it subscribes
 * to. None of that was covered.
 *
 * What is mocked, and why:
 *   - `mochiApi` is the component's ONLY seam to the main process, so it is the
 *     single behavioural double. Every state machine below is driven by what
 *     that seam returns, including its failure shapes.
 *   - `PackEditor` / `SpriteImporter` / `PetdexImporter` / `ColorCustomizerPanel`
 *     are separate multi-step surfaces with their own tests; here they stand in
 *     as prop harnesses so the gallery's OWN navigation, prefill and
 *     refused-save wiring is what gets asserted.
 *   - `SpriteRenderer` / `LottieRenderer` decode through `new Image()` and
 *     lottie-web, neither of which settles under happy-dom. Stubbed so the
 *     thumbnail BRANCH SELECTION (svg / lottie / sprite / no-art) and the props
 *     handed down (frame size, fps, mirror transform) are what is checked.
 *
 * Real code that is deliberately NOT mocked: `toDataUri`,
 * `applySvgColorMap`, `resolveActivePackId`, and the i18n catalog — the colour
 * recolouring of the default-mochi thumbnail is asserted through the real
 * transform, and every label is looked up with `i18nT` so a copy edit cannot
 * silently break these tests.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'

import type { PackMeta } from '../apps/mochi/src/shared/appearanceTypes'
import { i18nT } from '../i18n/t'

// ── Doubles ────────────────────────────────────────────────────────────────

type Fn = ReturnType<typeof vi.fn>

const H = vi.hoisted(() => {
  const listeners: {
    active?: (data?: { packId?: string }) => void
    packs?: () => void
    color?: (data: { packId: string; colorMap: Record<string, string> }) => void
  } = {}
  const offs = { active: 0, packs: 0, color: 0 }
  /** Payload the SpriteImporter stub hands back through `onDone`. */
  const spriteDone: { payload: Record<string, unknown> } = { payload: {} }
  const api: Record<string, unknown> = {}

  const install = () => {
    for (const k of Object.keys(api)) delete api[k]
    api.galleryListPacks = vi.fn()
    api.galleryGetPackDetail = vi.fn()
    api.getMochiConfig = vi.fn()
    api.presetsGetColorMap = vi.fn()
    api.gallerySetActive = vi.fn()
    api.galleryExport = vi.fn()
    api.galleryDelete = vi.fn()
    api.galleryImportBundle = vi.fn()
    api.gallerySaveSpritePack = vi.fn()
    api.onGalleryActiveChanged = vi.fn((cb: (d?: { packId?: string }) => void) => {
      listeners.active = cb
      return () => { offs.active += 1 }
    })
    api.onGalleryPacksChanged = vi.fn((cb: () => void) => {
      listeners.packs = cb
      return () => { offs.packs += 1 }
    })
    api.onColorMapChanged = vi.fn((cb: (d: { packId: string; colorMap: Record<string, string> }) => void) => {
      listeners.color = cb
      return () => { offs.color += 1 }
    })
    offs.active = 0
    offs.packs = 0
    offs.color = 0
    spriteDone.payload = {}
  }

  return { api, listeners, offs, spriteDone, install }
})

vi.mock('../apps/mochi/src/mochiApi', () => ({ api: H.api }))

vi.mock('../apps/mochi/src/renderer/PackEditor', () => ({
  PackEditor: ({ existingPack, onSave, onCancel }: {
    existingPack?: { name: string }
    onSave: () => void
    onCancel: () => void
  }) => (
    <div data-testid="pack-editor">
      <span>editor-for:{existingPack ? existingPack.name : 'new-pack'}</span>
      <div><button onClick={onSave}>editor-save</button></div>
      <div><button onClick={onCancel}>editor-cancel</button></div>
    </div>
  ),
}))

vi.mock('../apps/mochi/src/renderer/PetdexImporter', () => ({
  PetdexImporter: ({ onReady, onUseFile, onCancel }: {
    onReady: (next: { name: string }) => void
    onUseFile: () => void
    onCancel: () => void
  }) => (
    <div data-testid="petdex-importer">
      <div><button onClick={() => onReady({ name: 'Bulbasaur' })}>petdex-ready</button></div>
      <div><button onClick={onUseFile}>petdex-use-file</button></div>
      <div><button onClick={onCancel}>petdex-cancel</button></div>
    </div>
  ),
}))

vi.mock('../apps/mochi/src/renderer/SpriteImporter', () => ({
  SpriteImporter: ({ existingPack, prefill, saveError, onDone, onCancel }: {
    existingPack?: { name: string }
    prefill?: { name: string }
    saveError?: string | null
    onDone: (result: Record<string, unknown>) => void
    onCancel: () => void
  }) => (
    <div data-testid="sprite-importer">
      <span>sprite-for:{existingPack ? existingPack.name : 'new-pack'}</span>
      <span data-testid="sprite-prefill">{prefill ? prefill.name : 'no-prefill'}</span>
      {saveError ? <span data-testid="sprite-save-error">{saveError}</span> : null}
      <div><button onClick={() => onDone(H.spriteDone.payload)}>sprite-done</button></div>
      <div><button onClick={onCancel}>sprite-cancel</button></div>
    </div>
  ),
}))

vi.mock('../apps/mochi/src/renderer/ColorCustomizer', () => ({
  ColorCustomizerPanel: ({ idleSvgContent }: { idleSvgContent: string }) => (
    <div data-testid="color-panel">{idleSvgContent}</div>
  ),
}))

vi.mock('../apps/mochi/src/renderer/SpriteRenderer', () => ({
  SpriteRenderer: ({ src, frameWidth, frameHeight, fps, displaySize }: {
    src: string
    frameWidth: number
    frameHeight: number
    fps?: number
    displaySize?: number
  }) => (
    <div
      data-testid="sprite-renderer"
      data-src={src}
      data-fw={String(frameWidth)}
      data-fh={String(frameHeight)}
      data-fps={String(fps)}
      data-size={String(displaySize)}
    />
  ),
}))

vi.mock('../apps/mochi/src/renderer/LottieRenderer', () => ({
  LottieRenderer: ({ animationData, width }: { animationData: string; width?: number }) => (
    <div data-testid="lottie-renderer" data-anim={animationData} data-w={String(width)} />
  ),
}))

const { GalleryPanel } = await import('../apps/mochi/src/renderer/GalleryPanel')

// ── Fixtures ───────────────────────────────────────────────────────────────

const ORANGE = '#F5A623'
const SVG = `<svg xmlns="http://www.w3.org/2000/svg"><circle r="4" fill="${ORANGE}" /></svg>`
const LOTTIE = '{"v":"5.7.1","layers":[]}'

const svg = (content = SVG) => ({ content, format: 'svg' as const })
const lottie = (content = LOTTIE) => ({ content, format: 'lottie' as const })
const sprite = (content: string) => ({ content, format: 'sprite' as const })

const meta = (over: Partial<PackMeta> & { id: string; name: string }): PackMeta => ({
  author: 'Kiro',
  description: '',
  type: 'built-in',
  format: 'svg',
  thumbnail: '',
  ...over,
})

const MOCHI = meta({ id: 'default-mochi', name: 'Mochi Cat', description: 'a soft round cat' })
const GHOST = meta({ id: 'kiro-ghost', name: 'Kiro Ghost', format: 'lottie' })
const PIXEL = meta({ id: 'pixel-bot', name: 'Pixel Bot', author: 'Zed', type: 'custom', format: 'sprite' })
const DRAWN = meta({ id: 'hand-drawn', name: 'Hand Drawn', author: 'Zed', type: 'custom' })
const BLANK = meta({ id: 'no-art', name: 'No Art', author: 'Nobody', type: 'custom' })

const PACKS: PackMeta[] = [MOCHI, GHOST, PIXEL, DRAWN, BLANK]

interface Detail {
  meta: PackMeta
  animations: Record<string, { content: string; format: 'svg' | 'lottie' | 'sprite' }>
  sprite?: { frameWidth: number; frameHeight: number; fps: number; flipX?: boolean }
  flipX?: boolean
}

/** `offline` is deliberately absent so the "missing required slot" cell renders. */
const DETAILS: Record<string, Detail | null> = {
  'default-mochi': {
    meta: MOCHI,
    animations: {
      idle: svg(),
      walking: svg(),
      thinking: svg(),
      working: svg(),
      error: svg(),
      peeking: svg(),
      happy: svg(),
      // Present but empty: the pack declares the slot and ships no art, which is
      // the ambiguity AnimThumbnail warns about.
      sleepy: svg(''),
    },
  },
  'kiro-ghost': {
    meta: GHOST,
    animations: { idle: lottie(), walking: lottie() },
    flipX: true,
  },
  'pixel-bot': {
    meta: PIXEL,
    animations: { idle: sprite('QUJD'), walking: sprite('data:image/png;base64,REVG') },
    sprite: { frameWidth: 48, frameHeight: 24, fps: 9, flipX: true },
  },
  'hand-drawn': { meta: DRAWN, animations: { idle: svg(), walking: svg() } },
  'no-art': { meta: BLANK, animations: {} },
}

// ── Helpers ────────────────────────────────────────────────────────────────

const fn = (name: string): Fn => H.api[name] as Fn

/** The silenced `console.warn` spy installed for every test. */
let warnSpy: Fn

const t = (key: string, vars?: Record<string, unknown>) => i18nT(key, vars)

function cardFor(name: string): HTMLElement {
  const all = Array.from(document.querySelectorAll('[aria-pressed]')) as HTMLElement[]
  const hit = all.find((el) => el.textContent?.includes(name))
  if (!hit) throw new Error(`no pack card for ${name}`)
  return hit
}

/**
 * The overlay and the sheet, in that order.
 *
 * Queried by the explicit attribute rather than `getAllByRole('presentation')`:
 * every `alt=""` thumbnail is ALSO role=presentation, so the role query returns
 * the images too and "the sheet" would resolve to a thumbnail.
 */
function sheetNodes(): HTMLElement[] {
  return Array.from(document.querySelectorAll('div[role="presentation"]')) as HTMLElement[]
}

function sheet(): HTMLElement {
  const nodes = sheetNodes()
  if (nodes.length === 0) throw new Error('detail sheet is not open')
  return nodes[nodes.length - 1]
}

/** The decoded `src` of a card's thumbnail image. */
function thumbSrc(name: string): string {
  const img = cardFor(name).querySelector('img')
  if (!img) throw new Error(`no thumbnail image for ${name}`)
  return decodeURIComponent(img.getAttribute('src') ?? '')
}

async function mount() {
  const view = render(<GalleryPanel />)
  await waitFor(() => expect(screen.queryByText(t('apps.mochi.gallery.loading'))).toBeNull())
  return view
}

async function open(name: string) {
  fireEvent.click(cardFor(name))
  await waitFor(() => expect(sheetNodes().length).toBeGreaterThan(0))
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  // The empty-slot fixture makes AnimThumbnail warn by design; silenced here so
  // only the test that asserts the warning cares about it.
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {}) as unknown as Fn
  H.install()
  fn('galleryListPacks').mockResolvedValue(PACKS)
  fn('galleryGetPackDetail').mockImplementation((id: string) =>
    Promise.resolve(DETAILS[id] ?? null))
  fn('getMochiConfig').mockResolvedValue({ activeAppearance: 'default-mochi' })
  fn('presetsGetColorMap').mockResolvedValue({})
  fn('gallerySetActive').mockResolvedValue({ ok: true })
  fn('galleryExport').mockResolvedValue({ ok: true })
  fn('galleryDelete').mockResolvedValue({ ok: true })
  fn('galleryImportBundle').mockResolvedValue({ ok: true })
  fn('gallerySaveSpritePack').mockResolvedValue({ ok: true, packId: 'pixel-bot' })
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

// ── Grid ───────────────────────────────────────────────────────────────────

describe('GalleryPanel — grid', () => {
  it('shows the loading line first, then one card per pack', async () => {
    render(<GalleryPanel />)
    expect(screen.getByText(t('apps.mochi.gallery.loading'))).toBeTruthy()

    await waitFor(() => expect(screen.queryByText(t('apps.mochi.gallery.loading'))).toBeNull())
    for (const p of PACKS) expect(cardFor(p.name)).toBeTruthy()
    expect(within(cardFor('Pixel Bot')).getByText('Zed')).toBeTruthy()
    expect(within(cardFor('Pixel Bot')).getByText(t('apps.mochi.gallery.custom'))).toBeTruthy()
    expect(within(cardFor('Mochi Cat')).getByText(t('apps.mochi.gallery.built_in'))).toBeTruthy()
  })

  it('marks only the configured pack as active', async () => {
    await mount()
    expect(within(cardFor('Mochi Cat')).getByText(t('apps.mochi.gallery.active'))).toBeTruthy()
    expect(within(cardFor('Kiro Ghost')).queryByText(t('apps.mochi.gallery.active'))).toBeNull()
  })

  it('renders an svg thumbnail as an inline data URI', async () => {
    await mount()
    const img = cardFor('Hand Drawn').querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('data:image/svg+xml,')
    expect(thumbSrc('Hand Drawn')).toContain(ORANGE)
  })

  it('renders a lottie thumbnail through LottieRenderer, mirrored by the pack flag', async () => {
    await mount()
    const stub = within(cardFor('Kiro Ghost')).getByTestId('lottie-renderer')
    expect(stub.getAttribute('data-anim')).toBe(LOTTIE)
    expect(stub.getAttribute('data-w')).toBe('80')
    expect((stub.parentElement as HTMLElement).style.transform).toBe('scaleX(-1)')
  })

  it('renders a sprite thumbnail with the pack sprite config and its flip', async () => {
    await mount()
    const stub = within(cardFor('Pixel Bot')).getByTestId('sprite-renderer')
    expect(stub.getAttribute('data-src')).toBe('data:image/png;base64,QUJD')
    expect(stub.getAttribute('data-fw')).toBe('48')
    expect(stub.getAttribute('data-fh')).toBe('24')
    expect(stub.getAttribute('data-fps')).toBe('9')
    expect((stub.parentElement as HTMLElement).style.transform).toBe('scaleX(-1)')
  })

  it('falls back to the cat glyph when the pack has no idle art', async () => {
    await mount()
    const card = cardFor('No Art')
    expect(card.querySelector('img')).toBeNull()
    expect(within(card).queryByTestId('sprite-renderer')).toBeNull()
    expect(card.querySelector('svg')).toBeTruthy()
  })

  it('keeps rendering when one pack detail lookup rejects', async () => {
    fn('galleryGetPackDetail').mockImplementation((id: string) =>
      id === 'kiro-ghost' ? Promise.reject(new Error('nope')) : Promise.resolve(DETAILS[id] ?? null))
    await mount()
    expect(cardFor('Kiro Ghost')).toBeTruthy()
    expect(within(cardFor('Kiro Ghost')).queryByTestId('lottie-renderer')).toBeNull()
    expect(screen.queryByText('nope')).toBeNull()
  })

  it('shows the empty state when there are no packs', async () => {
    fn('galleryListPacks').mockResolvedValue([])
    await mount()
    expect(screen.getByText(t('apps.mochi.gallery.empty'))).toBeTruthy()
  })

  it('banners a pack-list failure and lets it be dismissed', async () => {
    fn('galleryListPacks').mockRejectedValue(new Error('ipc down'))
    await mount()
    expect(screen.getByText('ipc down')).toBeTruthy()

    fireEvent.click(screen.getByLabelText(t('apps.mochi.chatPanel.dismiss')))
    expect(screen.queryByText('ipc down')).toBeNull()
  })

  it('falls back to the built-in pack when the config read fails', async () => {
    fn('getMochiConfig').mockRejectedValue(new Error('no config'))
    await mount()
    expect(within(cardFor('Mochi Cat')).getByText(t('apps.mochi.gallery.active'))).toBeTruthy()
    expect(fn('presetsGetColorMap')).not.toHaveBeenCalled()
  })

  it('recolours only the default-mochi thumbnail from the stored colour map', async () => {
    fn('presetsGetColorMap').mockResolvedValue({ [ORANGE]: '#00FF00' })
    await mount()
    const mochi = thumbSrc('Mochi Cat')
    const drawn = thumbSrc('Hand Drawn')
    expect(mochi).toContain('#00FF00')
    expect(drawn).toContain(ORANGE)
  })

  it('ignores an empty stored colour map', async () => {
    await mount()
    expect(thumbSrc('Mochi Cat')).toContain(ORANGE)
  })
})

// ── Detail sheet ───────────────────────────────────────────────────────────

describe('GalleryPanel — detail sheet', () => {
  it('opens on card click with metadata and the state grid', async () => {
    await mount()
    await open('Mochi Cat')

    const panel = sheet()
    expect(within(panel).getByText('a soft round cat')).toBeTruthy()
    expect(within(panel).getByText('Kiro · SVG')).toBeTruthy()
    expect(within(panel).getByText(t('apps.mochi.gallery.states'))).toBeTruthy()
    expect(within(panel).getByText(t('apps.mochi.state.idle'))).toBeTruthy()
    // Optional states only appear when the pack ships them.
    expect(within(panel).getByText(t('apps.mochi.state.peeking'))).toBeTruthy()
    expect(within(panel).queryByText(t('apps.mochi.state.peekThinking'))).toBeNull()
    // A required slot with no art still gets a labelled cell.
    expect(within(panel).getByText(t('apps.mochi.state.offline'))).toBeTruthy()
    expect(within(panel).getByText('—')).toBeTruthy()
  })

  it('lists mood animations only for packs that ship them', async () => {
    await mount()
    await open('Mochi Cat')
    expect(within(sheet()).getByText(t('apps.mochi.gallery.moods'))).toBeTruthy()
    expect(within(sheet()).getByText(t('apps.mochi.mood.happy'))).toBeTruthy()

    fireEvent.click(within(sheet()).getByLabelText(t('apps.mochi.watchPanel.close')))
    await open('Hand Drawn')
    expect(within(sheet()).queryByText(t('apps.mochi.gallery.moods'))).toBeNull()
  })

  it('warns instead of silently drawing an empty box for a declared-but-empty slot', async () => {
    await mount()
    await open('Mochi Cat')

    expect(warnSpy).toHaveBeenCalledWith(
      '[mochi] animation thumbnail has no usable art',
      expect.objectContaining({ format: 'svg', contentLength: 0 }),
    )
  })

  it('passes an already-encoded sprite frame through untouched', async () => {
    await mount()
    await open('Pixel Bot')
    const stubs = within(sheet()).getAllByTestId('sprite-renderer')
    const walking = stubs.find((s) => s.getAttribute('data-src') === 'data:image/png;base64,REVG')
    expect(walking).toBeTruthy()
    // Sprite-only flip: this pack carries no pack-level flag, so `sprite.flipX` wins.
    expect((stubs[0].parentElement as HTMLElement).style.transform).toBe('scaleX(-1)')
  })

  it('toggles the sheet closed when the same card is clicked again', async () => {
    await mount()
    await open('Kiro Ghost')
    fireEvent.click(cardFor('Kiro Ghost'))
    await waitFor(() => expect(sheetNodes()).toHaveLength(0))
  })

  it('opens from the keyboard on Enter and on Space', async () => {
    await mount()
    fireEvent.keyDown(cardFor('Hand Drawn'), { key: 'Enter' })
    await waitFor(() => expect(sheetNodes().length).toBeGreaterThan(0))
    fireEvent.click(within(sheet()).getByLabelText(t('apps.mochi.watchPanel.close')))

    fireEvent.keyDown(cardFor('Hand Drawn'), { key: ' ' })
    await waitFor(() => expect(sheetNodes().length).toBeGreaterThan(0))
  })

  it('ignores unrelated keys on a card', async () => {
    await mount()
    fireEvent.keyDown(cardFor('Hand Drawn'), { key: 'a' })
    expect(sheetNodes()).toHaveLength(0)
  })

  it('closes on the overlay click but not on a click inside the sheet', async () => {
    await mount()
    await open('Hand Drawn')
    const nodes = sheetNodes()
    fireEvent.click(nodes[nodes.length - 1])
    expect(sheetNodes().length).toBeGreaterThan(0)

    fireEvent.click(sheetNodes()[0])
    await waitFor(() => expect(sheetNodes()).toHaveLength(0))
  })

  it('banners a null detail response without opening the sheet', async () => {
    await mount()
    fn('galleryGetPackDetail').mockResolvedValue(null)
    fireEvent.click(cardFor('Hand Drawn'))

    await waitFor(() => expect(
      screen.getByText(t('apps.mochi.errors.load_pack_detail'))).toBeTruthy())
    expect(sheetNodes()).toHaveLength(0)
  })

  it('banners a rejected detail response with the thrown message', async () => {
    await mount()
    fn('galleryGetPackDetail').mockRejectedValue(new Error('detail exploded'))
    fireEvent.click(cardFor('Hand Drawn'))

    await waitFor(() => expect(screen.getByText('detail exploded')).toBeTruthy())
    expect(sheetNodes()).toHaveLength(0)
  })

  it('offers the colour customiser only for default-mochi', async () => {
    await mount()
    await open('Mochi Cat')
    const toggle = within(sheet()).getByText(t('apps.mochi.color.customize_btn'))
    expect(within(sheet()).getByTestId('color-panel').textContent).toBe(SVG)

    fireEvent.click(toggle)
    expect(within(sheet()).getByText(t('apps.mochi.color.hide_btn'))).toBeTruthy()
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.color.hide_btn')))
    expect(within(sheet()).getByText(t('apps.mochi.color.customize_btn'))).toBeTruthy()
  })

  it('hides the colour customiser for every other pack', async () => {
    await mount()
    await open('Hand Drawn')
    expect(within(sheet()).queryByText(t('apps.mochi.color.customize_btn'))).toBeNull()
    expect(within(sheet()).queryByTestId('color-panel')).toBeNull()
  })

  it('shows apply for an inactive pack and the custom-pack actions only for custom packs', async () => {
    await mount()
    await open('Kiro Ghost')
    expect(within(sheet()).getByText(t('apps.mochi.gallery.apply'))).toBeTruthy()
    expect(within(sheet()).queryByText(t('apps.mochi.gallery.export'))).toBeNull()
    expect(within(sheet()).queryByText(t('apps.mochi.gallery.delete'))).toBeNull()

    fireEvent.click(within(sheet()).getByLabelText(t('apps.mochi.watchPanel.close')))
    await open('Mochi Cat')
    // Active pack: no apply button, and the active badge in the header instead.
    expect(within(sheet()).queryByText(t('apps.mochi.gallery.apply'))).toBeNull()
    expect(within(sheet()).getByText(t('apps.mochi.gallery.active'))).toBeTruthy()
  })
})

// ── Actions ────────────────────────────────────────────────────────────────

describe('GalleryPanel — actions', () => {
  it('marks the pack active only after the write is confirmed', async () => {
    await mount()
    await open('Kiro Ghost')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.apply')))

    await waitFor(() => expect(
      within(sheet()).getByText(t('apps.mochi.gallery.active'))).toBeTruthy())
    expect(fn('gallerySetActive')).toHaveBeenCalledWith('kiro-ghost')
    expect(within(sheet()).queryByText(t('apps.mochi.gallery.apply'))).toBeNull()
  })

  it('does not mark a pack active when the write is refused', async () => {
    fn('gallerySetActive').mockResolvedValue({ ok: false, error: 'read-only disk' })
    await mount()
    await open('Kiro Ghost')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.apply')))

    await waitFor(() => expect(screen.getByText('read-only disk')).toBeTruthy())
    expect(within(sheet()).getByText(t('apps.mochi.gallery.apply'))).toBeTruthy()
  })

  it('falls back to the generic apply error when the refusal carries no message', async () => {
    fn('gallerySetActive').mockResolvedValue({ ok: false })
    await mount()
    await open('Kiro Ghost')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.apply')))

    await waitFor(() => expect(
      screen.getByText(t('apps.mochi.errors.apply_pack'))).toBeTruthy())
  })

  it('banners a thrown apply', async () => {
    fn('gallerySetActive').mockRejectedValue(new Error('apply threw'))
    await mount()
    await open('Kiro Ghost')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.apply')))

    await waitFor(() => expect(screen.getByText('apply threw')).toBeTruthy())
  })

  it('toasts a successful export and clears the toast on its own', async () => {
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.export')))

    await waitFor(() => expect(
      screen.getByText(t('apps.mochi.gallery.export_success'))).toBeTruthy())
    expect(fn('galleryExport')).toHaveBeenCalledWith('pixel-bot')

    act(() => { vi.advanceTimersByTime(2100) })
    const toast = screen.getByText(t('apps.mochi.gallery.export_success'))
    expect(toast.style.animation).toContain('toastOut')

    act(() => { vi.advanceTimersByTime(500) })
    expect(screen.queryByText(t('apps.mochi.gallery.export_success'))).toBeNull()
  })

  it('stays silent when the export dialog is cancelled', async () => {
    fn('galleryExport').mockResolvedValue(null)
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.export')))

    await waitFor(() => expect(fn('galleryExport')).toHaveBeenCalled())
    expect(screen.queryByText(t('apps.mochi.gallery.export_success'))).toBeNull()
    expect(screen.queryByText(t('apps.mochi.errors.export'))).toBeNull()
  })

  it('banners a refused and a thrown export', async () => {
    fn('galleryExport').mockResolvedValue({ ok: false, error: 'no write access' })
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.export')))
    await waitFor(() => expect(screen.getByText('no write access')).toBeTruthy())

    fn('galleryExport').mockRejectedValue(new Error('export threw'))
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.export')))
    await waitFor(() => expect(screen.getByText('export threw')).toBeTruthy())
  })

  it('does not delete when the confirm dialog is dismissed', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.delete')))

    expect(confirmSpy).toHaveBeenCalledWith(
      t('apps.mochi.gallery.delete_confirm', { name: 'Pixel Bot' }))
    expect(fn('galleryDelete')).not.toHaveBeenCalled()
    expect(sheetNodes().length).toBeGreaterThan(0)
  })

  it('closes the sheet and refetches after a confirmed delete', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.delete')))

    await waitFor(() => expect(sheetNodes()).toHaveLength(0))
    expect(fn('galleryDelete')).toHaveBeenCalledWith('pixel-bot')
    expect(fn('galleryListPacks')).toHaveBeenCalledTimes(2)
  })

  it('keeps the sheet open and banners a refused delete', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fn('galleryDelete').mockResolvedValue({ ok: false, error: 'pack in use' })
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.delete')))

    await waitFor(() => expect(screen.getByText('pack in use')).toBeTruthy())
    expect(sheetNodes().length).toBeGreaterThan(0)
    expect(fn('galleryListPacks')).toHaveBeenCalledTimes(1)
  })

  it('banners a thrown delete', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fn('galleryDelete').mockRejectedValue(new Error('delete threw'))
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.delete')))

    await waitFor(() => expect(screen.getByText('delete threw')).toBeTruthy())
  })

  it('toasts a successful bundle import, and reports its failures', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.gallery.import_bundle')))
    await waitFor(() => expect(
      screen.getByText(t('apps.mochi.gallery.import_success'))).toBeTruthy())

    fn('galleryImportBundle').mockResolvedValue({ ok: false, error: 'bad zip' })
    fireEvent.click(screen.getByText(t('apps.mochi.gallery.import_bundle')))
    await waitFor(() => expect(screen.getByText('bad zip')).toBeTruthy())

    fn('galleryImportBundle').mockRejectedValue(new Error('import threw'))
    fireEvent.click(screen.getByText(t('apps.mochi.gallery.import_bundle')))
    await waitFor(() => expect(screen.getByText('import threw')).toBeTruthy())
  })

  it('stays silent when the import dialog is cancelled', async () => {
    fn('galleryImportBundle').mockResolvedValue(null)
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.gallery.import_bundle')))

    await waitFor(() => expect(fn('galleryImportBundle')).toHaveBeenCalled())
    expect(screen.queryByText(t('apps.mochi.gallery.import_success'))).toBeNull()
  })
})

// ── Editor navigation ──────────────────────────────────────────────────────

describe('GalleryPanel — editor navigation', () => {
  it('opens a blank editor from the header and toasts a created pack', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.gallery.create_new')))
    expect(screen.getByText('editor-for:new-pack')).toBeTruthy()

    fireEvent.click(screen.getByText('editor-save'))
    await waitFor(() => expect(
      screen.getByText(t('apps.mochi.editor.create_success'))).toBeTruthy())
    expect(screen.queryByTestId('pack-editor')).toBeNull()
    expect(fn('galleryListPacks')).toHaveBeenCalledTimes(2)
  })

  it('returns to the gallery when the editor is cancelled', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.gallery.create_new')))
    fireEvent.click(screen.getByText('editor-cancel'))

    expect(screen.queryByTestId('pack-editor')).toBeNull()
    expect(cardFor('Mochi Cat')).toBeTruthy()
    expect(fn('galleryListPacks')).toHaveBeenCalledTimes(1)
  })

  it('edits an svg pack in the pack editor and toasts a saved pack', async () => {
    await mount()
    await open('Hand Drawn')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.edit')))

    expect(screen.getByText('editor-for:Hand Drawn')).toBeTruthy()
    expect(sheetNodes()).toHaveLength(0)

    fireEvent.click(screen.getByText('editor-save'))
    await waitFor(() => expect(
      screen.getByText(t('apps.mochi.editor.save_success'))).toBeTruthy())
  })

  it('edits a sprite pack in the sprite importer instead', async () => {
    await mount()
    await open('Pixel Bot')
    fireEvent.click(within(sheet()).getByText(t('apps.mochi.gallery.edit')))

    expect(screen.getByText('sprite-for:Pixel Bot')).toBeTruthy()
    expect(screen.getByTestId('sprite-prefill').textContent).toBe('no-prefill')
  })

  it('opens a blank sprite importer from the header and cancels back', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    expect(screen.getByText('sprite-for:new-pack')).toBeTruthy()

    fireEvent.click(screen.getByText('sprite-cancel'))
    expect(screen.queryByTestId('sprite-importer')).toBeNull()
    expect(cardFor('Mochi Cat')).toBeTruthy()
  })

  it('carries a petdex pick into the sprite importer as prefill', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.petdex.title')))
    fireEvent.click(screen.getByText('petdex-ready'))

    expect(screen.getByTestId('sprite-prefill').textContent).toBe('Bulbasaur')
  })

  it('enters the sprite importer with no prefill when the petdex user brings a file', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.petdex.title')))
    fireEvent.click(screen.getByText('petdex-use-file'))

    expect(screen.getByTestId('sprite-prefill').textContent).toBe('no-prefill')
  })

  it('returns to the gallery when the petdex importer is cancelled', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.petdex.title')))
    fireEvent.click(screen.getByText('petdex-cancel'))

    expect(screen.queryByTestId('petdex-importer')).toBeNull()
    expect(cardFor('Mochi Cat')).toBeTruthy()
  })

  it('stays in the sprite importer when the save is refused, then leaves once it lands', async () => {
    fn('gallerySaveSpritePack').mockResolvedValue({ ok: false, error: 'sheet too tall' })
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    fireEvent.click(screen.getByText('sprite-done'))

    await waitFor(() => expect(
      screen.getByTestId('sprite-save-error').textContent).toBe('sheet too tall'))
    expect(screen.getByTestId('sprite-importer')).toBeTruthy()

    fn('gallerySaveSpritePack').mockResolvedValue({ ok: true, packId: 'new-sheet' })
    fireEvent.click(screen.getByText('sprite-done'))
    await waitFor(() => expect(screen.queryByTestId('sprite-importer')).toBeNull())
    expect(screen.getByText(t('apps.mochi.editor.create_success'))).toBeTruthy()
  })

  it('reports the generic sprite-save error when the refusal carries no message', async () => {
    fn('gallerySaveSpritePack').mockResolvedValue({ ok: false })
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    fireEvent.click(screen.getByText('sprite-done'))

    await waitFor(() => expect(screen.getByTestId('sprite-save-error').textContent)
      .toBe(t('apps.mochi.errors.create_sprite_pack')))
  })

  it('treats an absent save channel as a refused save', async () => {
    H.api.gallerySaveSpritePack = undefined
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    fireEvent.click(screen.getByText('sprite-done'))

    await waitFor(() => expect(screen.getByTestId('sprite-save-error')).toBeTruthy())
    expect(screen.getByTestId('sprite-importer')).toBeTruthy()
  })

  it('re-applies the pack when the saved sheet overwrites the active one', async () => {
    H.spriteDone.payload = { overwriteId: 'default-mochi' }
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    fireEvent.click(screen.getByText('sprite-done'))

    await waitFor(() => expect(fn('gallerySetActive')).toHaveBeenCalledWith('default-mochi'))
    await waitFor(() => expect(screen.queryByTestId('sprite-importer')).toBeNull())
  })

  it('re-applies when the newly assigned pack id is the active one', async () => {
    fn('gallerySaveSpritePack').mockResolvedValue({ ok: true, packId: 'default-mochi' })
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    fireEvent.click(screen.getByText('sprite-done'))

    await waitFor(() => expect(fn('gallerySetActive')).toHaveBeenCalledWith('default-mochi'))
  })

  it('does not re-apply when the saved pack is not the active one', async () => {
    await mount()
    fireEvent.click(screen.getByText(t('apps.mochi.sprite.title')))
    fireEvent.click(screen.getByText('sprite-done'))

    await waitFor(() => expect(screen.queryByTestId('sprite-importer')).toBeNull())
    expect(fn('gallerySetActive')).not.toHaveBeenCalled()
  })
})

// ── Broadcasts ─────────────────────────────────────────────────────────────

describe('GalleryPanel — broadcasts', () => {
  it('moves the active badge when the main process names the new pack', async () => {
    await mount()
    act(() => { H.listeners.active?.({ packId: 'kiro-ghost' }) })

    expect(within(cardFor('Kiro Ghost')).getByText(t('apps.mochi.gallery.active'))).toBeTruthy()
    expect(within(cardFor('Mochi Cat')).queryByText(t('apps.mochi.gallery.active'))).toBeNull()
    expect(fn('getMochiConfig')).toHaveBeenCalledTimes(1)
  })

  it('re-reads the config when the broadcast carries no pack id', async () => {
    await mount()
    fn('getMochiConfig').mockResolvedValue({ activeAppearance: 'pixel-bot' })
    act(() => { H.listeners.active?.({}) })

    await waitFor(() => expect(
      within(cardFor('Pixel Bot')).getByText(t('apps.mochi.gallery.active'))).toBeTruthy())
  })

  it('refetches the grid when the pack set changes', async () => {
    await mount()
    fn('galleryListPacks').mockResolvedValue([...PACKS, meta({ id: 'newbie', name: 'Newbie' })])
    act(() => { H.listeners.packs?.() })

    await waitFor(() => expect(cardFor('Newbie')).toBeTruthy())
  })

  it('recolours default-mochi live, and reverts when the map is emptied', async () => {
    await mount()
    act(() => { H.listeners.color?.({ packId: 'default-mochi', colorMap: { [ORANGE]: '#123456' } }) })
    await waitFor(() => expect(thumbSrc('Mochi Cat')).toContain('#123456'))

    act(() => { H.listeners.color?.({ packId: 'default-mochi', colorMap: {} }) })
    await waitFor(() => expect(thumbSrc('Mochi Cat')).toContain(ORANGE))
  })

  it('ignores a colour map broadcast for another pack', async () => {
    await mount()
    act(() => { H.listeners.color?.({ packId: 'hand-drawn', colorMap: { [ORANGE]: '#123456' } }) })

    const src = thumbSrc('Hand Drawn')
    expect(src).toContain(ORANGE)
    expect(src).not.toContain('#123456')
  })

  it('renders when the colour-map channel is absent from the bridge', async () => {
    H.api.onColorMapChanged = undefined
    await mount()
    expect(cardFor('Mochi Cat')).toBeTruthy()
  })

  it('unsubscribes from every channel on unmount', async () => {
    const view = await mount()
    view.unmount()

    expect(H.offs.active).toBe(1)
    expect(H.offs.packs).toBe(1)
    expect(H.offs.color).toBe(1)
  })
})
