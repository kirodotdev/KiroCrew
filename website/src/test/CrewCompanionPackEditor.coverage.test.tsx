/**
 * The SVG/Lottie pack editor's own behaviour, end to end through its bridge.
 *
 * `PackEditor` is the whole authoring surface: the slot grid, the per-slot popover
 * (pick a file / reuse another slot's art / clear), the open-ended "random extras",
 * and the save path with its overwrite-vs-save-as-new fork. Every one of those
 * reaches the desktop app through `galleryApi`, so the bridge is the only thing
 * mocked here — the component, its state machine, the shared header/footer/dialog
 * children and the real i18n strings all run for real.
 *
 * No fake timers: nothing in this editor is time-driven. Every asynchronous edge is
 * a promise the bridge double owns, so the tests await the state transition rather
 * than a clock. The one geometry-driven branch (the popover flipping above its
 * anchor near the bottom of the window) is exercised by stubbing
 * `getBoundingClientRect`, which happy-dom otherwise reports as all zeros.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'

import type { PackMeta } from '../apps/crew-companion/appearanceTypes'

// ── Bridge double ──────────────────────────────────────────────────────────

const mocks = vi.hoisted(() => ({
  api: {
    getCrewCompanionConfig: vi.fn(),
    galleryGetPackDetail: vi.fn(),
    galleryImportFile: vi.fn(),
    gallerySavePack: vi.fn(),
  },
}))

vi.mock('../apps/crew-companion/petBridge', () => ({
  galleryApi: mocks.api,
  petBridge: mocks.api,
}))

const api = mocks.api

// Imported AFTER the mock is registered.
const { PackEditor } = await import('../apps/crew-companion/PackEditor')

// ── Fixtures ───────────────────────────────────────────────────────────────

const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'
const SVG2 = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="2"/></svg>'
const LOTTIE = '{"v":"5.7.0","fr":30,"ip":0,"op":10,"layers":[]}'

const existing: PackMeta = {
  id: 'pack-42',
  name: 'Bramble',
  author: 'Ada',
  description: 'a mossy sprite',
  type: 'custom',
  format: 'svg',
  thumbnail: '',
}

/** A `galleryImportFile` success, in the wrapped `{ok, value}` shape. */
const imported = (content: string, format: 'svg' | 'lottie' | 'sprite' = 'svg') => ({
  ok: true as const,
  value: { content, filename: `art.${format === 'lottie' ? 'json' : 'svg'}`, format },
})

// ── Query helpers ──────────────────────────────────────────────────────────

/**
 * The slot tile carrying `label`.
 *
 * Tiles are `div role="button"`, so they are filtered away from the real
 * `<button>`s by tag. Optional tiles read "Done Optional", hence the prefix match.
 */
function slotFor(label: string): HTMLElement {
  const hit = screen
    .getAllByRole('button')
    .filter((el) => el.tagName === 'DIV')
    .find((el) => (el.textContent ?? '').replace(/\s+/g, ' ').trim().startsWith(label))
  if (!hit) throw new Error(`no slot tile labelled ${label}`)
  return hit
}

/**
 * The open slot popover.
 *
 * Matched by tag as well as role: a filled slot's `<img alt="">` is also implicitly
 * `role="presentation"`, so a role-only query becomes ambiguous the moment any slot
 * holds SVG art.
 */
function popover(): HTMLElement {
  const el = document.querySelector('div[role="presentation"]')
  if (!el) throw new Error('no slot popover is open')
  return el as HTMLElement
}

const popoverIsOpen = () => document.querySelector('div[role="presentation"]') !== null

/** Slot art is an `<img>` only for SVG; Lottie/sprite render an icon placeholder. */
const artOf = (tile: HTMLElement) => tile.querySelector('img')

/** The extras tile that owns `input` — the name field's own container. */
function extraTile(input: HTMLElement): HTMLElement {
  const box = input.parentElement
  if (!box) throw new Error('extras tile has no container')
  return box
}

/** The clickable thumbnail inside an extras tile (its remove button is a real button). */
function extraThumb(input: HTMLElement): HTMLElement {
  const hit = within(extraTile(input))
    .getAllByRole('button')
    .find((el) => el.tagName === 'DIV')
  if (!hit) throw new Error('extras tile has no thumbnail trigger')
  return hit
}

