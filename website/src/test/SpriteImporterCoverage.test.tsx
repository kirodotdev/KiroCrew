/**
 * Crew Companion sprite importer — first tests for
 * `apps/crew-companion/SpriteImporter.tsx`.
 *
 * The importer is the whole "turn a sprite sheet into an appearance pack" screen:
 * pick a sheet, detect its grid, slice it into rows, map each row onto one of the
 * pet's slots, and hand the result to its owner. Nothing here was covered, so
 * every behaviour below is pinned for the first time — including the two the
 * source calls out as bugs it already had to fix: a random CLIP must travel as
 * `randomAssignments` (the bridge files an unknown key in `assignments` as a
 * mood, which silently disabled a pack's random behaviour), and a failed save
 * must leave the editor mounted with the user's edits intact.
 *
 * Three boundaries are stubbed, deliberately:
 *
 *   - **Image decode + canvas.** The importer measures the sheet with
 *     `new Image()` and slices it through a 2d canvas. happy-dom never decodes an
 *     image (`load` never fires, `naturalWidth` stays 0) and has no canvas, so no
 *     row would ever exist. A fake `Image` reports the pixel size the test asks
 *     for and hands the test control of when `load` lands (`settle()`), while the
 *     canvas returns the alpha map the test built. Everything above that seam —
 *     the real `detectFrameSize`, the slicing loop, the assignment state machine,
 *     the dirty check, the save classification — runs for real.
 *   - **`SpriteRenderer`**, which drives a `requestAnimationFrame` loop off its
 *     own image decode and has its own tests. Here it stands in as a marker so
 *     the importer's OWN row/slot wiring is what gets asserted.
 *   - **`SimpleSelect`**, a Radix listbox that needs pointer capture and a
 *     portal to open. Swapped for a native `<select>` carrying the same props,
 *     so a row choice is one `change` event rather than a popover dance.
 *
 * No test depends on real elapsed time or on an animation frame landing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act, cleanup, within } from '@testing-library/react'

import type { PackMeta } from '../apps/crew-companion/appearanceTypes'

// ── Bridge double ──────────────────────────────────────────────────────────

const mocks = vi.hoisted(() => ({
  api: {
    importSpriteFile: vi.fn<() => Promise<unknown>>(),
    galleryGetPackDetail: vi.fn<(packId: string) => Promise<unknown>>(),
    galleryReadPackFile: vi.fn<(packId: string, file: string) => Promise<string | null>>(),
    getCrewCompanionConfig: vi.fn<() => Promise<unknown>>(),
  },
}))

vi.mock('../apps/crew-companion/petBridge', () => ({ galleryApi: mocks.api }))

vi.mock('../apps/crew-companion/SpriteRenderer', () => ({
  SpriteRenderer: ({ src, frameWidth, frameHeight, fps, displaySize }: {
    src: string
    frameWidth: number
    frameHeight: number
    fps?: number
    displaySize?: number
  }) => (
    <span
      data-strip={src}
      data-frame={`${frameWidth}x${frameHeight}@${fps ?? 0}`}
      data-display={displaySize ?? 0}
    />
  ),
}))

vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, optionLabels, value, onChange, clearLabel, 'aria-label': ariaLabel }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (v: string) => void
    clearLabel?: string
    'aria-label'?: string
  }) => (
    <select aria-label={ariaLabel} value={value} onChange={(e) => onChange(e.target.value)}>
      {clearLabel !== undefined ? <option value="">{clearLabel}</option> : null}
      {options.map((opt, i) => (
        <option key={opt} value={opt}>{optionLabels?.[i] ?? opt}</option>
      ))}
    </select>
  ),
}))

import { SpriteImporter } from '../apps/crew-companion/SpriteImporter'

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

/** One slot's row selector, found by the slot id the importer labels it with. */
function slotSelect(slot: string): HTMLSelectElement {
  return screen.getByLabelText(slot) as HTMLSelectElement
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
}

function pickerButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /Select Sprite Sheet|Change File/ }) as HTMLButtonElement
}

/** The overwrite-or-save-as-new dialog's own panel (the title's parent card). */
function saveDialog(): HTMLElement {
  const panel = screen.getByText('Overwrite existing or save as new?').parentElement
  if (!panel) throw new Error('save dialog is not open')
  return panel as HTMLElement
}

