/**
 * The avatar builder's Library pane — list, select, import, delete.
 *
 * Four things here are the pane's whole reason to exist, and each is a mistake
 * it must not make: listing a pack the user cannot wear as if they could, losing
 * a just-imported pack because the grid was not re-read, deleting a pack a crew
 * is wearing without saying which crews, and reporting the client's own guess
 * where the server named the real problem.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'

const mockApi = vi.hoisted(() => ({
  appearances: {
    list: vi.fn(),
    detail: vi.fn(),
    importBundle: vi.fn(),
    remove: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import CrewAvatarLibraryTab from '../components/CrewAvatarLibraryTab'
import { BUILTIN_PACK_ID } from '../lib/appearancePacks/library'

const BUILTIN = {
  id: BUILTIN_PACK_ID,
  name: 'Kiro',
  author: 'Kiro Crew',
  description: 'The default companion.',
  type: 'builtin',
  format: 'svg',
}
const AURORA = {
  id: 'aurora',
  name: 'Aurora',
  author: 'zoe',
  description: 'a paper fox',
  type: 'custom',
  format: 'svg',
}
const NEBULA = { ...AURORA, id: 'nebula', name: 'Nebula', format: 'lottie' }
/** What the library holds AFTER an import — the fresh listing the import path
 *  re-reads to learn the installed pack's format. */
const IMPORTED_SVG = { ...AURORA, id: 'imported', name: 'Imported' }
const IMPORTED_LOTTIE = { ...IMPORTED_SVG, format: 'lottie' }

/** A rejection shaped the way `api/client`'s `j()` throws one. */
function apiError(status: number, message: string, body = '') {
  return Object.assign(new Error(message), { status, body })
}

function mount(selectedId: string | null = null) {
  const onSelect = vi.fn()
  const utils = render(
    <CrewAvatarLibraryTab open name="oncall" selectedId={selectedId} onSelect={onSelect} />,
  )
  return { ...utils, onSelect }
}

/** A picked file. happy-dom's File has no `text()`, so it is supplied. */
function bundleFile(contents: string, name = 'aurora.json', size?: number) {
  const file = new File([contents], name, { type: 'application/json' })
  Object.defineProperty(file, 'text', { value: () => Promise.resolve(contents) })
  if (size !== undefined) Object.defineProperty(file, 'size', { value: size })
  return file
}

const pick = (file: File) =>
  fireEvent.change(screen.getByTestId('avatar-pack-import-input'), { target: { files: [file] } })

const VALID_BUNDLE = JSON.stringify({
  kind: 'crew-companion-pack',
  version: 1,
  id: 'aurora',
  manifest: { meta: { id: 'aurora', format: 'svg' }, states: { idle: 'idle.svg' } },
  files: { 'idle.svg': '<svg/>' },
})

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.appearances.list.mockResolvedValue({ packs: [BUILTIN, AURORA, NEBULA] })
  mockApi.appearances.importBundle.mockResolvedValue({ ok: true, id: 'imported' })
  mockApi.appearances.remove.mockResolvedValue({ ok: true, id: 'aurora' })
})