function renderEditor(existingPack?: PackMeta) {
  const onSave = vi.fn()
  const onCancel = vi.fn()
  const view = render(
    <PackEditor existingPack={existingPack} onSave={onSave} onCancel={onCancel} />,
  )
  return { onSave, onCancel, ...view }
}

/** Open `label`'s popover and pick a file that resolves to `content`. */
async function fillSlot(label: string, content: string, format: 'svg' | 'lottie' | 'sprite' = 'svg') {
  api.galleryImportFile.mockResolvedValueOnce(imported(content, format))
  fireEvent.click(slotFor(label))
  fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))
  await waitFor(() => expect(slotFor(label).querySelector('.animate-spin')).toBeNull())
}

/** Fill the extras tile owning `input`, and name it. */
async function fillExtra(input: HTMLElement, name: string, content: string) {
  api.galleryImportFile.mockResolvedValueOnce(imported(content))
  fireEvent.click(extraThumb(input))
  fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))
  await waitFor(() => expect(artOf(extraTile(input))).not.toBeNull())
  fireEvent.change(input, { target: { value: name } })
}

const savedPayload = () => api.gallerySavePack.mock.calls[0][0]

beforeEach(() => {
  vi.clearAllMocks()
  // `useLang` reads the config on mount; answer with no language so it never
  // schedules a state update the tests would have to await.
  api.getCrewCompanionConfig.mockResolvedValue({})
  api.galleryGetPackDetail.mockResolvedValue(null)
  api.galleryImportFile.mockResolvedValue(null)
  api.gallerySavePack.mockResolvedValue({ ok: true, value: { ...existing } })
})

afterEach(cleanup)

// ── Create mode: first render ──────────────────────────────────────────────

describe('PackEditor — create mode', () => {
  it('opens on the create title with every slot empty and saving refused', () => {
    renderEditor()

    expect(screen.getByText('Create New Pack')).toBeInTheDocument()
    // Idle is the only required slot, so it is the only thing "Missing:" names.
    expect(screen.getByText('Missing: Idle')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(artOf(slotFor('Idle'))).toBeNull()
  })

  it('offers a tile for every authorable slot and no more', () => {
    renderEditor()

    // Required + status + breathing + random-fixed + the add-clip affordance. The
    // five moods are still slots in state but have deliberately no tile.
    for (const label of ['Idle', 'Done', 'Error', 'inhale', 'hold', 'exhale', 'Walking']) {
      expect(slotFor(label)).toBeInTheDocument()
    }
    expect(slotFor('Add clip')).toBeInTheDocument()
    for (const mood of ['happy', 'sleepy', 'curious']) {
      expect(screen.queryByText(mood)).toBeNull()
    }
  })

  it('routes the back button and the footer cancel to onCancel', () => {
    const { onCancel } = renderEditor()

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(2)
  })
})

// ── The slot popover ───────────────────────────────────────────────────────

describe('PackEditor — slot popover', () => {
  it('toggles open and shut on the same tile', () => {
    renderEditor()

    fireEvent.click(slotFor('Idle'))
    expect(within(popover()).getByRole('button', { name: 'Select File' })).toBeInTheDocument()

    fireEvent.click(slotFor('Idle'))
    expect(popoverIsOpen()).toBe(false)
  })

  it('opens from the keyboard on Enter and on Space', () => {
    renderEditor()

    fireEvent.keyDown(slotFor('Idle'), { key: 'Enter' })
    expect(popoverIsOpen()).toBe(true)

    fireEvent.click(slotFor('Idle')) // shut it again
    fireEvent.keyDown(slotFor('Idle'), { key: ' ' })
    expect(popoverIsOpen()).toBe(true)
  })

  it('ignores unrelated keys on a tile', () => {
    renderEditor()

    fireEvent.keyDown(slotFor('Idle'), { key: 'a' })
    expect(popoverIsOpen()).toBe(false)
  })

  it('closes on a mousedown outside itself but not inside', () => {
    renderEditor()

    fireEvent.click(slotFor('Idle'))
    fireEvent.mouseDown(popover())
    expect(popoverIsOpen()).toBe(true)

    fireEvent.mouseDown(document.body)
    expect(popoverIsOpen()).toBe(false)
  })

  it('hangs below its anchor and clamps to the left window edge', () => {
    renderEditor()

    fireEvent.click(slotFor('Idle'))
    // happy-dom reports a zero rect, which centres the 180px popover off-screen to
    // the left — the clamp is what keeps it reachable.
    expect(popover().style.top).toBe('4px')
    expect(popover().style.bottom).toBe('')
    expect(popover().style.left).toBe('4px')
  })

  it('flips above its anchor and clamps to the right window edge near the bottom', () => {
    renderEditor()

    const rect = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
      top: window.innerHeight - 20,
      left: window.innerWidth - 10,
      width: 100,
      height: 100,
      right: 0,
      bottom: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect)
    try {
      fireEvent.click(slotFor('Idle'))
      expect(popover().style.top).toBe('')
      expect(popover().style.bottom).toBe('24px')
      expect(popover().style.left).toBe(`${window.innerWidth - 184}px`)
    } finally {
      rect.mockRestore()
    }
  })

  it('offers no reuse source or clear action while every slot is empty', () => {
    renderEditor()

    fireEvent.click(slotFor('Idle'))
    expect(within(popover()).queryByText('Use same animation as another state')).toBeNull()
    expect(within(popover()).queryByRole('button', { name: 'Clear' })).toBeNull()
  })

  it('paints hover feedback onto a popover row', () => {
    renderEditor()

    fireEvent.click(slotFor('Idle'))
    const row = within(popover()).getByRole('button', { name: 'Select File' })
    fireEvent.mouseEnter(row)
    expect(row.style.background).toBe('var(--cc-input-bg)')
    // The hover-OFF branch is deliberately not asserted: React 17+ synthesizes
    // onMouseLeave from `mouseout` at the root, and neither a bare `mouseleave`
    // (does not bubble) nor `mouseOut` with an explicit relatedTarget reaches the
    // handler reliably under jsdom. Asserting it produced a false failure, so the
    // reset path is left uncovered rather than pinned with a flaky event.
  })
})