/** Pick a sheet through the file button and let it decode. */
async function pickSheet(content = 'SHEETBYTES'): Promise<void> {
  api.importSpriteFile.mockResolvedValue({ ok: true, value: { content } })
  fireEvent.click(pickerButton())
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
  api.getCrewCompanionConfig.mockResolvedValue({ language: 'en' })

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

    expect(screen.getByText(/Create Sprite Pack/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()
    // No sheet means no rows, so neither the row previews nor the slot grids exist.
    expect(screen.queryByText(/^Animations found in your sheet/)).not.toBeInTheDocument()
    expect(screen.queryByText('Required (just Idle)')).not.toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
    // The missing-slot hint is suppressed until there is something to assign.
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
    // Nothing was read from storage: this is a fresh pack, not an edit.
    expect(api.galleryGetPackDetail).not.toHaveBeenCalled()
  })

  it('starts every frame field on its default and keeps the sprite unflipped', () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)

    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('FPS').value).toBe('8')
    expect(fieldInput('Y Offset').value).toBe('0')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  })

  it('leaves the screen untouched when the pick is cancelled, refused or empty', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)

    // Cancelled: the bridge answers with nothing at all.
    api.importSpriteFile.mockResolvedValue(null)
    fireEvent.click(pickerButton())
    await settle()
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()

    // Refused: a reason the owner shows, but no bytes.
    api.importSpriteFile.mockResolvedValue({ ok: false, error: 'That image could not be read' })
    fireEvent.click(pickerButton())
    await settle()
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()

    // Accepted but empty: an ok with no content is not a sheet.
    api.importSpriteFile.mockResolvedValue({ ok: true, value: { content: '' } })
    fireEvent.click(pickerButton())
    await settle()

    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
    expect(screen.queryByText('Required (just Idle)')).not.toBeInTheDocument()
  })

  it('reports a failed save from its owner without unmounting the work', () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} saveError="disk is full" />)

    expect(screen.getByRole('alert')).toHaveTextContent('disk is full')
    // The picker is still there: the configured sheet was not thrown away.
    expect(screen.getByRole('button', { name: /Select Sprite Sheet/ })).toBeInTheDocument()
  })

  it('shows no alert while the owner has reported nothing', () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} saveError={null} />)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('calls back on cancel', () => {
    const onCancel = vi.fn()
    render(<SpriteImporter onDone={vi.fn()} onCancel={onCancel} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})

describe('SpriteImporter — picking a sheet', () => {
  it('slices the sheet on the default grid when it carries no detectable rules', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    // Detection reports the whole sheet as one frame, which is refused rather
    // than applied — a 64px "frame" on a 64px sheet is not a grid.
    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('Y Offset').value).toBe('0')

    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()
    expect(screen.getByText('Preview (64×64px → 2 cols × 2 rows)')).toBeInTheDocument()
    // Two row previews, each rendered at the sheet's own frame size. The slot
    // cards render theirs at 64px, so the two are told apart by that.
    expect(document.querySelectorAll('[data-display="0"]')).toHaveLength(2)
    expect(document.querySelectorAll('[data-display="64"]')).toHaveLength(0)
    // floor(64/32) = 2 frames per row.
    expect(screen.getAllByText('2f')).toHaveLength(2)
    // The sheet itself is shown, and the button now offers to replace it.
    expect(document.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,SHEETBYTES')
    expect(screen.getByRole('button', { name: /Change File/ })).toBeInTheDocument()
    // Every slot starts empty, and only Idle is required.
    expect(slotSelect('idle').value).toBe('')
    expect(slotSelect('walking').value).toBe('')
    expect(screen.getByText('Missing: Idle')).toBeInTheDocument()
  })

  it('adopts a detected grid and top offset from a ruled sheet', async () => {
    // 64x66 ruled every 16px with two blank rows on top.
    sheetSize = { width: 64, height: 66 }
    sheetPixels = alphaRuled(64, 66, 16, 2)
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    // 15px of art between 1px rules, and the two blank rows become the offset.
    expect(fieldInput('Frame W').value).toBe('15')
    expect(fieldInput('Frame H').value).toBe('15')
    expect(fieldInput('Y Offset').value).toBe('2')
    // ceil((66-2)/15) = 5 rows, the last one clipped to the sheet's bottom edge.
    expect(screen.getByText('Animations found in your sheet (5)')).toBeInTheDocument()
    // floor(64/15) = 4 columns per row.
    expect(screen.getAllByText('4f')).toHaveLength(5)
  })

  it('offers every optional slot group once there are rows to assign', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    expect(screen.getByText('Required (just Idle)')).toBeInTheDocument()
    for (const group of ['Status', 'Breathing', 'Random']) {
      // Each optional group announces itself as optional, so a user who skips
      // one is not left wondering what they broke. Matched on the whole label,
      // which spans two nodes (the group name and the "(optional)" note).
      expect(screen.getByText(
        (_content, el) => el?.textContent === `${group} (optional)`,
      )).toBeInTheDocument()
    }
    for (const slot of ['idle', 'done', 'error', 'inhale', 'hold', 'exhale', 'walking']) {
      expect(slotSelect(slot)).toBeInTheDocument()
    }
    // Each selector lists one option per row plus the clear row.
    expect(slotSelect('done').querySelectorAll('option')).toHaveLength(3)
  })

  it('re-slices the rows when the frame height changes', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()
    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()

    fireEvent.change(fieldInput('Frame H'), { target: { value: '16' } })
    await settle()

    expect(screen.getByText('Animations found in your sheet (4)')).toBeInTheDocument()
    // The row previews follow the new geometry.
    expect(document.querySelector('[data-strip]')).toHaveAttribute('data-frame', '32x16@8')
  })

  it('drops every row when the Y offset runs past the bottom of the sheet', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()
    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()

    fireEvent.change(fieldInput('Y Offset'), { target: { value: '200' } })
    await settle()

    expect(screen.queryByText(/^Animations found in your sheet/)).not.toBeInTheDocument()
    expect(screen.queryByText('Required (just Idle)')).not.toBeInTheDocument()
    // With nothing to assign the missing hint goes quiet again, and so does Save.
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
  })

  it('keeps slicing on the default frame width when the field is emptied to zero', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Frame W'), { target: { value: '0' } })
    await settle()

    // The field keeps what the user typed…
    expect(fieldInput('Frame W').value).toBe('0')
    // …while the slicer refuses to divide by it and holds the 32px default, so
    // the rows survive a half-typed number.
    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()
    // The grid summary counts columns at 1px rather than dividing by zero.
    expect(screen.getByText('Preview (64×64px → 64 cols × 2 rows)')).toBeInTheDocument()
  })
})

