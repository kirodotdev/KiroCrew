/**
 * Mochi PackEditor — first tests for `apps/mochi/src/renderer/PackEditor.tsx`.
 *
 * The editor is the WYSIWYG surface for authoring an SVG/Lottie appearance pack
 * and had no test at all, so every behaviour below is pinned here for the first
 * time: the create/edit headers, the three slot grids (required / peek /
 * moods), the per-slot popover (select file, "use same as", clear, outside
 * click, keyboard open), the loading and error states of a file import, the
 * edit-mode prefill from `galleryGetPackDetail` — including the sprite slot
 * that must NOT be mislabelled as an `.svg` — and the whole save path with its
 * overwrite / save-as-new dialog.
 *
 * The editor talks to the Electron main process through exactly one seam
 * (`api` in `mochiApi`), so that module is the single mock. Nothing here waits
 * on real time or a real animation frame: every state change is driven by a
 * click, a key, or a promise the test resolves itself.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import type { PackMeta } from '../apps/mochi/src/shared/appearanceTypes'

/** One slot's art as the importer hands it back. */
interface ImportedFile {
  content: string
  filename: string
  format: 'svg' | 'lottie'
}

/** The payload `gallerySavePack` receives. */
interface SavePayload {
  meta: {
    id: string
    name: string
    author: string
    description: string
    format: string
  }
  states: Record<string, string>
  moods: Record<string, string>
}

const galleryImportFile = vi.fn<() => Promise<unknown>>()
const gallerySavePack = vi.fn<(pack: unknown) => Promise<unknown>>()
const galleryGetPackDetail = vi.fn<(packId: string) => Promise<unknown>>()

vi.mock('../apps/mochi/src/mochiApi', () => ({
  api: {
    galleryImportFile,
    gallerySavePack,
    galleryGetPackDetail,
  },
}))

const { PackEditor } = await import('../apps/mochi/src/renderer/PackEditor')

const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4" /></svg>'
const REQUIRED = ['Idle', 'Walking', 'Thinking', 'Working', 'Error', 'Offline']

/** An svg slot payload with a per-slot marker so a save payload is checkable. */
function svgFile(marker: string): ImportedFile {
  return { content: `${SVG}<!--${marker}-->`, filename: `${marker}.svg`, format: 'svg' }
}

/** An existing pack, as the gallery hands one to the editor. */
function existing(over: Partial<PackMeta> = {}): PackMeta {
  return {
    id: 'pack-legacy-default',
    name: 'Legacy Default',
    author: 'Ada',
    description: 'a tidy little robot',
    type: 'custom',
    format: 'svg',
    thumbnail: 'thumb.png',
    ...over,
  }
}

/** A `galleryGetPackDetail` reply filling every required state. */
function detailWithRequiredStates(extra: Record<string, unknown> = {}) {
  const animations: Record<string, unknown> = {}
  for (const k of ['idle', 'walking', 'thinking', 'working', 'error', 'offline']) {
    animations[k] = { content: `${SVG}<!--${k}-->`, format: 'svg' }
  }
  return { animations: { ...animations, ...extra } }
}

/** A promise plus its resolver, so a pending api call can be held open. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

/** The clickable card for one slot, addressed by its visible label. */
function slot(label: string): HTMLElement {
  return screen.getByRole('button', { name: label })
}

/** Open a slot's action popover and return the menu. */
function openSlot(label: string): HTMLElement {
  fireEvent.click(slot(label))
  return screen.getByRole('menu')
}

/** Drive the importer for one slot and wait until its art lands. */
async function importInto(label: string, file: ImportedFile): Promise<void> {
  galleryImportFile.mockResolvedValueOnce(file)
  const menu = openSlot(label)
  fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
  await waitFor(() => {
    expect(slot(label).querySelector(file.format === 'svg' ? 'img' : 'svg.lucide-film')).toBeTruthy()
  })
}

/** Copy an already-filled slot into another via the popover's "use same as". */
function copyInto(target: string, source: string): void {
  const menu = openSlot(target)
  fireEvent.click(within(menu).getByRole('menuitem', { name: source }))
}

/** Type into one of the header's text inputs. */
function fillField(placeholder: string, value: string): void {
  fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } })
}

/** The footer's Save button (never the dialog's "Save as New"). */
function saveButton(): HTMLElement {
  return screen.getByRole('button', { name: /^Save$/ })
}

/**
 * Fill all six required states plus a name, which is the minimum the footer
 * accepts. Only `idle` goes through the importer; the rest are copied, which is
 * both faster and the path a real author uses.
 */