// ── Picking art ────────────────────────────────────────────────────────────

describe('PackEditor — picking art for a slot', () => {
  it('shows a spinner while the picker is open, then the chosen SVG', async () => {
    renderEditor()

    let resolvePick!: (v: unknown) => void
    api.galleryImportFile.mockReturnValueOnce(new Promise((r) => { resolvePick = r }))

    fireEvent.click(slotFor('Idle'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))
    expect(slotFor('Idle').querySelector('.animate-spin')).not.toBeNull()

    await act(async () => { resolvePick(imported(SVG)) })

    expect(slotFor('Idle').querySelector('.animate-spin')).toBeNull()
    const img = artOf(slotFor('Idle'))
    expect(img).not.toBeNull()
    expect(decodeURIComponent(img?.getAttribute('src') ?? '')).toContain('<rect')
    // The required-slot hint clears, and a name is the only thing still missing.
    expect(screen.queryByText('Missing: Idle')).toBeNull()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('treats a cancelled picker as a no-op, with no error banner', async () => {
    renderEditor()

    api.galleryImportFile.mockResolvedValueOnce(null)
    fireEvent.click(slotFor('Idle'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))

    await waitFor(() => expect(slotFor('Idle').querySelector('.animate-spin')).toBeNull())
    expect(artOf(slotFor('Idle'))).toBeNull()
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
  })

  it("surfaces the picker's own refusal reason, and lets the user dismiss it", async () => {
    renderEditor()

    api.galleryImportFile.mockResolvedValueOnce({ ok: false, error: 'that is a .txt' })
    fireEvent.click(slotFor('Idle'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))

    expect(await screen.findByText('that is a .txt')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByText('that is a .txt')).toBeNull()
  })

  it('falls back to the generic invalid-file message when the refusal carries none', async () => {
    renderEditor()

    api.galleryImportFile.mockResolvedValueOnce({ ok: false, error: '' })
    fireEvent.click(slotFor('Idle'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))

    expect(await screen.findByText('Invalid file')).toBeInTheDocument()
  })

  it('reports a thrown picker error by its message, and a non-Error by the fallback', async () => {
    renderEditor()

    api.galleryImportFile.mockRejectedValueOnce(new Error('picker exploded'))
    fireEvent.click(slotFor('Idle'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))
    expect(await screen.findByText('picker exploded')).toBeInTheDocument()

    // A thrown non-Error narrows to '' and must not blank the banner.
    api.galleryImportFile.mockRejectedValueOnce({ nope: true })
    fireEvent.click(slotFor('Done'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Select File' }))
    expect(await screen.findByText('Could not import that file')).toBeInTheDocument()
  })

  it('renders a Lottie slot as an icon rather than an <img>', async () => {
    renderEditor()

    await fillSlot('Idle', LOTTIE, 'lottie')

    expect(artOf(slotFor('Idle'))).toBeNull()
    expect(slotFor('Idle').querySelector('.lucide-film')).not.toBeNull()
  })

  it('copies another slot\u2019s art through "use same animation as"', async () => {
    renderEditor()

    await fillSlot('Idle', SVG)

    fireEvent.click(slotFor('Done'))
    expect(within(popover()).getByText('Use same animation as another state')).toBeInTheDocument()
    fireEvent.click(within(popover()).getByRole('button', { name: 'Idle' }))

    expect(decodeURIComponent(artOf(slotFor('Done'))?.getAttribute('src') ?? '')).toContain('<rect')
    // A source slot never offers itself.
    fireEvent.click(slotFor('Idle'))
    expect(within(popover()).queryByRole('button', { name: 'Idle' })).toBeNull()
    expect(within(popover()).getByRole('button', { name: 'Done' })).toBeInTheDocument()
  })

  it('clears a filled slot, and the clear action disappears with it', async () => {
    renderEditor()

    await fillSlot('Idle', SVG)

    fireEvent.click(slotFor('Idle'))
    fireEvent.click(within(popover()).getByRole('button', { name: 'Clear' }))
    expect(artOf(slotFor('Idle'))).toBeNull()
    expect(screen.getByText('Missing: Idle')).toBeInTheDocument()

    fireEvent.click(slotFor('Idle'))
    expect(within(popover()).queryByRole('button', { name: 'Clear' })).toBeNull()
  })
})

// ── Random extras ──────────────────────────────────────────────────────────

describe('PackEditor — random extras', () => {
  it('adds an extra from a click and from the keyboard, and removes it again', () => {
    renderEditor()

    fireEvent.click(slotFor('Add clip'))
    expect(screen.getAllByPlaceholderText('name')).toHaveLength(1)

    fireEvent.keyDown(slotFor('Add clip'), { key: 'Enter' })
    expect(screen.getAllByPlaceholderText('name')).toHaveLength(2)

    fireEvent.keyDown(slotFor('Add clip'), { key: 'x' })
    expect(screen.getAllByPlaceholderText('name')).toHaveLength(2)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    expect(screen.getAllByPlaceholderText('name')).toHaveLength(1)
  })

  it('keeps a typed extra name', () => {
    renderEditor()

    fireEvent.click(slotFor('Add clip'))
    const input = screen.getByPlaceholderText('name')
    fireEvent.change(input, { target: { value: 'wave' } })
    expect((input as HTMLInputElement).value).toBe('wave')
  })
})

// ── Saving a new pack ──────────────────────────────────────────────────────

describe('PackEditor — saving a new pack', () => {
  it('mints an id, trims the fields, and hands the saved meta back', async () => {
    const { onSave } = renderEditor()

    await fillSlot('Idle', SVG)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: '  Bramble  ' } })
    fireEvent.change(screen.getByPlaceholderText('Your Name'), { target: { value: ' Ada ' } })
    fireEvent.change(screen.getByPlaceholderText(/A cute orange cat/), { target: { value: ' mossy ' } })

    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    fireEvent.click(save)

    // A brand-new pack never asks overwrite-or-new; it saves straight through.
    expect(screen.queryByText('Overwrite existing or save as new?')).toBeNull()
    await waitFor(() => expect(onSave).toHaveBeenCalledWith({ ...existing }))

    const payload = savedPayload()
    expect(payload.meta.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-/i)
    expect(payload.meta).toMatchObject({ name: 'Bramble', author: 'Ada', description: 'mossy', format: 'svg' })
    expect(payload.states).toEqual({ idle: SVG })
    expect(payload.moods).toEqual({})
    expect(payload.random).toEqual({})
  })

  it('falls back to an unknown author and records a Lottie pack as Lottie', async () => {
    renderEditor()

    await fillSlot('Idle', LOTTIE, 'lottie')
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.gallerySavePack).toHaveBeenCalled())
    expect(savedPayload().meta).toMatchObject({ author: 'Unknown', format: 'lottie' })
  })

  it('carries optional slots and named extras into the right maps', async () => {
    renderEditor()

    await fillSlot('Idle', SVG)
    await fillSlot('Done', SVG2)
    fireEvent.click(slotFor('Add clip'))
    await fillExtra(screen.getByPlaceholderText('name'), 'wave', SVG2)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.gallerySavePack).toHaveBeenCalled())

    expect(savedPayload().states).toEqual({ idle: SVG, done: SVG2 })
    expect(savedPayload().random).toEqual({ wave: SVG2 })
  })

  it('drops an unnamed or artless extra instead of saving a blank clip', async () => {
    renderEditor()

    await fillSlot('Idle', SVG)
    // Two extras: one with art but no name, one named but never filled.
    fireEvent.click(slotFor('Add clip'))
    await fillExtra(screen.getByPlaceholderText('name'), '', SVG2)
    fireEvent.click(slotFor('Add clip'))
    fireEvent.change(screen.getAllByPlaceholderText('name')[1], { target: { value: 'empty' } })
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.gallerySavePack).toHaveBeenCalled())
    expect(savedPayload().random).toEqual({})
  })

  it('refuses two extras that share a name rather than losing one clip', async () => {
    renderEditor()

    await fillSlot('Idle', SVG)
    fireEvent.click(slotFor('Add clip'))
    await fillExtra(screen.getAllByPlaceholderText('name')[0], 'wave', SVG)
    fireEvent.click(slotFor('Add clip'))
    await fillExtra(screen.getAllByPlaceholderText('name')[1], ' wave ', SVG2)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText(/Two extras can't share the name "wave"/)).toBeInTheDocument()
    expect(api.gallerySavePack).not.toHaveBeenCalled()
    // The editor stays usable rather than stuck in "Saving…".
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('refuses an extra named after a built-in slot', async () => {
    renderEditor()

    await fillSlot('Idle', SVG)
    fireEvent.click(slotFor('Add clip'))
    await fillExtra(screen.getByPlaceholderText('name'), 'idle', SVG2)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText(/"idle" is a built-in slot name/)).toBeInTheDocument()
    expect(api.gallerySavePack).not.toHaveBeenCalled()
  })

  it("shows the store's refusal reason and leaves the work in the editor", async () => {
    const { onSave } = renderEditor()

    api.gallerySavePack.mockResolvedValueOnce({ ok: false, error: 'disk full' })
    await fillSlot('Idle', SVG)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('disk full')).toBeInTheDocument()
    expect(onSave).not.toHaveBeenCalled()
    expect(artOf(slotFor('Idle'))).not.toBeNull()
  })

  it('falls back to the generic save-failed message for a reasonless refusal', async () => {
    renderEditor()

    api.gallerySavePack.mockResolvedValueOnce({ ok: false })
    await fillSlot('Idle', SVG)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Save failed')).toBeInTheDocument()
  })

  it('reports a thrown save by its message', async () => {
    renderEditor()

    api.gallerySavePack.mockRejectedValueOnce(new Error('gateway down'))
    await fillSlot('Idle', SVG)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('gateway down')).toBeInTheDocument()
  })

  it('accepts a bare meta response, without the {ok, value} wrapper', async () => {
    const { onSave } = renderEditor()

    api.gallerySavePack.mockResolvedValueOnce({ ...existing, id: 'bare' })
    await fillSlot('Idle', SVG)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ id: 'bare' })))
  })

  it('shows "Saving…" and ignores a second click while one save is in flight', async () => {
    const { onSave } = renderEditor()

    let finish!: (v: unknown) => void
    api.gallerySavePack.mockReturnValueOnce(new Promise((r) => { finish = r }))
    await fillSlot('Idle', SVG)
    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Blob' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    const saving = screen.getByRole('button', { name: 'Saving…' })
    expect(saving).toBeDisabled()
    fireEvent.click(saving)
    expect(api.gallerySavePack).toHaveBeenCalledTimes(1)

    await act(async () => { finish({ ok: true, value: { ...existing } }) })
    expect(onSave).toHaveBeenCalledTimes(1)
  })
})

