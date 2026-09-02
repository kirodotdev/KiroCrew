/**
 * Mochi sprite importer — first tests for `apps/mochi/src/renderer/SpriteImporter.tsx`.
 *
 * The importer is the whole "turn a sprite sheet into an appearance pack" screen:
 * pick a sheet, adopt (or detect) its grid, slice it into rows, map each row onto
 * a pet state or mood, and hand the result to its owner. None of that had a test,
 * so every behaviour below is pinned here for the first time — including the two
 * paths that must never silently guess: a sheet that matches the petdex.dev grid
 * announces the pre-fill, and one that does not falls back to frame detection.
 *
 * Two boundaries are stubbed, deliberately:
 *
 *   - **Image decode + canvas.** The importer measures the sheet with
 *     `new Image()` and slices it through a 2d canvas. happy-dom never decodes an
 *     image (`load` never fires, `naturalWidth` stays 0) and has no canvas, so the
 *     rows would never exist. A fake `Image` reports the pixel size the test asks
 *     for and hands the test control of when `load` lands (`settle()`), while the
 *     canvas returns the alpha map the test built. Everything above that seam —
 *     the grid maths, `detectFrameSize`, the row slicing loop, the assignment
 *     state machine, the dirty check — runs for real.
 *   - **`SpriteRenderer`**, which drives a `requestAnimationFrame` loop off its own
 *     image decode and has its own tests. Here it stands in as a marker so the
 *     importer's OWN row/slot wiring is what gets asserted.
 *
 * No test depends on real elapsed time or on an animation frame landing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act, cleanup, within } from '@testing-library/react'

import type { PackMeta } from '../apps/mochi/src/shared/appearanceTypes'

// ── Bridge double ──────────────────────────────────────────────────────────

const mocks = vi.hoisted(() => ({
  api: {
    importSpriteFile: vi.fn<() => Promise<unknown>>(),
    galleryGetPackDetail: vi.fn<(packId: string) => Promise<unknown>>(),
    galleryReadPackFile: vi.fn<(packId: string, file: string) => Promise<string | null>>(),
  },
}))

vi.mock('../apps/mochi/src/mochiApi', () => ({ api: mocks.api }))

vi.mock('../apps/mochi/src/renderer/SpriteRenderer', () => ({
  SpriteRenderer: ({ src, frameWidth, frameHeight, fps }: {
    src: string
    frameWidth: number
    frameHeight: number
    fps?: number
  }) => (
    <span data-strip={src} data-frame={`${frameWidth}x${frameHeight}@${fps ?? 0}`} />
  ),
}))

import { SpriteImporter, type SpritePrefillInput } from '../apps/mochi/src/renderer/SpriteImporter'

const api = mocks.api

// ── Image / canvas doubles ─────────────────────────────────────────────────

interface AlphaMap {
  width: number
  height: number
  data: Uint8ClampedArray
}

/** Pixel size the next decoded image reports. */
let sheetSize = { width: 64, height: 64 }
/** What `getImageData` hands to frame detection. */
let sheetPixels: AlphaMap = alphaOpaque(64, 64)
/** Images whose `load` has not been delivered yet. */
const awaitingDecode: FakeImage[] = []
/** Makes each sliced row's data URI distinguishable. */
let sliceSeq = 0

/**
 * An `Image` that decodes only when the test says so.
 *
 * Both consumers in the importer are supported: the file-pick path assigns
 * `onload`, the slicing effect uses `addEventListener('load')` and removes it on
 * cleanup — so a stale image from a superseded slice pass fires into nothing,
 * exactly as in the browser.
 */
class FakeImage {
  naturalWidth = 0
  naturalHeight = 0
  onload: (() => void) | null = null
  private readonly handlers = new Set<() => void>()
  private value = ''

  get src(): string { return this.value }

  set src(next: string) {
    this.value = next
    this.naturalWidth = sheetSize.width
    this.naturalHeight = sheetSize.height
    awaitingDecode.push(this)
  }

  addEventListener(type: string, cb: () => void): void {
    if (type === 'load') this.handlers.add(cb)
  }

  removeEventListener(type: string, cb: () => void): void {
    if (type === 'load') this.handlers.delete(cb)
  }

  finishLoad(): void {
    this.onload?.()
    for (const cb of [...this.handlers]) cb()
  }
}