async function makeSaveable(name = 'Bright Pack'): Promise<void> {
  await importInto('Idle', svgFile('idle'))
  for (const label of REQUIRED.slice(1)) copyInto(label, 'Idle')
  fillField('My Custom Mochi', name)
  await waitFor(() => expect(saveButton()).not.toBeDisabled())
}

/** Pin the anchor rect the popover positions against. */
function stubAnchorRect(rect: Partial<DOMRect>) {
  const base = {
    top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0, x: 0, y: 0,
    toJSON: () => ({}),
  }
  return vi
    .spyOn(Element.prototype, 'getBoundingClientRect')
    .mockReturnValue({ ...base, ...rect } as DOMRect)
}

beforeEach(() => {
  galleryImportFile.mockReset()
  gallerySavePack.mockReset()
  galleryGetPackDetail.mockReset()
  galleryImportFile.mockResolvedValue(null)
  gallerySavePack.mockResolvedValue({ ok: true, value: existing({ id: 'saved-1' }) })
  galleryGetPackDetail.mockResolvedValue(null)
})

describe('PackEditor — create mode', () => {
  it('renders the create heading and every slot in the three grids', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    expect(screen.getByText('Create New Pack')).toBeTruthy()
    expect(screen.getByText('Required States')).toBeTruthy()
    expect(screen.getByText('Peek Animations')).toBeTruthy()
    expect(screen.getByText('Mood Animations')).toBeTruthy()

    for (const label of [...REQUIRED, 'Peeking', 'Peek & Think', 'Happy', 'Sleepy', 'Curious', 'Busy', 'Scared']) {
      expect(slot(label)).toBeTruthy()
    }
    // Nothing is filled yet, so no slot carries art.
    expect(document.querySelectorAll('img').length).toBe(0)
  })

  it('blocks save until every required state and a name are present', async () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    expect(saveButton()).toBeDisabled()
    expect(screen.getByText(/Missing: Idle, Walking, Thinking, Working, Error, Offline/)).toBeTruthy()

    await importInto('Idle', svgFile('idle'))
    expect(screen.getByText(/Missing: Walking, Thinking, Working, Error, Offline/)).toBeTruthy()
    expect(saveButton()).toBeDisabled()

    for (const label of REQUIRED.slice(1)) copyInto(label, 'Idle')
    // All six filled, but the name is still empty.
    expect(screen.queryByText(/^Missing:/)).toBeNull()
    expect(saveButton()).toBeDisabled()

    fillField('My Custom Mochi', '  Bright Pack  ')
    expect(saveButton()).not.toBeDisabled()
  })

  it('never enables save on whitespace alone in the name', async () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await importInto('Idle', svgFile('idle'))
    for (const label of REQUIRED.slice(1)) copyInto(label, 'Idle')

    fillField('My Custom Mochi', '   ')
    expect(saveButton()).toBeDisabled()
  })

  it('forwards the trimmed pack, its states and its moods to the backend', async () => {
    const onSave = vi.fn()
    render(<PackEditor onSave={onSave} onCancel={vi.fn()} />)

    await makeSaveable('  Bright Pack  ')
    fillField('Your Name', '  Ada  ')
    fillField('e.g. A cute orange cat, a pixel robot, a fluffy bunny...', '  a tidy robot  ')
    copyInto('Peeking', 'Idle')
    copyInto('Happy', 'Idle')

    fireEvent.click(saveButton())
    await waitFor(() => expect(gallerySavePack).toHaveBeenCalledTimes(1))

    const payload = gallerySavePack.mock.calls[0][0] as SavePayload
    expect(payload.meta).toEqual({
      id: '',
      name: 'Bright Pack',
      author: 'Ada',
      description: 'a tidy robot',
      format: 'svg',
    })
    // Peek states ride along in `states`; only moods land in `moods`.
    expect(Object.keys(payload.states).sort()).toEqual(
      ['error', 'idle', 'offline', 'peeking', 'thinking', 'walking', 'working'],
    )
    expect(Object.keys(payload.moods)).toEqual(['happy'])
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(existing({ id: 'saved-1' })))
  })

  it('substitutes the unknown-author label when the author box is left empty', async () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await makeSaveable()

    fireEvent.click(saveButton())
    await waitFor(() => expect(gallerySavePack).toHaveBeenCalled())
    expect((gallerySavePack.mock.calls[0][0] as SavePayload).meta.author).toBe('Unknown')
  })

  it('derives the pack format from the idle slot, so a Lottie idle saves as lottie', async () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    await importInto('Idle', { content: '{"v":"5.7.4"}', filename: 'idle.json', format: 'lottie' })
    // A Lottie slot has no rasterisable thumbnail, so it shows the film glyph.
    expect(slot('Idle').querySelector('svg.lucide-film')).toBeTruthy()
    for (const label of REQUIRED.slice(1)) copyInto(label, 'Idle')
    fillField('My Custom Mochi', 'Lottie Pack')

    fireEvent.click(saveButton())
    await waitFor(() => expect(gallerySavePack).toHaveBeenCalled())
    expect((gallerySavePack.mock.calls[0][0] as SavePayload).meta.format).toBe('lottie')
  })

  it('reports a rejected save and stays mounted so the work is not lost', async () => {
    const onSave = vi.fn()
    gallerySavePack.mockResolvedValue({ ok: false, error: 'Disk is full' })
    render(<PackEditor onSave={onSave} onCancel={vi.fn()} />)
    await makeSaveable()

    fireEvent.click(saveButton())
    await waitFor(() => expect(screen.getByText('Disk is full')).toBeTruthy())
    expect(onSave).not.toHaveBeenCalled()
    // The editor is still there with the author's slots intact.
    expect(slot('Idle').querySelector('img')).toBeTruthy()
    expect(saveButton()).not.toBeDisabled()
  })

  it('falls back to the generic save error when the backend gives no reason', async () => {
    gallerySavePack.mockResolvedValue({ ok: false })
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await makeSaveable()

    fireEvent.click(saveButton())
    await waitFor(() => expect(screen.getByText('Save failed')).toBeTruthy())
  })

  it('surfaces a thrown save instead of failing silently', async () => {
    gallerySavePack.mockRejectedValue(new Error('ipc channel closed'))
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await makeSaveable()

    fireEvent.click(saveButton())
    await waitFor(() => expect(screen.getByText('ipc channel closed')).toBeTruthy())
  })

  it('falls back to the generic save message for a reason-less throw', async () => {
    gallerySavePack.mockRejectedValue(new Error(''))
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await makeSaveable()

    fireEvent.click(saveButton())
    await waitFor(() => expect(screen.getByText('Save failed')).toBeTruthy())
  })

  it('accepts a bare pack meta reply, not only the wrapped result shape', async () => {
    const meta = existing({ id: 'bare-1' })
    gallerySavePack.mockResolvedValue(meta)
    const onSave = vi.fn()
    render(<PackEditor onSave={onSave} onCancel={vi.fn()} />)
    await makeSaveable()

    fireEvent.click(saveButton())
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(meta))
  })

  it('reports cancel to the owning page without saving', () => {
    const onCancel = vi.fn()
    render(<PackEditor onSave={vi.fn()} onCancel={onCancel} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(gallerySavePack).not.toHaveBeenCalled()
  })

  it('does not consult the backend for pack detail when there is no pack yet', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    expect(galleryGetPackDetail).not.toHaveBeenCalled()
  })
})