// ── Edit mode ──────────────────────────────────────────────────────────────

describe('PackEditor — edit mode', () => {
  const detail = {
    animations: {
      idle: SVG,
      walking: { content: LOTTIE, format: 'lottie' as const },
      wave: SVG2,
    },
    randomNames: ['wave', 'missing-art'],
  }

  it('pre-fills the header, the slots and the named extras from the pack detail', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce(detail)
    renderEditor(existing)

    expect(await screen.findByText(/Edit Pack/)).toBeInTheDocument()
    expect(api.galleryGetPackDetail).toHaveBeenCalledWith('pack-42')

    await waitFor(() => expect(artOf(slotFor('Idle'))).not.toBeNull())
    expect((screen.getByPlaceholderText('My Custom Avatar') as HTMLInputElement).value).toBe('Bramble')
    expect((screen.getByPlaceholderText('Your Name') as HTMLInputElement).value).toBe('Ada')
    // Walking arrived as an object entry declaring Lottie, so it gets the icon.
    expect(artOf(slotFor('Walking'))).toBeNull()
    expect(slotFor('Walking').querySelector('.lucide-film')).not.toBeNull()
    // Only the random name that actually had art becomes an extra.
    const names = screen.getAllByPlaceholderText('name') as HTMLInputElement[]
    expect(names.map((n) => n.value)).toEqual(['wave'])
    // Nothing is missing and nothing has changed, so there is nothing to save.
    expect(screen.queryByText(/^Missing:/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('asks overwrite-or-new once the pack is dirty, and overwrites in place', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce(detail)
    const { onSave } = renderEditor(existing)
    await waitFor(() => expect(artOf(slotFor('Idle'))).not.toBeNull())

    fireEvent.change(screen.getByPlaceholderText(/A cute orange cat/), { target: { value: 'now mossier' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(screen.getByText('Overwrite existing or save as new?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Overwrite' }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(savedPayload().meta.id).toBe('pack-42')
    expect(savedPayload().meta.description).toBe('now mossier')
    expect(savedPayload().random).toEqual({ wave: SVG2 })
    expect(screen.queryByText('Overwrite existing or save as new?')).toBeNull()
  })

  it('mints a fresh id for "save as new"', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce(detail)
    renderEditor(existing)
    await waitFor(() => expect(artOf(slotFor('Idle'))).not.toBeNull())

    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Bramble II' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save as New' }))

    await waitFor(() => expect(api.gallerySavePack).toHaveBeenCalled())
    expect(savedPayload().meta.id).not.toBe('pack-42')
    expect(savedPayload().meta.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-/i)
  })

  it('dismisses the dialog without saving', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce(detail)
    renderEditor(existing)
    await waitFor(() => expect(artOf(slotFor('Idle'))).not.toBeNull())

    fireEvent.change(screen.getByPlaceholderText('My Custom Avatar'), { target: { value: 'Bramble II' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    // Two "Cancel"s are on screen once the dialog is up; the dialog's is the last.
    const cancels = screen.getAllByRole('button', { name: 'Cancel' })
    fireEvent.click(cancels[cancels.length - 1])

    expect(screen.queryByText('Overwrite existing or save as new?')).toBeNull()
    expect(api.gallerySavePack).not.toHaveBeenCalled()
  })

  it('removing a restored extra drops its art from the next save', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce(detail)
    renderEditor(existing)
    await waitFor(() => expect(screen.getAllByPlaceholderText('name')).toHaveLength(1))

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.queryByPlaceholderText('name')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(screen.getByRole('button', { name: 'Overwrite' }))
    await waitFor(() => expect(api.gallerySavePack).toHaveBeenCalled())
    expect(savedPayload().random).toEqual({})
  })

  it('leaves the slots empty when the pack has no animations at all', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce({})
    renderEditor(existing)

    expect(await screen.findByText('Missing: Idle')).toBeInTheDocument()
    expect(artOf(slotFor('Idle'))).toBeNull()
  })

  it('ignores a non-array randomNames instead of trusting the payload', async () => {
    api.galleryGetPackDetail.mockResolvedValueOnce({
      animations: { idle: SVG },
      randomNames: 'wave',
    })
    renderEditor(existing)

    await waitFor(() => expect(artOf(slotFor('Idle'))).not.toBeNull())
    expect(screen.queryByPlaceholderText('name')).toBeNull()
  })

  it('reports a failed detail read, by message and by fallback', async () => {
    api.galleryGetPackDetail.mockRejectedValueOnce(new Error('detail 500'))
    const first = renderEditor(existing)
    expect(await screen.findByText('detail 500')).toBeInTheDocument()
    first.unmount()

    api.galleryGetPackDetail.mockRejectedValueOnce('just a string')
    renderEditor(existing)
    // errorText() narrows a thrown string, so that is what shows.
    expect(await screen.findByText('just a string')).toBeInTheDocument()
  })

  it('falls back to the load-failed message when the throw carries no text', async () => {
    api.galleryGetPackDetail.mockRejectedValueOnce({ code: 500 })
    renderEditor(existing)

    expect(await screen.findByText("Could not load that avatar's data")).toBeInTheDocument()
  })
})