/** A sheet with no transparent gaps at all: detection can learn nothing from it. */
function alphaOpaque(width: number, height: number): AlphaMap {
  const data = new Uint8ClampedArray(width * height * 4)
  for (let i = 3; i < data.length; i += 4) data[i] = 255
  return { width, height, data }
}

/**
 * A sheet ruled by 1px transparent grid lines every `pitch` px, under `topPad`
 * fully transparent rows — the shape `detectFrameSize` is built to read.
 */
function alphaRuled(width: number, height: number, pitch: number, topPad: number): AlphaMap {
  const data = new Uint8ClampedArray(width * height * 4)
  for (let y = topPad; y < height; y++) {
    if ((y - topPad) % pitch === pitch - 1) continue
    for (let x = 0; x < width; x++) {
      if (x % pitch === pitch - 1) continue
      data[(y * width + x) * 4 + 3] = 255
    }
  }
  return { width, height, data }
}

interface CanvasSeam {
  getContext: unknown
  toDataURL: unknown
}
const canvasProto = HTMLCanvasElement.prototype as unknown as CanvasSeam
let realGetContext: unknown
let realToDataURL: unknown

/**
 * Deliver pending promises and image decodes, alternating: each decode commits
 * state whose effect creates the NEXT image (pick -> measure -> slice), so one
 * pass is never enough.
 */
async function settle(passes = 4): Promise<void> {
  for (let i = 0; i < passes; i++) {
    await act(async () => { await Promise.resolve() })
    const batch = awaitingDecode.splice(0, awaitingDecode.length)
    if (batch.length === 0) continue
    await act(async () => { for (const img of batch) img.finishLoad() })
  }
}

// ── Query helpers ──────────────────────────────────────────────────────────

/** The input sitting under a `NumberField` / pack-info label. */
function fieldInput(label: string): HTMLInputElement {
  const input = screen.getByText(label).parentElement?.querySelector('input')
  if (!input) throw new Error(`no input beside "${label}"`)
  return input as HTMLInputElement
}

/** The row selector inside one assignment card, found by its visible label. */
function slotSelect(label: string): HTMLSelectElement {
  const select = screen.getByText(label).closest('div')?.querySelector('select')
  if (!select) throw new Error(`no row selector for slot "${label}"`)
  return select as HTMLSelectElement
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
}

/** The overwrite-or-save-as-new dialog's own panel (the title's parent card). */
function saveDialog(): HTMLElement {
  const panel = screen.getByText('Overwrite existing or save as new?').parentElement
  if (!panel) throw new Error('save dialog is not open')
  return panel as HTMLElement
}

const REQUIRED_LABELS = ['Idle *', 'Walking *', 'Thinking *', 'Working *', 'Error *', 'Offline *']

/** Sheet dimensions that divide exactly into the documented 192x208 petdex grid. */
const PETDEX_SHEET = { width: 192 * 8, height: 208 * 9 }

/** Pick a sheet through the file button and let it decode. */
async function pickSheet(mime = 'image/png'): Promise<void> {
  api.importSpriteFile.mockResolvedValue({ content: 'SHEETBYTES', mime })
  fireEvent.click(screen.getByRole('button', { name: /Select Sprite Sheet|Change File/ }))
  await settle()
}

const existingPack: PackMeta = {
  id: 'pack-77',
  name: 'Pixel Ghost',
  author: 'Zed',
  description: 'a small pixel ghost',
  type: 'custom',
  format: 'sprite',
  thumbnail: 'thumb.png',
}

beforeEach(() => {
  vi.clearAllMocks()
  awaitingDecode.length = 0
  sliceSeq = 0
  sheetSize = { width: 64, height: 64 }
  sheetPixels = alphaOpaque(64, 64)
  api.galleryGetPackDetail.mockResolvedValue(null)
  api.galleryReadPackFile.mockResolvedValue(null)
  api.importSpriteFile.mockResolvedValue(null)

  vi.stubGlobal('Image', FakeImage)
  realGetContext = canvasProto.getContext
  realToDataURL = canvasProto.toDataURL
  canvasProto.getContext = () => ({
    clearRect: () => {},
    drawImage: () => {},
    getImageData: () => sheetPixels,
  })
  canvasProto.toDataURL = function (this: HTMLCanvasElement): string {
    return `data:image/png;base64,slice-${this.width}x${this.height}-${sliceSeq++}`
  }
})

afterEach(() => {
  cleanup()
  canvasProto.getContext = realGetContext
  canvasProto.toDataURL = realToDataURL
  vi.unstubAllGlobals()
})