describe('library pane — listing', () => {
  it('reads the library on open and renders one card per pack', async () => {
    mount()
    expect(screen.getByTestId('avatar-library-loading')).toBeInTheDocument()

    await waitFor(() => expect(screen.getByTestId(`avatar-pack-card-${BUILTIN_PACK_ID}`)).toBeInTheDocument())
    expect(screen.getByTestId('avatar-pack-card-aurora')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-pack-card-nebula')).toBeInTheDocument()
    expect(screen.getByText('Aurora')).toBeInTheDocument()
    // Aurora and Nebula share an author, so scope the credit to its own card.
    expect(within(screen.getByTestId('avatar-pack-card-aurora')).getByText('Author: zoe')).toBeInTheDocument()
    expect(screen.getByText('Built-in')).toBeInTheDocument()
  })

  it('draws a custom thumbnail from the slot route and the built-in one locally', async () => {
    mount()
    const thumb = await screen.findByTestId('avatar-pack-thumb-aurora')
    expect(thumb.getAttribute('src')).toBe('/api/appearances/aurora/slot/idle')
    // The built-in pack's art ships in this bundle; the slot route 404s for it.
    expect(screen.queryByTestId(`avatar-pack-thumb-${BUILTIN_PACK_ID}`)).toBeNull()
  })

  it('selects a wearable pack on click', async () => {
    const { onSelect } = mount()
    fireEvent.click(await screen.findByTestId('avatar-pack-select-aurora'))
    expect(onSelect).toHaveBeenCalledWith('aurora')
  })

  it('marks the pack the draft already wears', async () => {
    mount('aurora')
    await waitFor(() => expect(screen.getByTestId('avatar-pack-select-aurora')).toHaveAttribute('aria-selected', 'true'))
    expect(screen.getByTestId(`avatar-pack-select-${BUILTIN_PACK_ID}`)).toHaveAttribute('aria-selected', 'false')
  })

  it('asks for no art for a pack a crew cannot wear', async () => {
    // The slot route serves a lottie pack as `application/json` and a sprite pack
    // as a whole PNG sheet, so an <img> pointed at either draws the browser's
    // broken-image glyph — which reads as a damaged pack rather than an
    // unsupported one.
    mount()
    await screen.findByTestId('avatar-pack-card-nebula')
    expect(screen.getByTestId('avatar-pack-noart-nebula')).toBeInTheDocument()
    expect(screen.queryByTestId('avatar-pack-thumb-nebula')).toBeNull()
    // The wearable one still fetches its real frame.
    expect(screen.getByTestId('avatar-pack-thumb-aurora').getAttribute('src')).toBe(
      '/api/appearances/aurora/slot/idle',
    )
  })

  it('greys a non-SVG pack and refuses to select it', async () => {
    // A crew's face is an <img>: it cannot play Lottie, and core ships no
    // player. Listing the pack anyway is what stops the import reading as a
    // failure.
    const { onSelect } = mount()
    const card = await screen.findByTestId('avatar-pack-select-nebula')
    expect(card).toBeDisabled()
    expect(screen.getByTestId('avatar-pack-unsupported-nebula')).toHaveTextContent(
      'Not supported for crews yet',
    )
    fireEvent.click(card)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('dims the inert half of an unwearable card but not its Delete', async () => {
    // Delete is the ONE action an unwearable pack still offers, and an
    // unwearable pack is exactly the one a user wants gone. Dimming the whole
    // card took the live control with it and read as disabled.
    mockApi.appearances.list.mockResolvedValue({ packs: [BUILTIN, AURORA, NEBULA] })
    mount()
    const card = await screen.findByTestId('avatar-pack-card-nebula')
    expect(screen.getByTestId('avatar-pack-select-nebula').className).toContain('opacity-50')
    expect(card.className).not.toContain('opacity-50')
    const del = screen.getByTestId('avatar-pack-delete-nebula')
    expect(del.closest('.opacity-50')).toBeNull()
    expect(del).not.toBeDisabled()
  })

  it('shows the empty state when the library holds nothing', async () => {
    mockApi.appearances.list.mockResolvedValue({ packs: [] })
    mount()
    expect(await screen.findByTestId('avatar-library-empty')).toBeInTheDocument()
  })

  it('reports a failed read and re-reads on retry', async () => {
    mockApi.appearances.list.mockRejectedValueOnce(apiError(503, 'library unavailable'))
    mount()
    expect(await screen.findByTestId('avatar-library-error')).toHaveTextContent('library unavailable')

    fireEvent.click(screen.getByTestId('avatar-library-retry'))
    await waitFor(() => expect(screen.getByTestId('avatar-pack-card-aurora')).toBeInTheDocument())
  })
})

describe('library pane — scroll edges', () => {
  it('fades only the edge that has more content behind it', async () => {
    // A card cut at a hard border reads as a layout bug rather than as a list
    // that scrolls, so the fade is what tells those two apart — and a list that
    // FITS must carry no fade at all, or a full view looks dimmed.
    mount()
    const grid = await screen.findByTestId('avatar-library-grid')
    expect(grid).toHaveAttribute('data-fade', 'none')

    Object.defineProperty(grid, 'scrollHeight', { value: 600, configurable: true })
    Object.defineProperty(grid, 'clientHeight', { value: 380, configurable: true })
    grid.scrollTop = 0
    fireEvent.scroll(grid)
    expect(grid).toHaveAttribute('data-fade', 'bottom')

    grid.scrollTop = 100
    fireEvent.scroll(grid)
    expect(grid).toHaveAttribute('data-fade', 'topbottom')

    grid.scrollTop = 220
    fireEvent.scroll(grid)
    expect(grid).toHaveAttribute('data-fade', 'top')
  })
})

describe('library pane — import', () => {
  it('posts the bundle, re-reads the library and selects what was installed', async () => {
    mockApi.appearances.list
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA, NEBULA] })
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA, NEBULA, IMPORTED_SVG] })
    const { onSelect } = mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    pick(bundleFile(VALID_BUNDLE))

    await waitFor(() => expect(mockApi.appearances.importBundle).toHaveBeenCalledTimes(1))
    // The WHOLE document, envelope included. This assertion is the one that has to
    // match the importer rather than this pane's idea of a bundle: `import_bundle`
    // refuses a payload whose `kind` is wrong and reads the pack id from `id`, so an
    // expectation written without them passes while every real import fails.
    expect(mockApi.appearances.importBundle.mock.calls[0][0]).toEqual({
      kind: 'crew-companion-pack',
      version: 1,
      id: 'aurora',
      manifest: { meta: { id: 'aurora', format: 'svg' }, states: { idle: 'idle.svg' } },
      files: { 'idle.svg': '<svg/>' },
    })
    // Re-read: a grid that still showed the old set would hide the new pack.
    await waitFor(() => expect(mockApi.appearances.list).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('imported'))
  })

  it('installs a non-SVG pack but refuses to wear it, and says so', async () => {
    // The server accepts a Lottie bundle, so the import SUCCEEDS — but a crew's
    // face is an <img>. Auto-selecting it enabled Apply on a face that cannot
    // render, so the format is re-read from the fresh listing before selecting.
    mockApi.appearances.list
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA, NEBULA] })
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA, NEBULA, IMPORTED_LOTTIE] })
    const { onSelect } = mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    pick(bundleFile(VALID_BUNDLE))

    await waitFor(() => expect(mockApi.appearances.list).toHaveBeenCalledTimes(2))
    // Shown after a SUCCESSFUL import, which is the sharpest case for it not
    // being an error: the server did what it was asked.
    expect(await screen.findByTestId('avatar-pack-pick-hint')).toHaveTextContent(
      'That pack is installed, but a crew can only wear an SVG pack.',
    )
    expect(screen.queryByTestId('avatar-pack-error')).toBeNull()
    expect(onSelect).not.toHaveBeenCalled()
    // It still LISTS — the import worked, and hiding it would say otherwise.
    expect(screen.getByTestId('avatar-pack-card-imported')).toBeInTheDocument()
  })

  it('refuses a file that is not a bundle without asking the server', async () => {
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    pick(bundleFile('not json at all'))

    // A muted HINT, not an error notice: nothing was asked of the server and
    // nothing failed, so dressing it as an error would misreport what happened
    // (and `errors-use-error-notice` forbids exactly that inversion).
    expect(await screen.findByTestId('avatar-pack-pick-hint')).toHaveTextContent(
      'That file is not a pack bundle.',
    )
    expect(screen.queryByTestId('avatar-pack-error')).toBeNull()
    expect(mockApi.appearances.importBundle).not.toHaveBeenCalled()
  })

  it('refuses an over-large file by its size, before reading it', async () => {
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    pick(bundleFile(VALID_BUNDLE, 'huge.json', 25 * 1024 * 1024))

    expect(await screen.findByTestId('avatar-pack-pick-hint')).toHaveTextContent(
      'That pack file is too big. The limit is 24 MB.',
    )
    expect(screen.queryByTestId('avatar-pack-error')).toBeNull()
    expect(mockApi.appearances.importBundle).not.toHaveBeenCalled()
  })

  it('shows the server\u2019s own rejection, which names the failing check', async () => {
    mockApi.appearances.importBundle.mockRejectedValueOnce(
      apiError(400, 'manifest names idle.svg but the bundle does not carry it'),
    )
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    pick(bundleFile(VALID_BUNDLE))

    expect(await screen.findByTestId('avatar-pack-error')).toHaveTextContent(
      'manifest names idle.svg but the bundle does not carry it',
    )
  })
})