describe('SpriteImporter — saving a new pack', () => {
  it('holds Save back until the pack has a name and an idle row', async () => {
    render(<SpriteImporter onDone={vi.fn()} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    // Named, but nothing is assigned yet.
    expect(saveButton()).toBeDisabled()

    fireEvent.change(slotSelect('idle'), { target: { value: '0' } })
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
    expect(saveButton()).toBeEnabled()

    // Whitespace is not a name.
    fireEvent.change(fieldInput('Name *'), { target: { value: '   ' } })
    expect(saveButton()).toBeDisabled()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(slotSelect('idle'), { target: { value: '' } })
    expect(saveButton()).toBeDisabled()
    expect(screen.getByText('Missing: Idle')).toBeInTheDocument()
  })

  it('hands the owner the grid, the sliced strips and the row mapping', async () => {
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(fieldInput('Author'), { target: { value: 'Zed' } })
    fireEvent.change(fieldInput('Character Description'), { target: { value: 'a leafy sprout' } })
    fireEvent.change(fieldInput('FPS'), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.change(slotSelect('idle'), { target: { value: '0' } })
    fireEvent.change(slotSelect('done'), { target: { value: '1' } })
    // A fresh import has nothing to overwrite, so Save commits with no dialog.
    fireEvent.click(saveButton())

    expect(screen.queryByText('Overwrite existing or save as new?')).not.toBeInTheDocument()
    expect(onDone).toHaveBeenCalledTimes(1)
    const data = onDone.mock.calls[0][0]
    expect(data).toMatchObject({
      name: 'Sprout',
      author: 'Zed',
      description: 'a leafy sprout',
      frameWidth: 32,
      frameHeight: 32,
      fps: 12,
      flipX: true,
      offsetY: 0,
      sourceImage: 'data:image/png;base64,SHEETBYTES',
    })
    expect(data.overwriteId).toBeUndefined()
    expect(data.rowAssignments).toEqual({ idle: 0, done: 1 })
    // Each mapped slot carries the sliced strip for its row, not the whole sheet.
    expect(data.assignments.idle).toMatch(/^data:image\/png;base64,slice-64x32-\d+$/)
    expect(data.assignments.done).toMatch(/^data:image\/png;base64,slice-64x32-\d+$/)
    expect(data.assignments.done).not.toBe(data.assignments.idle)
    // No random clip was assigned, so the key is absent rather than empty.
    expect(data.randomAssignments).toBeUndefined()
  })

  it('files a random slot as a clip instead of a state', async () => {
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(slotSelect('idle'), { target: { value: '0' } })
    fireEvent.change(slotSelect('walking'), { target: { value: '1' } })
    fireEvent.click(saveButton())

    const data = onDone.mock.calls[0][0]
    // Walking is spontaneous behaviour: it must NOT land in `assignments`, which
    // the bridge would file as a mood.
    expect('walking' in data.assignments).toBe(false)
    expect(data.randomAssignments.walking).toMatch(/^data:image\/png;base64,slice-64x32-\d+$/)
    // The row mapping still records it, so the sheet can be re-edited.
    expect(data.rowAssignments).toEqual({ idle: 0, walking: 1 })
  })

  it('leaves unassigned slots out of the saved pack entirely', async () => {
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(slotSelect('idle'), { target: { value: '0' } })
    fireEvent.change(slotSelect('hold'), { target: { value: '1' } })
    // Assigned then cleared: the slot goes back to carrying nothing.
    fireEvent.change(slotSelect('hold'), { target: { value: '' } })
    fireEvent.click(saveButton())

    const data = onDone.mock.calls[0][0]
    expect(data.rowAssignments).toEqual({ idle: 0 })
    expect('hold' in data.assignments).toBe(false)
    expect('error' in data.assignments).toBe(false)
  })

  it('lets one row serve several slots without slicing it twice', async () => {
    const onDone = vi.fn()
    render(<SpriteImporter onDone={onDone} onCancel={vi.fn()} />)
    await pickSheet()

    fireEvent.change(fieldInput('Name *'), { target: { value: 'Sprout' } })
    fireEvent.change(slotSelect('idle'), { target: { value: '0' } })
    fireEvent.change(slotSelect('inhale'), { target: { value: '0' } })
    fireEvent.click(saveButton())

    const data = onDone.mock.calls[0][0]
    expect(data.assignments.inhale).toBe(data.assignments.idle)
  })
})

describe('SpriteImporter — editing an existing pack', () => {
  const spriteDetail = (over: Record<string, unknown> = {}) => ({
    sprite: {
      frameWidth: 32,
      frameHeight: 32,
      fps: 12,
      flipX: true,
      offsetY: 0,
      source: 'sheet.png',
      // Idle is mapped, so the stored pack is savable as-is and the dirty check
      // is the only thing gating Save.
      rowAssignments: { idle: 1, done: 0 },
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
    expect(api.galleryGetPackDetail).toHaveBeenCalledWith('pack-77')
    expect(api.galleryReadPackFile).toHaveBeenCalledWith('pack-77', 'sheet.png')
    expect(document.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,STOREDSHEET')
    expect(fieldInput('Name *').value).toBe('Pixel Ghost')
    expect(fieldInput('Author').value).toBe('Zed')
    expect(fieldInput('FPS').value).toBe('12')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()
    expect(slotSelect('idle').value).toBe('1')
    expect(slotSelect('done').value).toBe('0')
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

  it('keeps a loaded clip name as a random clip rather than turning it into a mood', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    const onDone = await mountEdit({
      randomNames: ['look away'],
      sprite: {
        frameWidth: 32, frameHeight: 32, fps: 8, offsetY: 0, source: 'sheet.png',
        rowAssignments: { idle: 0, 'look away': 1 },
      },
    })

    // The clip has no slot of its own on this screen, so the only proof it
    // survived the round trip is what the save carries.
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(saveButton())
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Overwrite' }))

    const data = onDone.mock.calls[0][0]
    expect('look away' in data.assignments).toBe(false)
    expect(data.randomAssignments['look away']).toMatch(/^data:image\/png;base64,slice-64x32-\d+$/)
    expect(data.rowAssignments).toMatchObject({ idle: 0, 'look away': 1 })
  })

  it('files an unrecognised slot as a mood when the pack claims no such clip', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    const onDone = await mountEdit({
      randomNames: [],
      sprite: {
        frameWidth: 32, frameHeight: 32, fps: 8, offsetY: 0, source: 'sheet.png',
        rowAssignments: { idle: 0, happy: 1 },
      },
    })

    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(saveButton())
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Overwrite' }))

    const data = onDone.mock.calls[0][0]
    expect(data.assignments.happy).toMatch(/^data:image\/png;base64,slice-64x32-\d+$/)
    expect(data.randomAssignments).toBeUndefined()
  })

  it('rebuilds the rows from the stored strips when the source sheet is gone', async () => {
    const onDone = await mountEdit({
      sprite: {
        frameWidth: 24, frameHeight: 24, fps: 12, offsetY: 0,
        rowAssignments: { idle: 1 },
      },
      animations: {
        idle: { content: 'IDLEBYTES', format: 'sprite' },
        done: { content: 'IDLEBYTES', format: 'sprite' },
        error: { content: 'data:image/png;base64,ERRBYTES', format: 'sprite' },
      },
    })

    // No source means no decode: the rows come from the strips themselves, and
    // the stored rowAssignments are dropped in favour of the rebuilt mapping.
    expect(api.galleryReadPackFile).not.toHaveBeenCalled()
    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()
    // The two slots sharing a strip share its row; the bare base64 one is
    // promoted to a data URI, the already-prefixed one is left alone.
    expect(slotSelect('idle').value).toBe('0')
    expect(slotSelect('done').value).toBe('0')
    expect(slotSelect('error').value).toBe('1')
    expect(document.querySelectorAll('[data-strip="data:image/png;base64,IDLEBYTES"]').length).toBeGreaterThan(0)
    expect(document.querySelectorAll('[data-strip="data:image/png;base64,ERRBYTES"]').length).toBeGreaterThan(0)
    // A strip has no frame count to report.
    expect(screen.getAllByText('0f')).toHaveLength(2)
    // Rebuilt-from-strips is still the stored state, so Save stays shut.
    expect(saveButton()).toBeDisabled()

    fireEvent.change(fieldInput('FPS'), { target: { value: '10' } })
    fireEvent.click(saveButton())
    fireEvent.click(within(saveDialog()).getByRole('button', { name: 'Overwrite' }))

    const data = onDone.mock.calls[0][0]
    // There was no source sheet to carry over, and none is invented.
    expect(data.sourceImage).toBeUndefined()
    expect(data).toMatchObject({ fps: 10, frameWidth: 24, frameHeight: 24, overwriteId: 'pack-77' })
    expect(data.assignments.idle).toBe('data:image/png;base64,IDLEBYTES')
  })

  it('falls back to the stored strips when reading the source sheet fails', async () => {
    api.galleryReadPackFile.mockResolvedValue(null)
    await mountEdit({
      ...spriteDetail(),
      animations: { idle: 'IDLEBYTES' },
    })

    expect(api.galleryReadPackFile).toHaveBeenCalledWith('pack-77', 'sheet.png')
    // A bare filename entry is still readable — entryContent takes the string.
    expect(screen.getByText('Animations found in your sheet (1)')).toBeInTheDocument()
    expect(slotSelect('idle').value).toBe('0')
    expect(document.querySelector('img')).toBeNull()
  })

  it('shows an empty editor when the pack detail carries no sprite', async () => {
    await mountEdit({ sprite: null })

    expect(screen.getByText('Edit Sprite Pack')).toBeInTheDocument()
    expect(screen.queryByText('Required (just Idle)')).not.toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
  })

  it('survives a pack whose detail cannot be read at all', async () => {
    await mountEdit(null)

    expect(screen.getByText('Edit Sprite Pack')).toBeInTheDocument()
    expect(fieldInput('Name *').value).toBe('Pixel Ghost')
    expect(fieldInput('Frame W').value).toBe('32')
    expect(saveButton()).toBeDisabled()
  })

  it('falls back to the defaults for a stored pack that recorded no geometry', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    await mountEdit({ sprite: { source: 'sheet.png' } })

    expect(fieldInput('Frame W').value).toBe('32')
    expect(fieldInput('Frame H').value).toBe('32')
    expect(fieldInput('FPS').value).toBe('8')
    expect(fieldInput('Y Offset').value).toBe('0')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    // No stored mapping, so every slot is empty and Idle is reported missing.
    expect(slotSelect('idle').value).toBe('')
    expect(screen.getByText('Missing: Idle')).toBeInTheDocument()
  })

  it('re-slices a replacement sheet, but does not count it as a change on its own', async () => {
    api.galleryReadPackFile.mockResolvedValue('STOREDSHEET')
    await mountEdit(spriteDetail())
    expect(screen.getByText('Animations found in your sheet (2)')).toBeInTheDocument()

    sheetSize = { width: 64, height: 128 }
    sheetPixels = alphaOpaque(64, 128)
    await pickSheet('REPLACEMENT')

    // The taller sheet is adopted and re-sliced on the stored 32px grid.
    expect(screen.getByText('Animations found in your sheet (4)')).toBeInTheDocument()
    expect(document.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,REPLACEMENT')
    // The dirty check compares the pack's FIELDS (name, grid, assignments) and
    // the sheet is not one of them, so swapping the art alone leaves Save shut.
    // Pinned as it stands: the new sheet cannot be saved until something the
    // snapshot does watch also changes.
    expect(saveButton()).toBeDisabled()

    fireEvent.click(screen.getByRole('switch'))
    expect(saveButton()).toBeEnabled()
  })
})