describe('SpriteImporter — before a sheet is picked', () => {
  it('offers only the file picker and refuses to save', () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)

    expect(screen.getByText('Create Sprite Pack')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()
    // No sheet means no rows, so neither the row previews nor the slot grids exist.
    expect(screen.queryByText('Source Image', { exact: false })).not.toBeInTheDocument()
    expect(screen.queryByText('Required States')).not.toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
    // The missing-state hint is suppressed until there is something to assign.
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
  })

  it('starts every frame field on its default and keeps the sprite unflipped', () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)

    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('FPS').value).toBe('8')
    expect(fieldInput('Y Offset').value).toBe('0')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  })

  it('leaves the screen untouched when the file pick is cancelled or refused', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)

    api.importSpriteFile.mockResolvedValue(null)
    fireEvent.click(screen.getByRole('button', { name: /Select Sprite Sheet/ }))
    await settle()
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()

    api.importSpriteFile.mockResolvedValue({ ok: false })
    fireEvent.click(screen.getByRole('button', { name: /Select Sprite Sheet/ }))
    await settle()

    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()
    expect(screen.queryByText('Required States')).not.toBeInTheDocument()
  })

  it('reports a failed save from its owner without unmounting the work', () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} saveError="disk is full" />)

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('disk is full')
    // The picker is still there: the configured sheet was not thrown away.
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()
  })

  it('calls back on cancel', () => {
    const onCancel = vi.fn()
    render(<SpriteImporter onDone={vi.fn()} onCancel={onCancel} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})

describe('SpriteImporter — picking a sheet', () => {
  it('adopts the petdex grid and pre-fills every slot when the sheet matches the convention', async () => {
    sheetSize = PETDEX_SHEET
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet('image/webp')

    expect(fieldInput('Frame W').value).toBe('192')
    expect(fieldInput('Frame H').value).toBe('208')
    expect(fieldInput('FPS').value).toBe('8')
    expect(fieldInput('Y Offset').value).toBe('0')
    // The pre-fill is announced: dropdowns that fill themselves read as a bug.
    expect(screen.getByText(/matches the petdex.dev layout/)).toBeInTheDocument()
    // 1536x1872 over a 192x208 box = 8 columns of 9 rows.
    expect(screen.getByText('Rows (9)')).toBeInTheDocument()
    expect(screen.getByText(/1536×1872px → 8 cols × 9 rows/)).toBeInTheDocument()

    // The documented row convention, including the two non-obvious picks:
    // walking takes running-right (row 1) and thinking takes review (row 8).
    expect(slotSelect('Idle *').value).toBe('0')
    expect(slotSelect('Walking *').value).toBe('1')
    expect(slotSelect('Thinking *').value).toBe('8')
    expect(slotSelect('Working *').value).toBe('7')
    expect(slotSelect('Error *').value).toBe('5')
    expect(slotSelect('Offline *').value).toBe('6')
    expect(slotSelect('Peeking').value).toBe('3')
    expect(slotSelect('Happy').value).toBe('3')
    expect(slotSelect('Sleepy').value).toBe('6')
  })

  it('keeps the mime the file carried, so a WebP sheet is not left to sniffing', async () => {
    sheetSize = PETDEX_SHEET
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet('image/webp')

    const preview = document.querySelector('img')
    expect(preview?.getAttribute('src')).toBe('data:image/webp;base64,SHEETBYTES')
    // Second pick: the button now offers to replace the sheet.
    expect(screen.getByRole('button', { name: /Change File/ })).toBeInTheDocument()
  })

  it('falls back to frame detection for a sheet that is off the petdex grid', async () => {
    // 64x66 is not divisible by 192x208, so the convention cannot apply. The
    // sheet is ruled every 16px with two blank rows on top.
    sheetSize = { width: 64, height: 66 }
    sheetPixels = alphaRuled(64, 66, 16, 2)
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    // 15px of art between 1px rules, and the two blank rows become the offset.
    expect(fieldInput('Frame W').value).toBe('15')
    expect(fieldInput('Frame H').value).toBe('15')
    expect(fieldInput('Y Offset').value).toBe('2')
    // Nothing was pre-mapped, so no pre-fill notice is shown.
    expect(screen.queryByText(/matches the petdex.dev layout/)).not.toBeInTheDocument()
    // ceil((66-2)/15) = 5 rows, the last one clipped to the sheet's bottom edge.
    expect(screen.getByText('Rows (5)')).toBeInTheDocument()
    // floor(64/15) = 4 columns per row.
    expect(screen.getAllByText('4f')).toHaveLength(5)
    // Every slot starts empty: a detected grid is not a mapping.
    for (const label of REQUIRED_LABELS) expect(slotSelect(label).value).toBe('')
    expect(screen.getByText('Missing: Idle, Walking, Thinking, Working, Error, Offline')).toBeInTheDocument()
  })

  it('keeps the default frame size when the sheet has no transparent grid lines', async () => {
    sheetSize = { width: 64, height: 64 }
    sheetPixels = alphaOpaque(64, 64)
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    // Detection reports the whole sheet as one frame, which is refused rather
    // than applied — a 64px "frame" on a 64px sheet is not a grid.
    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('Y Offset').value).toBe('0')
    expect(screen.getByText('Rows (2)')).toBeInTheDocument()
  })

  it('re-slices the rows when the frame height changes', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()
    expect(screen.getByText('Rows (2)')).toBeInTheDocument()

    fireEvent.change(fieldInput('Frame H'), { target: { value: '16' } })
    await settle()

    expect(screen.getByText('Rows (4)')).toBeInTheDocument()
    // The slot previews follow the new geometry.
    expect(document.querySelector('[data-strip]')).toHaveAttribute('data-frame', '32x16@8')
  })

  it('drops every row when the Y offset runs past the bottom of the sheet', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()
    expect(screen.getByText('Rows (2)')).toBeInTheDocument()

    fireEvent.change(fieldInput('Y Offset'), { target: { value: '200' } })
    await settle()

    expect(screen.queryByText(/^Rows \(/)).not.toBeInTheDocument()
    expect(screen.queryByText('Required States')).not.toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
  })
})