describe('library pane — delete', () => {
  it('offers no delete on the built-in pack', async () => {
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    expect(screen.queryByTestId(`avatar-pack-delete-${BUILTIN_PACK_ID}`)).toBeNull()
    expect(screen.getByTestId('avatar-pack-delete-aurora')).toBeInTheDocument()
  })

  it('takes two clicks, and re-reads the library after the delete', async () => {
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    expect(mockApi.appearances.remove).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))
    await waitFor(() => expect(mockApi.appearances.remove).toHaveBeenCalledWith('aurora'))
    await waitFor(() => expect(mockApi.appearances.list).toHaveBeenCalledTimes(2))
  })

  it('clears the draft when the pack it named is deleted', async () => {
    // Left set, Apply would persist a pack the library no longer holds — a face
    // that silently resolves to the seeded ghost.
    mockApi.appearances.list
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA, NEBULA] })
      .mockResolvedValueOnce({ packs: [BUILTIN, NEBULA] })
    const { onSelect } = mount('aurora')
    await screen.findByTestId('avatar-pack-card-aurora')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith(null))
  })

  it('clears the draft BEFORE the delete request, not after it', async () => {
    // Apply is gated on the draft's pack id, which the parent owns, and this
    // tab's `busy` never reaches it. So clearing afterwards left a window as long
    // as the round-trip in which Apply would persist a reference to a pack that
    // was being deleted. The clear has to happen before the request goes out.
    let settle: (v: unknown) => void = () => {}
    mockApi.appearances.remove.mockImplementationOnce(
      () => new Promise(resolve => { settle = resolve }),
    )
    const { onSelect } = mount('aurora')
    await screen.findByTestId('avatar-pack-card-aurora')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))

    // Still in flight, and the draft is already clear.
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith(null))
    expect(mockApi.appearances.remove).toHaveBeenCalledWith('aurora')

    await act(async () => { settle({ ok: true }) })
  })

  it('locks every selector while a delete is in flight', async () => {
    // The fence above only holds if the selectable set cannot change under it.
    let settle: (v: unknown) => void = () => {}
    mockApi.appearances.remove.mockImplementationOnce(
      () => new Promise(resolve => { settle = resolve }),
    )
    const { onSelect } = mount('aurora')
    await screen.findByTestId('avatar-pack-card-aurora')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith(null))

    expect(screen.getByTestId(`avatar-pack-select-${BUILTIN_PACK_ID}`)).toBeDisabled()
    expect(screen.getByTestId('avatar-pack-delete-nebula')).toBeDisabled()
    onSelect.mockClear()
    fireEvent.click(screen.getByTestId(`avatar-pack-select-${BUILTIN_PACK_ID}`))
    expect(onSelect).not.toHaveBeenCalled()

    await act(async () => { settle({ ok: true }) })
  })

  it('puts the draft back when the delete is refused', async () => {
    // A 409 while a crew wears the pack is the ordinary refusal: the pack is
    // still there, so the crew that was wearing it must still be wearing it.
    mockApi.appearances.remove.mockRejectedValueOnce(
      apiError(409, 'that pack is still worn by a crew', JSON.stringify({ code: 'pack_in_use', crews: ['oncall'] })),
    )
    const { onSelect } = mount('aurora')
    await screen.findByTestId('avatar-pack-card-aurora')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))

    expect(await screen.findByTestId('avatar-pack-error')).toHaveTextContent(
      'Aurora was not deleted — it is still worn by: oncall',
    )
    expect(onSelect.mock.calls.map(c => c[0])).toEqual([null, 'aurora'])
  })

  it('leaves the draft alone when a DIFFERENT pack is deleted', async () => {
    mockApi.appearances.list
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA, NEBULA] })
      .mockResolvedValueOnce({ packs: [BUILTIN, AURORA] })
    const { onSelect } = mount('aurora')
    await screen.findByTestId('avatar-pack-card-nebula')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-nebula'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-nebula'))

    await waitFor(() => expect(mockApi.appearances.remove).toHaveBeenCalledWith('nebula'))
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('moves focus to Keep when a delete arms', async () => {
    // Arming unmounts the Delete link the user just clicked, so focus would fall to
    // the body mid-destructive-flow. It lands on the SAFE half: on the confirm, a
    // stray Enter would finish a delete the user has not read.
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    await waitFor(() =>
      expect(screen.getByTestId('avatar-pack-delete-cancel-aurora')).toHaveFocus(),
    )
  })

  it('marks the chosen card in words, so the grid reads as a chooser at rest', async () => {
    // A hover ring only exists under a pointer. At first sight nothing told the
    // reader this grid was a picker rather than a display of what is installed —
    // one card visibly marked is what carries that, so the mark is the affordance
    // as much as it is the state.
    mount('aurora')
    await screen.findByTestId('avatar-pack-card-aurora')
    expect(screen.getByTestId('avatar-pack-selected-aurora')).toHaveTextContent('Selected')
    expect(screen.queryByTestId(`avatar-pack-selected-${BUILTIN_PACK_ID}`)).toBeNull()
  })

  it('states what deleting costs, next to the button that does it', async () => {
    // The reader refused to click because nothing said whether a delete could be
    // undone. It cannot — but the bundle is re-importable, which is the half that
    // makes the decision easy instead of frightening.
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    expect(screen.getByTestId('avatar-pack-delete-note-aurora')).toHaveTextContent(
      'Removed for every crew. Import the bundle again to get it back.',
    )
  })

  it('shows an author only on a custom pack, never on the built-in', async () => {
    // "Author: Kiro Crew" on the built-in left the reader unable to tell whether
    // that named a person, a team or the app, and the "Built-in" badge already says
    // the useful part.
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    expect(within(screen.getByTestId('avatar-pack-card-aurora')).getByText('Author: zoe')).toBeInTheDocument()
    expect(
      within(screen.getByTestId(`avatar-pack-card-${BUILTIN_PACK_ID}`)).queryByText(/^Author:/),
    ).toBeNull()
  })

  it('gives a selectable card a pointer and a hover ring, and an unwearable one neither', async () => {
    // The first-run reader rated clicking a card a guess, and selecting a pack is
    // this pane's main path.
    mockApi.appearances.list.mockResolvedValue({ packs: [BUILTIN, AURORA, NEBULA] })
    mount()
    const selectable = await screen.findByTestId('avatar-pack-select-aurora')
    expect(selectable.className).toContain('cursor-pointer')
    expect(selectable.className).toContain('hover:ring-2')
    const inert = screen.getByTestId('avatar-pack-select-nebula')
    expect(inert.className).not.toContain('cursor-pointer')
    expect(inert.className).not.toContain('hover:ring-2')
  })

  it('disarms on the second thought', async () => {
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-cancel-aurora'))
    expect(screen.getByTestId('avatar-pack-delete-aurora')).toBeInTheDocument()
    expect(mockApi.appearances.remove).not.toHaveBeenCalled()
  })

  it('names the crews wearing the pack when the server refuses, and stops', async () => {
    // "In use" with no list leaves the user hunting a roster for a face they
    // cannot see. `?force=1` exists server-side and is deliberately not offered.
    mockApi.appearances.remove.mockRejectedValueOnce(
      apiError(409, 'that pack is still worn by a crew', JSON.stringify({ code: 'pack_in_use', crews: ['oncall', 'radar'] })),
    )
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')

    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))

    // The outcome leads AND the subject is named: "In use by: …" never said the
    // delete had failed, and "That pack" left the reader matching one message to a
    // grid of cards — the notice renders above the whole grid, after the armed
    // confirm that identified the pack has already collapsed.
    expect(await screen.findByTestId('avatar-pack-error')).toHaveTextContent(
      'Aurora was not deleted — it is still worn by: oncall, radar',
    )
    // The card stays: nothing was removed.
    expect(screen.getByTestId('avatar-pack-card-aurora')).toBeInTheDocument()
  })

  it('falls back to the server message when a refusal is not a wearer conflict', async () => {
    mockApi.appearances.remove.mockRejectedValueOnce(
      apiError(400, 'the built-in pack cannot be deleted'),
    )
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))
    expect(await screen.findByTestId('avatar-pack-error')).toHaveTextContent(
      'the built-in pack cannot be deleted',
    )
  })

  it('survives a 409 whose body is not readable JSON', async () => {
    mockApi.appearances.remove.mockRejectedValueOnce(apiError(409, 'still worn', '<html>nope'))
    mount()
    await screen.findByTestId('avatar-pack-card-aurora')
    fireEvent.click(screen.getByTestId('avatar-pack-delete-aurora'))
    fireEvent.click(screen.getByTestId('avatar-pack-delete-confirm-aurora'))
    // Not the named-list sentence with nothing after its colon: the outcome has
    // to read whole even when the wearer list does not survive the transport.
    expect(await screen.findByTestId('avatar-pack-error')).toHaveTextContent(
      'Aurora was not deleted — a crew is still wearing it.',
    )
  })
})
