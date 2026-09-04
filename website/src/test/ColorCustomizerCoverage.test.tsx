/**
 * Mochi color customizer — first tests for
 * `apps/mochi/src/renderer/ColorCustomizer.tsx`.
 *
 * The panel is the pet's entire recolouring surface (a preset grid plus a manual
 * per-body-part editor) and had no test at all, so every behaviour below is
 * pinned here for the first time. Everything it can reach outside itself goes
 * through one seam (`api` in `mochiApi`), so that module is the single mock; the
 * colour maths (`colorCustomizer`, `catPresets`, `builtInCatPresets`) is pure and
 * is exercised for real rather than stubbed, because the previews ARE its output
 * and a stub would prove nothing about what the user sees.
 *
 * The behaviours worth pinning are the ones a user can be lied to by:
 *
 *  - what is read on mount actually reaches the previews (a saved map recolours
 *    the "Current" thumbnail and pre-fills each colour input), and an EMPTY read
 *    leaves the art alone rather than blanking it;
 *  - a preset click, an Enter/Space activation and a manual colour edit each
 *    persist through `gallerySetColorMap`, and an edit drops the active mark
 *    because the result is no longer that preset;
 *  - the save form's guard (a whitespace-only name writes nothing) and the two
 *    ways out of it, which differ on whether the typed name survives;
 *  - delete, which must persist the SHORTER list and must not double as a click
 *    on the card it sits inside.
 *
 * No fake timers: the panel has no timers of its own. The one async read is
 * flushed explicitly after mount so nothing here depends on elapsed time.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { CatPreset } from '../apps/mochi/src/shared/catPresets'
import type { ColorMap } from '../apps/mochi/src/shared/colorCustomizer'

const presetsGetColorMap = vi.fn<(avatarId: string) => Promise<ColorMap>>()
const presetsLoadCustom = vi.fn<() => Promise<CatPreset[]>>()
const presetsSaveCustom = vi.fn<(presets: CatPreset[]) => Promise<void>>()
const gallerySetColorMap = vi.fn<(avatarId: string, map: ColorMap) => Promise<void>>()

vi.mock('../apps/mochi/src/mochiApi', () => ({
  api: { presetsGetColorMap, presetsLoadCustom, presetsSaveCustom, gallerySetColorMap },
}))

const { ColorCustomizerPanel } = await import('../apps/mochi/src/renderer/ColorCustomizer')
const { BUILT_IN_CAT_PRESETS } = await import('../apps/mochi/src/shared/builtInCatPresets')
const { applySvgColorMap } = await import('../apps/mochi/src/shared/colorCustomizer')
const { toDataUri } = await import('../apps/mochi/src/renderer/animationResolver')

/* ── fixtures ─────────────────────────────────────────────────── */

/**
 * Idle art with four distinct source colours: three that the panel has a body
 * part label for (one of them a "dark" colour, which the highlight map fades
 * differently) and one it does not, so the unlabelled fallback is exercised too.
 */
const IDLE_SVG = [
  '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">',
  '<path fill="#F9A85F" d="M0 0h10v10H0z" />',
  '<path fill="#FCD9B3" d="M2 2h4v4H2z" />',
  '<path stroke="#522210" d="M0 0L10 10" />',
  '<path fill="#123456" d="M8 8h2v2H8z" />',
  '</svg>',
].join('')

/** The ten built-in cards, by the English display name each one renders. */
const BUILT_IN_NAMES = [
  'Orange Tabby', 'Tuxedo', 'Calico', 'Russian Blue', 'Siamese',
  'British Shorthair', 'White', 'Black', 'Tabby', 'Ragdoll',
]

/** A preset as `presetsLoadCustom` hands one back from disk. */
function customPreset(over: Partial<CatPreset> = {}): CatPreset {
  return {
    id: 'custom-1',
    name: 'Sunset Kitty',
    description: '',
    colorMap: { '#F9A85F': '#FF0000' },
    swatches: ['#FF0000', '#F9A85F'],
    builtIn: false,
    ...over,
  }
}

function builtInMap(id: string): ColorMap {
  const found = BUILT_IN_CAT_PRESETS.find(p => p.id === id)
  if (!found) throw new Error(`no built-in preset ${id}`)
  return found.colorMap
}

/* ── harness ──────────────────────────────────────────────────── */