describe('SpriteImporter — a prefilled import', () => {
  const prefill: SpritePrefillInput = {
    name: 'Sprout',
    author: 'petdex.dev',
    description: 'a leafy sprout',
    imageUri: 'data:image/webp;base64,PREFILLED',
    frameWidth: 192,
    frameHeight: 208,
    fps: 8,
    rowAssignments: { idle: 0, walking: 1, nonsense: 4 },
  }

  it('starts from the supplied sheet, grid and mapping, and says so', async () => {
    sheetSize = PETDEX_SHEET
    render(<SpriteImporter prefill={prefill} onDone={vi.fn()} onCancel={vi.fn()} />)
    await settle()

    expect(fieldInput('Name *').value).toBe('Sprout')
    expect(fieldInput('Author').value).toBe('petdex.dev')
    expect(fieldInput('Character Description').value).toBe('a leafy sprout')
    expect(fieldInput('Frame W').value).toBe('192')
    expect(fieldInput('Frame H').value).toBe('208')
    expect(screen.getByText(/matches the petdex.dev layout/)).toBeInTheDocument()
    expect(screen.getByText('Rows (9)')).toBeInTheDocument()

    expect(slotSelect('Idle *').value).toBe('0')
    expect(slotSelect('Walking *').value).toBe('1')
    // A slot the pet vocabulary does not have is ignored, not invented.
    expect(slotSelect('Thinking *').value).toBe('')
  })

  it('falls back to the detected grid when the source could not confirm one', async () => {
    sheetSize = { width: 64, height: 64 }
    render(
      <SpriteImporter
        prefill={{ ...prefill, frameWidth: 0, frameHeight: 0, fps: 0, rowAssignments: {} }}
        onDone={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    await settle()

    // 0 means "could not determine": the defaults stay in charge rather than
    // shearing every frame against an unverified geometry.
    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('FPS').value).toBe('8')
    expect(screen.queryByText(/matches the petdex.dev layout/)).not.toBeInTheDocument()
  })
})

describe('SpriteImporter — saving a new pack', () => {
  it('holds Save back until the pack has a name and all six required rows', async () => {
    sheetSize = PETDEX_SHEET
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    // Every required slot is mapped, but the pack is still nameless.
    expect(saveButton()).toBeDisabled()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    expect(saveButton()).toBeEnabled()

    fireEvent.change(fieldInput('Name *'), { target: { value: '   ' } })
    expect(saveButton()).toBeDisabled()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(slotSelect('Idle *'), { target: { value: '' } })
    expect(saveButton()).toBeDisabled()
    expect(screen.getByText('Missing: Idle')).toBeInTheDocument()
  })

  it('hands the owner the grid, the sliced strips and the row mapping', async () => {
    sheetSize = PETDEX_SHEET
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet('image/webp')

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(fieldInput('Author'), { target: { value: 'Zed' } })
    fireEvent.change(fieldInput('Character Description'), { target: { value: 'a leafy sprout' } })
    fireEvent.click(screen.getByRole('switch'))
    // A fresh import has nothing to overwrite, so Save commits with no dialog.
    fireEvent.click(saveButton())

    expect(onDone).toHaveBeenCalledTimes(1)
    const data = onDone.mock.calls[0][0]
    expect(data).toMatchObject({
      name: 'Sprout',
      author: 'Zed',
      description: 'a leafy sprout',
      frameWidth: 192,
      frameHeight: 208,
      fps: 8,
      flipX: true,
      offsetY: 0,
      sourceImage: 'data:image/webp;base64,SHEETBYTES',
    })
    expect(data.overwriteId).toBeUndefined()
    expect(data.rowAssignments).toMatchObject({
      idle: 0, walking: 1, thinking: 8, working: 7, error: 5, offline: 6,
    })
    // Each mapped slot carries the sliced strip for its row, not the whole sheet.
    for (const slot of Object.keys(data.rowAssignments)) {
      expect(data.assignments[slot]).toMatch(/^data:image\/png;base64,slice-1536x208-\d+$/)
    }
  })

  it('saves a mapping the user assigned row by row', async () => {
    // A hand-made sheet: nothing is pre-mapped, so every slot below is a choice
    // the user makes in the dropdowns.
    sheetSize = { width: 64, height: 66 }
    sheetPixels = alphaRuled(64, 66, 16, 2)
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet()

    const chosen: Record<string, number> = {
      'Idle *': 0, 'Walking *': 1, 'Thinking *': 2, 'Working *': 3, 'Error *': 4, 'Offline *': 0,
    }
    for (const [label, row] of Object.entries(chosen)) {
      fireEvent.change(slotSelect(label), { target: { value: String(row) } })
    }
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Hand Mapped' } })
    fireEvent.click(saveButton())

    const data = onDone.mock.calls[0][0]
    expect(data.rowAssignments).toEqual({
      idle: 0, walking: 1, thinking: 2, working: 3, error: 4, offline: 0,
    })
    expect(data).toMatchObject({ frameWidth: 15, frameHeight: 15, offsetY: 2 })
    // Row 4 is the clipped bottom row, so its strip is shorter than the others.
    expect(data.assignments.error).toMatch(/^data:image\/png;base64,slice-60x4-\d+$/)
    expect(data.assignments.idle).toMatch(/^data:image\/png;base64,slice-60x15-\d+$/)
    // The same row can serve two states without being sliced twice.
    expect(data.assignments.offline).toBe(data.assignments.idle)
  })

  it('leaves unmapped slots out of the saved pack entirely', async () => {    sheetSize = PETDEX_SHEET
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(slotSelect('Sleepy'), { target: { value: '' } })
    fireEvent.click(saveButton())

    const data = onDone.mock.calls[0][0]
    expect('sleepy' in data.rowAssignments).toBe(false)
    expect('sleepy' in data.assignments).toBe(false)
    expect(data.rowAssignments.happy).toBe(3)
  })
})

describe('SpriteImporter — editing an existing pack', () => {
  const spriteDetail = (over: Record<string, unknown> = {}) => ({
    sprite: {
      frameWidth: 24,
      frameHeight: 24,
      fps: 12,
      flipX: true,
      offsetY: 4,
      source: 'sheet.png',
      // All six required states are mapped, so the stored pack is savable as-is
      // and the dirty check is the only thing gating Save.
      rowAssignments: {
        idle: 1, walking: 0, thinking: 2, working: 0, error: 1, offline: 2, happy: 2,
      },
      ...over,
    },
  })

  async function mountEdit(detail: unknown, onDone = vi.fn()): Promise<ReturnType<typeof vi.fn>> {
    api.galleryGetPackDetail.mockResolvedValue(detail)
    render(<SpriteImporter existingPack={existingPack} onDone={onDone} onCancel={vi.fn()} />)
    await settle()
    return onDone
  }

  it('restores the stored grid, sheet and row mapping, and starts clean', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    await mountEdit(spriteDetail())

    expect(screen.getByText('Edit Sprite Pack')).toBeInTheDocument()
    expect(api.galleryReadPackFile).toHaveBeenCalledWith('pack-77', 'sheet.png')
    expect(document.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,STOREDSHEET')
    expect(fieldInput('Frame W').value).toBe('24')
    expect(fieldInput('FPS').value).toBe('12')
    expect(fieldInput('Y Offset').value).toBe('4')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    // ceil((64-4)/24) = 3 rows.
    expect(screen.getByText('Rows (3)')).toBeInTheDocument()
    expect(slotSelect('Idle *').value).toBe('1')
    expect(slotSelect('Walking *').value).toBe('0')
    expect(slotSelect('Happy').value).toBe('2')
    // Nothing has changed yet, so there is nothing to save.
    expect(saveButton()).toBeDisabled()
  })

  it('re-opens Save as soon as anything differs from the stored pack', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    await mountEdit(spriteDetail())
    expect(saveButton()).toBeDisabled()

    fireEvent.click(screen.getByRole('switch'))
    expect(saveButton()).toBeEnabled()

    // Flipping back restores the stored state exactly, so it is clean again.
    fireEvent.click(screen.getByRole('switch'))
    expect(saveButton()).toBeDisabled()
  })

  it('asks whether to overwrite or branch once the pack is dirty', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    const onDone = await mountEdit(spriteDetail())

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Pixel Ghost v2' } })
    fireEvent.click(saveButton())

    expect(onDone).not.toHaveBeenCalled()
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Overwrite' }))

    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone.mock.calls[0][0]).toMatchObject({ overwriteId: 'pack-77', name: 'Pixel Ghost v2' })
    expect(screen.queryByText('Overwrite existing or save as new?')).not.toBeInTheDocument()
  })

  it('branches into a new pack instead when asked', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    const onDone = await mountEdit(spriteDetail())

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Pixel Ghost v2' } })
    fireEvent.click(saveButton())
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Save as New' }))

    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone.mock.calls[0][0].overwriteId).toBeUndefined()
  })

  it('keeps the editor and its edits when the dialog is dismissed', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    const onDone = await mountEdit(spriteDetail())

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Pixel Ghost v2' } })
    fireEvent.click(saveButton())
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Cancel' }))

    expect(onDone).not.toHaveBeenCalled()
    expect(screen.queryByText('Overwrite existing or save as new?')).not.toBeInTheDocument()
    expect(fieldInput('Name *').value).toBe('Pixel Ghost v2')
    expect(saveButton()).toBeEnabled()
  })

  it('rebuilds the rows from the stored strips when the source sheet is gone', async () => {
    await mountEdit({
      sprite: {
        frameWidth: 24, frameHeight: 24, fps: 12, offsetY: 0,
        rowAssignments: { idle: 1 },
      },
      animations: {
        idle: { content: 'IDLEBYTES', format: 'png' },
        walking: { content: 'IDLEBYTES', format: 'png' },
        thinking: { content: 'data:image/png;base64,THINKBYTES', format: 'png' },
      },
    })

    // Two distinct strips: the shared one is stored once and mapped twice.
    expect(screen.getByText('Rows (2)')).toBeInTheDocument()
    expect(slotSelect('Idle *').value).toBe('0')
    expect(slotSelect('Walking *').value).toBe('0')
    expect(slotSelect('Thinking *').value).toBe('1')
    expect(document.querySelectorAll('[data-strip="data:image/png;base64,IDLEBYTES"]').length).toBeGreaterThan(0)
    // Rebuilt-from-strips is still the stored state, so Save stays shut.
    expect(saveButton()).toBeDisabled()
    expect(screen.getByText('Missing: Working, Error, Offline')).toBeInTheDocument()
  })

  it('falls back to the stored strips when reading the source sheet fails', async () => {
    api.galleryReadPackFile.mockResolvedValue(null)
    await mountEdit({
      ...spriteDetail(),
      animations: { idle: { content: 'IDLEBYTES', format: 'png' } },
    })

    expect(api.galleryReadPackFile).toHaveBeenCalledWith('pack-77', 'sheet.png')
    expect(screen.getByText('Rows (1)')).toBeInTheDocument()
    expect(slotSelect('Idle *').value).toBe('0')
  })

  it('shows an empty editor when the pack detail carries no sprite', async () => {
    await mountEdit({ sprite: null })

    expect(screen.getByText('Edit Sprite Pack')).toBeInTheDocument()
    expect(screen.queryByText('Required States')).not.toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
  })

  it('survives a pack whose detail cannot be read at all', async () => {
    await mountEdit(null)

    expect(screen.getByText('Edit Sprite Pack')).toBeInTheDocument()
    expect(fieldInput('Name *').value).toBe('Pixel Ghost')
    expect(saveButton()).toBeDisabled()
  })

  it('lets the user replace the sheet of a pack it is editing', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    await mountEdit(spriteDetail())
    expect(screen.getByText('Rows (3)')).toBeInTheDocument()

    sheetSize = PETDEX_SHEET
    await pickSheet()

    expect(screen.getByText('Rows (9)')).toBeInTheDocument()
    expect(fieldInput('Frame W').value).toBe('192')
    // A different sheet is a change, so the pack is dirty and savable.
    expect(saveButton()).toBeEnabled()
  })

  it('falls back to the defaults for a stored pack that recorded no geometry', async () => {
    const strip = { content: 'ONESTRIP', format: 'png' }
    const onDone = await mountEdit({
      // No frame size, no fps, no offset, no mapping and no source sheet: an old
      // pack that only ever stored its strips.
      sprite: { flipX: false },
      animations: {
        idle: strip, walking: strip, thinking: strip,
        working: strip, error: strip, offline: strip,
      },
    })

    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('FPS').value).toBe('8')
    expect(fieldInput('Y Offset').value).toBe('0')
    // One shared strip, so all six required states point at the same row.
    expect(screen.getByText('Rows (1)')).toBeInTheDocument()
    for (const label of REQUIRED_LABELS) expect(slotSelect(label).value).toBe('0')

    fireEvent.change(fieldInput('FPS'), { target: { value: '10' } })
    fireEvent.click(saveButton())
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Overwrite' }))

    expect(onDone).toHaveBeenCalledTimes(1)
    const data = onDone.mock.calls[0][0]
    // There was no source sheet to carry over, and none is invented.
    expect(data.sourceImage).toBeUndefined()
    expect(data).toMatchObject({ fps: 10, frameWidth: 32, frameHeight: 32, overwriteId: 'pack-77' })
    expect(data.assignments.idle).toBe('data:image/png;base64,ONESTRIP')
  })
})