describe('PackEditor — slot popover', () => {
  it('opens on a slot click and lists only the actions that apply to an empty slot', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Walking')
    expect(menu.getAttribute('aria-label')).toBe('Walking')
    expect(within(menu).getByRole('menuitem', { name: 'Select File' })).toBeTruthy()
    // Nothing else is filled, so there is no copy source and nothing to clear.
    expect(within(menu).queryByText('Use same animation as another state')).toBeNull()
    expect(within(menu).queryByRole('menuitem', { name: 'Clear' })).toBeNull()
  })

  it('offers every other filled slot as a copy source, and empties one on clear', async () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await importInto('Idle', svgFile('idle'))

    const menu = openSlot('Thinking')
    expect(within(menu).getByText('Use same animation as another state')).toBeTruthy()
    // Only `idle` has art, and a slot is never offered itself.
    expect(within(menu).getAllByRole('menuitem').map((b) => b.textContent?.trim()))
      .toEqual(['Select File', 'Idle'])

    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Idle' }))
    expect(slot('Thinking').querySelector('img')).toBeTruthy()
    expect(screen.queryByRole('menu')).toBeNull()

    const filledMenu = openSlot('Thinking')
    fireEvent.click(within(filledMenu).getByRole('menuitem', { name: 'Clear' }))
    expect(slot('Thinking').querySelector('img')).toBeNull()
  })

  it('closes on an outside mousedown but not on a click inside itself', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Offline')
    fireEvent.mouseDown(menu)
    expect(screen.getByRole('menu')).toBeTruthy()

    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('toggles shut when the same slot fires a second bare click', () => {
    // A bare click (no preceding mousedown) isolates the component's own toggle
    // branch from the document-level outside-click closer.
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    openSlot('Error')
    fireEvent.click(slot('Error'))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('moves the popover to a different slot when another one is clicked', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    openSlot('Error')
    fireEvent.click(slot('Happy'))
    expect(screen.getByRole('menu').getAttribute('aria-label')).toBe('Happy')
  })

  it('opens from the keyboard on Enter and on Space, and ignores other keys', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    fireEvent.keyDown(slot('Peeking'), { key: 'Enter' })
    expect(screen.getByRole('menu').getAttribute('aria-label')).toBe('Peeking')

    fireEvent.keyDown(slot('Peeking'), { key: 'Escape' })
    expect(screen.getByRole('menu')).toBeTruthy()

    fireEvent.mouseDown(document.body)
    fireEvent.keyDown(slot('Sleepy'), { key: ' ' })
    expect(screen.getByRole('menu').getAttribute('aria-label')).toBe('Sleepy')
  })

  it('opens from the keyboard in the required grid too, not just the optional ones', () => {
    // Each of the three grids carries its own handler, so the required grid is
    // asserted separately rather than assumed from the peek/mood grids.
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    fireEvent.keyDown(slot('Thinking'), { key: 'Enter' })
    expect(screen.getByRole('menu').getAttribute('aria-label')).toBe('Thinking')

    fireEvent.mouseDown(document.body)
    fireEvent.keyDown(slot('Walking'), { key: ' ' })
    expect(screen.getByRole('menu').getAttribute('aria-label')).toBe('Walking')
  })

  it('highlights each kind of menu item as the pointer moves across the menu', async () => {
    const user = userEvent.setup()
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)
    await importInto('Idle', svgFile('idle'))

    const menu = openSlot('Idle')
    const select = within(menu).getByRole('menuitem', { name: 'Select File' })
    const clear = within(menu).getByRole('menuitem', { name: 'Clear' })

    // Only the highlight-on-enter is asserted. The un-highlight is written to
    // the leave event's `target`, which is whatever node the pointer actually
    // left (an icon, a text run) rather than the menu item, so the item's own
    // background is not reliably the thing that gets reset.
    await user.hover(select)
    expect(select.style.background).toBe('var(--bg-input)')
    await user.hover(clear)
    expect(clear.style.background).toBe('var(--bg-input)')
    await user.hover(select)

    fireEvent.mouseDown(document.body)
    // A copy-source item only exists on a slot that is not the source itself.
    const other = openSlot('Walking')
    const copy = within(other).getByRole('menuitem', { name: 'Idle' })
    await user.hover(copy)
    expect(copy.style.background).toBe('var(--bg-input)')
    await user.hover(within(other).getByRole('menuitem', { name: 'Select File' }))
  })

  it('ignores a key that bubbled up from inside the slot rather than the slot itself', () => {
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    // Checked in a required slot and in a mood slot, because each grid carries
    // its own copy of the guard.
    fireEvent.keyDown(within(slot('Idle')).getByText('Idle'), { key: 'Enter' })
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.keyDown(within(slot('Curious')).getByText('Curious'), { key: 'Enter' })
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('drops below the anchor when there is room and flips above when there is not', () => {
    const tall = stubAnchorRect({ top: 10, left: 200, width: 100, height: 100 })
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    let menu = openSlot('Idle')
    expect(menu.style.top).toBe('114px')
    expect(menu.style.bottom).toBe('')
    // Centred on the anchor: 200 + 50 - 90.
    expect(menu.style.left).toBe('160px')

    fireEvent.mouseDown(document.body)
    tall.mockReturnValue({
      top: window.innerHeight - 40, left: 200, width: 100, height: 40,
      right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect)
    menu = openSlot('Idle')
    expect(menu.style.top).toBe('')
    expect(menu.style.bottom).toBe(`${window.innerHeight - (window.innerHeight - 40) + 4}px`)
    tall.mockRestore()
  })

  it('keeps the popover inside the window on both edges', () => {
    const spy = stubAnchorRect({ top: 10, left: 0, width: 0, height: 20 })
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    // Centring would put it off the left edge, so it clamps to the 4px margin.
    expect(openSlot('Idle').style.left).toBe('4px')

    fireEvent.mouseDown(document.body)
    spy.mockReturnValue({
      top: 10, left: window.innerWidth, width: 0, height: 20,
      right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect)
    expect(openSlot('Idle').style.left).toBe(`${window.innerWidth - 184}px`)
    spy.mockRestore()
  })
})

describe('PackEditor — importing a file', () => {
  it('shows a spinner on the slot being imported and only there', async () => {
    const pending = deferred<ImportedFile>()
    galleryImportFile.mockReturnValue(pending.promise)
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Working')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
    await waitFor(() => expect(slot('Working').querySelector('svg.lucide-inline')).toBeTruthy())
    expect(slot('Idle').querySelector('svg.lucide-inline')).toBeNull()

    pending.resolve(svgFile('working'))
    await waitFor(() => expect(slot('Working').querySelector('img')).toBeTruthy())
    expect(slot('Working').querySelector('svg.lucide-inline')).toBeNull()
  })

  it('shows the same spinner in the peek and mood grids', async () => {
    // Each grid renders its own loading branch, so both are exercised.
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    for (const label of ['Peeking', 'Happy']) {
      const pending = deferred<ImportedFile>()
      galleryImportFile.mockReturnValue(pending.promise)
      const menu = openSlot(label)
      fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
      await waitFor(() => expect(slot(label).querySelector('svg.lucide-inline')).toBeTruthy())

      pending.resolve(svgFile(label.toLowerCase()))
      await waitFor(() => expect(slot(label).querySelector('img')).toBeTruthy())
    }
  })

  it('leaves the slot untouched and raises nothing when the picker is dismissed', async () => {
    galleryImportFile.mockResolvedValue(null)
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Working')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
    await waitFor(() => expect(galleryImportFile).toHaveBeenCalled())

    expect(slot('Working').querySelector('img')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
  })

  it('reports a rejected file, and the banner can be dismissed', async () => {
    galleryImportFile.mockResolvedValue({ ok: false, error: 'Not an SVG' })
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Working')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
    await waitFor(() => expect(screen.getByText('Not an SVG')).toBeTruthy())
    expect(slot('Working').querySelector('img')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Not an SVG')).toBeNull()
  })

  it('falls back to the generic invalid-file message when none is given', async () => {
    galleryImportFile.mockResolvedValue({ ok: false })
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Working')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
    await waitFor(() => expect(screen.getByText('Invalid file')).toBeTruthy())
  })

  it('surfaces a thrown import', async () => {
    galleryImportFile.mockRejectedValue(new Error('no such file'))
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Working')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
    await waitFor(() => expect(screen.getByText('no such file')).toBeTruthy())
  })

  it('falls back to the generic import message for a reason-less throw', async () => {
    galleryImportFile.mockRejectedValue(new Error(''))
    render(<PackEditor onSave={vi.fn()} onCancel={vi.fn()} />)

    const menu = openSlot('Working')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Select File' }))
    await waitFor(() => expect(screen.getByText('Failed to import file')).toBeTruthy())
  })
})

describe('PackEditor — edit mode', () => {
  it('prefills every slot the pack ships and treats a sprite slot as absent', async () => {
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates({
      happy: { content: '{"v":"5.7.4"}', format: 'lottie' },
      // A sprite pack is authored in SpriteImporter, so this editor has no
      // per-slot document to load — it must not claim the sheet is an svg.
      peeking: { content: 'iVBORw0KGgo=', format: 'sprite' },
    }))
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)

    expect(screen.getByText('Edit Pack')).toBeTruthy()
    await waitFor(() => expect(slot('Idle').querySelector('img')).toBeTruthy())
    for (const label of REQUIRED) expect(slot(label).querySelector('img')).toBeTruthy()
    expect(slot('Happy').querySelector('svg.lucide-film')).toBeTruthy()
    expect(slot('Peeking').querySelector('img')).toBeNull()
    expect(galleryGetPackDetail).toHaveBeenCalledWith('pack-legacy-default')

    // Header fields come straight off the pack meta.
    expect((screen.getByPlaceholderText('My Custom Mochi') as HTMLInputElement).value).toBe('Legacy Default')
    expect((screen.getByPlaceholderText('Your Name') as HTMLInputElement).value).toBe('Ada')
  })

  it('keeps save disabled until something actually changes', async () => {
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates())
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)

    await waitFor(() => expect(saveButton()).toBeDisabled())
    fillField('My Custom Mochi', 'Legacy Default v2')
    expect(saveButton()).not.toBeDisabled()
  })

  it('treats the flip switch as a change, even though the flag itself is not sent', async () => {
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates())
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)
    await waitFor(() => expect(saveButton()).toBeDisabled())

    const flip = screen.getByRole('switch', { name: 'Flip Horizontal' })
    fireEvent.click(flip)
    expect(flip.getAttribute('aria-checked')).toBe('true')
    expect(saveButton()).not.toBeDisabled()
  })

  it('asks overwrite-or-new before saving, and overwrite keeps the pack id', async () => {
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates())
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)
    await waitFor(() => expect(saveButton()).toBeDisabled())
    fillField('My Custom Mochi', 'Legacy Default v2')

    fireEvent.click(saveButton())
    expect(screen.getByText('Overwrite existing or save as new?')).toBeTruthy()
    expect(gallerySavePack).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Overwrite' }))
    await waitFor(() => expect(gallerySavePack).toHaveBeenCalledTimes(1))
    expect((gallerySavePack.mock.calls[0][0] as SavePayload).meta).toMatchObject({
      id: 'pack-legacy-default',
      name: 'Legacy Default v2',
    })
    expect(screen.queryByText('Overwrite existing or save as new?')).toBeNull()
  })

  it('clears the id when the author chooses save-as-new', async () => {
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates())
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)
    await waitFor(() => expect(saveButton()).toBeDisabled())
    fillField('My Custom Mochi', 'Forked Pack')

    fireEvent.click(saveButton())
    fireEvent.click(screen.getByRole('button', { name: 'Save as New' }))
    await waitFor(() => expect(gallerySavePack).toHaveBeenCalled())
    expect((gallerySavePack.mock.calls[0][0] as SavePayload).meta.id).toBe('')
  })

  it('saves nothing when the dialog is dismissed', async () => {
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates())
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)
    await waitFor(() => expect(saveButton()).toBeDisabled())
    fillField('My Custom Mochi', 'Legacy Default v2')

    fireEvent.click(saveButton())
    const dialogCancel = screen.getAllByRole('button', { name: 'Cancel' })
    fireEvent.click(dialogCancel[dialogCancel.length - 1])

    expect(screen.queryByText('Overwrite existing or save as new?')).toBeNull()
    expect(gallerySavePack).not.toHaveBeenCalled()
  })

  it('renders empty fields for a pack whose meta omits name, author and description', async () => {
    // A hand-written or older manifest can be missing these, and the editor is
    // written to fall back to empty strings rather than print "undefined".
    const sparse = { ...existing(), name: undefined, author: undefined, description: undefined }
    galleryGetPackDetail.mockResolvedValue(detailWithRequiredStates())
    render(<PackEditor existingPack={sparse as unknown as PackMeta} onSave={vi.fn()} onCancel={vi.fn()} />)

    await waitFor(() => expect(slot('Idle').querySelector('img')).toBeTruthy())
    expect((screen.getByPlaceholderText('My Custom Mochi') as HTMLInputElement).value).toBe('')
    expect((screen.getByPlaceholderText('Your Name') as HTMLInputElement).value).toBe('')
    // Nothing was typed, so this is still a pristine pack.
    expect(saveButton()).toBeDisabled()
  })

  it('reports a failed detail load instead of showing a silently empty grid', async () => {
    galleryGetPackDetail.mockRejectedValue(new Error('pack.json is unreadable'))
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('pack.json is unreadable')).toBeTruthy())
    expect(slot('Idle').querySelector('img')).toBeNull()
  })

  it('falls back to the generic load message when the failure carries no reason', async () => {
    galleryGetPackDetail.mockRejectedValue(new Error(''))
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('Failed to load pack data')).toBeTruthy())
  })

  it('leaves the grid empty and quiet when the backend has no detail for the pack', async () => {
    galleryGetPackDetail.mockResolvedValue({ animations: undefined })
    render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)

    await waitFor(() => expect(galleryGetPackDetail).toHaveBeenCalled())
    expect(slot('Idle').querySelector('img')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
  })

  it('does not touch state when the detail arrives after the editor is gone', async () => {
    const pending = deferred<unknown>()
    galleryGetPackDetail.mockReturnValue(pending.promise)
    const view = render(<PackEditor existingPack={existing()} onSave={vi.fn()} onCancel={vi.fn()} />)

    view.unmount()
    pending.resolve(detailWithRequiredStates())
    await pending.promise
    expect(screen.queryByText('Edit Pack')).toBeNull()
  })
})