/** Flush the one mount read (`Promise.all` of two api calls) inside `act`. */
async function flush(): Promise<void> {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function renderPanel(): Promise<HTMLElement> {
  const { container } = render(<ColorCustomizerPanel idleSvgContent={IDLE_SVG} />)
  await flush()
  return container
}

/** The card wrapper for a preset, addressed through its preview image. */
function card(name: string): HTMLElement {
  const wrapper = screen.getByAltText(name).closest('[role="button"]')
  if (!wrapper) throw new Error(`no preset card for ${name}`)
  return wrapper as HTMLElement
}

/** The manual colour input belonging to one body-part card, by its label. */
function colorInput(bodyPart: string): HTMLInputElement {
  const cell = screen.getByAltText(bodyPart).parentElement
  const input = cell?.querySelector<HTMLInputElement>('input[type="color"]')
  if (!input) throw new Error(`no colour input for ${bodyPart}`)
  return input
}

/** Wait for the save form to have closed itself after a successful write. */
async function formClosed(): Promise<void> {
  await waitFor(() => expect(screen.queryByPlaceholderText('Preset name')).toBeNull())
}

/** The map handed to the most recent `gallerySetColorMap` call. */
function lastPersistedMap(): ColorMap {
  const calls = gallerySetColorMap.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[calls.length - 1][1]
}

beforeEach(() => {
  vi.clearAllMocks()
  presetsGetColorMap.mockResolvedValue({})
  presetsLoadCustom.mockResolvedValue([])
  presetsSaveCustom.mockResolvedValue(undefined)
  gallerySetColorMap.mockResolvedValue(undefined)
})

/* ── mount ────────────────────────────────────────────────────── */

describe('ColorCustomizerPanel — what is on screen after mount', () => {
  it('renders both sections, all ten built-in presets and one editor per source colour', async () => {
    const container = await renderPanel()

    expect(screen.getByText('Presets')).toBeTruthy()
    expect(screen.getByText('Manual')).toBeTruthy()
    for (const name of BUILT_IN_NAMES) expect(screen.getByAltText(name)).toBeTruthy()

    // The "Current" cell is the combined preview, not a per-part editor.
    expect(screen.getByAltText('final')).toBeTruthy()
    expect(screen.getByText('Current')).toBeTruthy()

    // Three labelled parts plus the colour with no label, which shows its hex.
    expect(screen.getByAltText('Body / Fur')).toBeTruthy()
    expect(screen.getByAltText('Tummy / Pads')).toBeTruthy()
    expect(screen.getByAltText('Outlines / Eyes')).toBeTruthy()
    expect(screen.getByText('#123456')).toBeTruthy()
    expect(container.querySelectorAll('input[type="color"]')).toHaveLength(4)

    expect(presetsGetColorMap).toHaveBeenCalledWith('default-mochi')
    expect(presetsLoadCustom).toHaveBeenCalled()
  })

  it('recolours the preview and pre-fills the inputs from the saved map', async () => {
    presetsGetColorMap.mockResolvedValue({ '#F9A85F': '#00FF00' })
    await renderPanel()

    // The combined preview is built from the recoloured art, not the raw art.
    expect(screen.getByAltText('final').getAttribute('src'))
      .toBe(toDataUri(applySvgColorMap(IDLE_SVG, { '#F9A85F': '#00FF00' })))
    expect(colorInput('Body / Fur').value.toLowerCase()).toBe('#00ff00')
    // An untouched part still shows its own source colour.
    expect(colorInput('Tummy / Pads').value.toLowerCase()).toBe('#fcd9b3')
  })

  it('leaves the art alone when the saved map and the custom list are empty', async () => {
    await renderPanel()

    expect(screen.getByAltText('final').getAttribute('src')).toBe(toDataUri(IDLE_SVG))
    expect(colorInput('Body / Fur').value.toLowerCase()).toBe('#f9a85f')
    expect(screen.queryAllByRole('button', { name: 'Delete' })).toHaveLength(0)
    expect(gallerySetColorMap).not.toHaveBeenCalled()
  })

  it('shows a stored custom preset with a delete affordance the built-ins lack', async () => {
    presetsLoadCustom.mockResolvedValue([customPreset()])
    await renderPanel()

    expect(within(card('Sunset Kitty')).getByRole('button', { name: 'Delete' })).toBeTruthy()
    expect(within(card('Tuxedo')).queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('previews a colourless custom preset as the unmodified art', async () => {
    presetsLoadCustom.mockResolvedValue([customPreset({ colorMap: {} })])
    await renderPanel()

    expect(screen.getByAltText('Sunset Kitty').getAttribute('src')).toBe(toDataUri(IDLE_SVG))
  })
})

/* ── applying presets and manual edits ────────────────────────── */

describe('ColorCustomizerPanel — applying a preset', () => {
  it('marks the clicked preset active and persists its colour map', async () => {
    await renderPanel()

    fireEvent.click(card('Tuxedo'))

    expect(gallerySetColorMap).toHaveBeenCalledWith('default-mochi', builtInMap('tuxedo'))
    expect(card('Tuxedo').getAttribute('aria-pressed')).toBe('true')
    expect(card('Calico').getAttribute('aria-pressed')).toBe('false')
    // The manual editors follow the preset rather than keeping the source colour.
    expect(colorInput('Body / Fur').value.toLowerCase())
      .toBe(String(builtInMap('tuxedo')['#F9A85F']).toLowerCase())
  })

  it('activates a card from the keyboard with Enter and with Space, and ignores other keys', async () => {
    await renderPanel()

    fireEvent.keyDown(card('Siamese'), { key: 'Enter' })
    expect(lastPersistedMap()).toEqual(builtInMap('siamese'))
    expect(card('Siamese').getAttribute('aria-pressed')).toBe('true')

    fireEvent.keyDown(card('Calico'), { key: ' ' })
    expect(lastPersistedMap()).toEqual(builtInMap('calico'))
    expect(card('Calico').getAttribute('aria-pressed')).toBe('true')
    expect(card('Siamese').getAttribute('aria-pressed')).toBe('false')

    const before = gallerySetColorMap.mock.calls.length
    fireEvent.keyDown(card('Ragdoll'), { key: 'a' })
    expect(gallerySetColorMap.mock.calls).toHaveLength(before)
  })

  it('merges a manual edit into the map and drops the active preset mark', async () => {
    await renderPanel()
    fireEvent.click(card('Tuxedo'))

    fireEvent.change(colorInput('Tummy / Pads'), { target: { value: '#010203' } })

    const persisted = lastPersistedMap()
    expect(persisted['#FCD9B3']).toBe('#010203')
    // Everything the preset set is still there — the edit is a merge, not a reset.
    expect(persisted['#F9A85F']).toBe(builtInMap('tuxedo')['#F9A85F'])
    expect(card('Tuxedo').getAttribute('aria-pressed')).toBe('false')
    expect(colorInput('Tummy / Pads').value.toLowerCase()).toBe('#010203')
  })

  it('Reset clears the map, the active mark and the previews', async () => {
    presetsGetColorMap.mockResolvedValue({ '#F9A85F': '#00FF00' })
    await renderPanel()
    fireEvent.click(card('Tuxedo'))

    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await flush()

    expect(lastPersistedMap()).toEqual({})
    expect(card('Tuxedo').getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByAltText('final').getAttribute('src')).toBe(toDataUri(IDLE_SVG))
    expect(colorInput('Body / Fur').value.toLowerCase()).toBe('#f9a85f')
  })
})

/* ── saving a custom preset ───────────────────────────────────── */

describe('ColorCustomizerPanel — saving the current colours as a preset', () => {
  it('opens the form on demand and writes nothing for a whitespace-only name', async () => {
    await renderPanel()
    expect(screen.queryByPlaceholderText('Preset name')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    const nameField = screen.getByPlaceholderText('Preset name')
    const confirm = screen.getByRole('button', { name: 'Save as Preset' })
    expect((confirm as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(nameField, { target: { value: '   ' } })
    expect((confirm as HTMLButtonElement).disabled).toBe(true)

    fireEvent.keyDown(nameField, { key: 'Enter' })
    await flush()
    expect(presetsSaveCustom).not.toHaveBeenCalled()
    // The form stays open so the name can be corrected.
    expect(screen.getByPlaceholderText('Preset name')).toBeTruthy()
  })

  it('saves the edited colours under the typed name and shows the new card', async () => {
    await renderPanel()
    fireEvent.change(colorInput('Body / Fur'), { target: { value: '#00ff00' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    fireEvent.change(screen.getByPlaceholderText('Preset name'), { target: { value: 'Sunset Kitty' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    await formClosed()
    expect(presetsSaveCustom).toHaveBeenCalled()

    const saved = presetsSaveCustom.mock.calls[0][0]
    expect(saved).toHaveLength(1)
    expect(saved[0].name).toBe('Sunset Kitty')
    expect(saved[0].builtIn).toBe(false)
    expect(saved[0].colorMap).toEqual({ '#F9A85F': '#00ff00' })
    // One edited colour yields one swatch, and the pad brings it up to two.
    expect(saved[0].swatches).toHaveLength(2)

    // The preset joins the grid once the form has closed.
    expect(screen.getByAltText('Sunset Kitty')).toBeTruthy()
  })

  it('keeps the swatches as they are when two colours already differ', async () => {
    await renderPanel()
    fireEvent.change(colorInput('Body / Fur'), { target: { value: '#00ff00' } })
    fireEvent.change(colorInput('Tummy / Pads'), { target: { value: '#0000ff' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    fireEvent.change(screen.getByPlaceholderText('Preset name'), { target: { value: 'Two Tone' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    await formClosed()

    // Two distinct values already satisfy the 2-swatch floor, so nothing is padded.
    expect(presetsSaveCustom.mock.calls[0][0][0].swatches).toEqual(['#00ff00', '#0000ff'])
  })

  it('saves on Enter, and Escape closes the form while keeping the typed name', async () => {
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    fireEvent.change(screen.getByPlaceholderText('Preset name'), { target: { value: 'Via Enter' } })
    fireEvent.keyDown(screen.getByPlaceholderText('Preset name'), { key: 'Enter' })
    await formClosed()
    expect(presetsSaveCustom.mock.calls[0][0][0].name).toBe('Via Enter')

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    fireEvent.change(screen.getByPlaceholderText('Preset name'), { target: { value: 'Abandoned' } })
    fireEvent.keyDown(screen.getByPlaceholderText('Preset name'), { key: 'Escape' })
    expect(screen.queryByPlaceholderText('Preset name')).toBeNull()
    expect(presetsSaveCustom).toHaveBeenCalledTimes(1)

    // Escape only hides the form: reopening still holds what was typed.
    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    expect((screen.getByPlaceholderText('Preset name') as HTMLInputElement).value).toBe('Abandoned')
  })

  it('Cancel closes the form and discards the typed name', async () => {
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    fireEvent.change(screen.getByPlaceholderText('Preset name'), { target: { value: 'Discarded' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByPlaceholderText('Preset name')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Save as Preset' }))
    expect((screen.getByPlaceholderText('Preset name') as HTMLInputElement).value).toBe('')
    expect(presetsSaveCustom).not.toHaveBeenCalled()
  })
})

/* ── deleting a custom preset ─────────────────────────────────── */

describe('ColorCustomizerPanel — deleting a custom preset', () => {
  it('persists the shorter list and does not also apply the preset it sat on', async () => {
    presetsLoadCustom.mockResolvedValue([customPreset()])
    await renderPanel()

    fireEvent.click(within(card('Sunset Kitty')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(presetsSaveCustom).toHaveBeenCalledWith([]))

    expect(screen.queryByAltText('Sunset Kitty')).toBeNull()
    // The card's own click handler must not have fired through the delete button.
    expect(gallerySetColorMap).not.toHaveBeenCalled()
  })

  it('keeps the active mark when a DIFFERENT preset is deleted', async () => {
    presetsLoadCustom.mockResolvedValue([
      customPreset(),
      customPreset({ id: 'custom-2', name: 'Ocean Kitty', colorMap: { '#F9A85F': '#0000FF' } }),
    ])
    await renderPanel()

    fireEvent.click(card('Sunset Kitty'))
    expect(card('Sunset Kitty').getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(within(card('Ocean Kitty')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(presetsSaveCustom).toHaveBeenCalled())

    const remaining = presetsSaveCustom.mock.calls[0][0]
    expect(remaining.map(p => p.id)).toEqual(['custom-1'])
    expect(screen.queryByAltText('Ocean Kitty')).toBeNull()
    expect(card('Sunset Kitty').getAttribute('aria-pressed')).toBe('true')
  })

  it('clears the active mark when the ACTIVE preset is deleted', async () => {
    presetsLoadCustom.mockResolvedValue([customPreset()])
    await renderPanel()

    fireEvent.click(card('Sunset Kitty'))
    expect(card('Sunset Kitty').getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(within(card('Sunset Kitty')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByAltText('Sunset Kitty')).toBeNull())

    for (const name of BUILT_IN_NAMES) {
      expect(card(name).getAttribute('aria-pressed')).toBe('false')
    }
    // The colours the deleted preset applied are still in effect — delete removes
    // the entry, not the look.
    expect(colorInput('Body / Fur').value.toLowerCase()).toBe('#ff0000')
  })
})