describe('SpriteImporter — degenerate input', () => {
  it('assumes PNG when the picked file reports no mime at all', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    api.importSpriteFile.mockResolvedValue({ content: 'SHEETBYTES', mime: '' })
    fireEvent.click(screen.getByRole('button', { name: /Select Sprite Sheet/ }))
    await settle()

    expect(document.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,SHEETBYTES')
  })

  it('keeps slicing on the default frame width when the field is emptied to zero', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()
    expect(screen.getByText('Rows (2)')).toBeInTheDocument()

    fireEvent.change(fieldInput('Frame W'), { target: { value: '0' } })
    await settle()

    // The field keeps what the user typed…
    expect(fieldInput('Frame W').value).toBe('0')
    // …while the slicer refuses to divide by it and holds the 32px default, so
    // the rows survive a half-typed number.
    expect(screen.getByText('Rows (2)')).toBeInTheDocument()
    // The grid summary counts columns at 1px rather than dividing by zero.
    expect(screen.getByText(/64×64px → 64 cols × 2 rows/)).toBeInTheDocument()

    fireEvent.change(fieldInput('Frame H'), { target: { value: '0' } })
    await settle()

    expect(screen.getByText('Rows (2)')).toBeInTheDocument()
    expect(screen.getByText(/64×64px → 64 cols × 64 rows/)).toBeInTheDocument()
  })

  it('cannot save a stored pack that lost its name', async () => {
    const strip = { content: 'ONESTRIP', format: 'png' }
    api.galleryGetPackDetail.mockResolvedValue({
      sprite: { flipX: false },
      animations: {
        idle: strip, walking: strip, thinking: strip,
        working: strip, error: strip, offline: strip,
      },
    })
    render(
      <SpriteImporter
        existingPack={{ ...existingPack, name: '', author: '', description: '' }}
        onDone={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    await settle()

    expect(fieldInput('Name *').value).toBe('')
    expect(fieldInput('Author').value).toBe('')
    // Every required row is mapped, so the name is the only thing missing — and
    // it is enough to keep Save shut.
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch'))
    expect(saveButton()).toBeDisabled()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Renamed' } })
    expect(saveButton()).toBeEnabled()
  })
})
