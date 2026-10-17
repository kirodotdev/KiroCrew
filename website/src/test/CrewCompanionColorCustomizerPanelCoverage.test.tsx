/**
 * `apps/crew-companion/ColorCustomizerPanel.tsx` — the companion's whole
 * recolouring surface, driven end to end through its one seam.
 *
 * The panel is a preset grid plus a manual per-body-part editor. Everything it can
 * reach outside itself goes through `petBridge`, so that module is the only double
 * here: the colour maths (`colorCustomizer`, `catPresets`, `builtInCatPresets`) and
 * the data-URI encoder run for real, because the previews ARE their output and a
 * stub would prove nothing about the image a user actually sees.
 *
 * The behaviours pinned below are the ones a user can be lied to by:
 *
 *  - what is read on mount reaches the previews (a saved map recolours the combined
 *    thumbnail and pre-fills every colour input) and an EMPTY read leaves the art
 *    alone rather than blanking it;
 *  - the per-part highlight preview really fades the OTHER colours, with the dark
 *    outline colours faded to their own shade;
 *  - a preset click, an Enter/Space activation and a manual edit each persist
 *    through `gallerySetColorMap` against the pack id the backend actually reloads
 *    by (`kiro-ghost`), and an edit drops the active mark because the result is no
 *    longer that preset;
 *  - the save form's guard, and the two ways out of it, which differ on whether the
 *    typed name survives;
 *  - delete, which must persist the SHORTER list and must not double as a click on
 *    the card the button sits inside.
 *
 * Fake timers are armed defensively (this panel schedules nothing of its own, but a
 * colour input is exactly where a debounce lands later, and a stray callback firing
 * after teardown surfaces as an unhandled error rather than a failed assertion).
 *
 * The user-visible strings are read through the real `i18nT`, never hardcoded: this
 * panel's keys are absent from the English catalogue, so i18next echoes the key back
 * and hardcoded copy would silently rot the day the catalogue gains them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { i18nT } from '../i18n/t'
import type { CatPreset } from '../apps/crew-companion/catPresets'
import type { ColorMap } from '../apps/crew-companion/colorCustomizer'

/* ── bridge double ────────────────────────────────────────────── */

const presetsGetColorMap = vi.fn<(packId: string) => Promise<ColorMap | undefined>>()
const presetsLoadCustom = vi.fn<() => Promise<CatPreset[]>>()
const presetsSaveCustom = vi.fn<(presets: CatPreset[]) => Promise<void>>()
const gallerySetColorMap = vi.fn<(packId: string, map: ColorMap) => Promise<void>>()

const bridge = { presetsGetColorMap, presetsLoadCustom, presetsSaveCustom, gallerySetColorMap }

vi.mock('../apps/crew-companion/petBridge', () => ({ petBridge: bridge, galleryApi: bridge }))

const { ColorCustomizerPanel } = await import('../apps/crew-companion/ColorCustomizerPanel')
const { BUILT_IN_CAT_PRESETS } = await import('../apps/crew-companion/builtInCatPresets')
const { applySvgColorMap } = await import('../apps/crew-companion/colorCustomizer')
const { toDataUri } = await import('../apps/crew-companion/animationResolver')

/* ── fixtures ─────────────────────────────────────────────────── */

/** The backend's canonical built-in pack id (appearances.py `DEFAULT_PACK`). */
const PACK_ID = 'kiro-ghost'

/**
 * Idle art with four distinct source colours: two the panel has a body-part label
 * for, one "dark" colour the highlight map fades differently, and one it has no
 * label for at all — so the hex fallback is exercised too.
 */
const IDLE_SVG = [
  '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">',
  '<path fill="#F9A85F" d="M0 0h10v10H0z" />',
  '<path fill="#FCD9B3" d="M2 2h4v4H2z" />',
  '<path stroke="#522210" d="M0 0L10 10" />',
  '<path fill="#123456" d="M8 8h2v2H8z" />',
  '</svg>',
].join('')

/** The source colours the fixture art declares, in extraction order. */
const SOURCES = ['#F9A85F', '#FCD9B3', '#522210', '#123456']

/** Labels, read the way the component reads them. */
const BODY_FUR = i18nT('apps.crewCompanion.color.bodyFur')
const TUMMY = i18nT('apps.crewCompanion.color.tummyPaws')
const OUTLINES = i18nT('apps.crewCompanion.color.outlines')
const RESET = i18nT('apps.crewCompanion.color.reset')
const SAVE_PRESET = i18nT('apps.crewCompanion.color.savePreset')
const CANCEL = i18nT('apps.crewCompanion.gallery.cancel')
const DELETE = i18nT('apps.crewCompanion.color.delete')
const NAME_PLACEHOLDER = i18nT('apps.crewCompanion.color.promptName')

/** A built-in card's display name is its i18n key resolved. */
function builtInLabel(id: string): string {
  const found = BUILT_IN_CAT_PRESETS.find(p => p.id === id)
  if (!found) throw new Error(`no built-in preset ${id}`)
  return i18nT(found.name)
}

function builtInMap(id: string): ColorMap {
  const found = BUILT_IN_CAT_PRESETS.find(p => p.id === id)
  if (!found) throw new Error(`no built-in preset ${id}`)
  return found.colorMap
}

/** A preset in the shape `presetsLoadCustom` hands one back from disk. */
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

/* ── harness ──────────────────────────────────────────────────── */

/** Flush the one mount read (a `Promise.all` of two bridge calls) inside `act`. */
async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve()
  })
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

/** The map handed to the most recent `gallerySetColorMap` call. */
function lastPersistedMap(): ColorMap {
  const calls = gallerySetColorMap.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[calls.length - 1][1]
}

/** Wait for the save form to have closed itself after a successful write. */
async function formClosed(): Promise<void> {
  await waitFor(() => expect(screen.queryByPlaceholderText(NAME_PLACEHOLDER)).toBeNull(), {
    timeout: 5_000,
  })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.clearAllMocks()
  presetsGetColorMap.mockResolvedValue({})
  presetsLoadCustom.mockResolvedValue([])
  presetsSaveCustom.mockResolvedValue(undefined)
  gallerySetColorMap.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
})

/* ── mount ────────────────────────────────────────────────────── */

describe('ColorCustomizerPanel — what is on screen after mount', () => {
  it('renders both sections, every built-in preset and one editor per source colour', async () => {
    const container = await renderPanel()

    expect(screen.getByText(i18nT('apps.crewCompanion.color.presets'))).toBeTruthy()
    expect(screen.getByText(i18nT('apps.crewCompanion.color.manual'))).toBeTruthy()
    for (const preset of BUILT_IN_CAT_PRESETS) {
      expect(screen.getByAltText(i18nT(preset.name))).toBeTruthy()
    }

    // The combined cell is the whole-art preview, not a per-part editor.
    expect(screen.getByAltText('final')).toBeTruthy()
    expect(screen.getByText(i18nT('apps.crewCompanion.color.currentEffect'))).toBeTruthy()

    // Three labelled parts plus the colour with no label, which shows its hex.
    expect(screen.getByAltText(BODY_FUR)).toBeTruthy()
    expect(screen.getByAltText(TUMMY)).toBeTruthy()
    expect(screen.getByAltText(OUTLINES)).toBeTruthy()
    expect(screen.getByAltText('#123456')).toBeTruthy()
    expect(container.querySelectorAll('input[type="color"]')).toHaveLength(SOURCES.length)

    expect(presetsGetColorMap).toHaveBeenCalledWith(PACK_ID)
    expect(presetsLoadCustom).toHaveBeenCalled()
  })

  it('recolours the combined preview and pre-fills the inputs from the saved map', async () => {
    presetsGetColorMap.mockResolvedValue({ '#F9A85F': '#00FF00' })
    await renderPanel()

    expect(screen.getByAltText('final').getAttribute('src'))
      .toBe(toDataUri(applySvgColorMap(IDLE_SVG, { '#F9A85F': '#00FF00' })))
    expect(colorInput(BODY_FUR).value.toLowerCase()).toBe('#00ff00')
    // An untouched part still shows its own source colour.
    expect(colorInput(TUMMY).value.toLowerCase()).toBe('#fcd9b3')
  })

  it('leaves the art alone when the saved map and the custom list are empty', async () => {
    await renderPanel()

    expect(screen.getByAltText('final').getAttribute('src')).toBe(toDataUri(IDLE_SVG))
    expect(colorInput(BODY_FUR).value.toLowerCase()).toBe('#f9a85f')
    expect(screen.queryAllByRole('button', { name: DELETE })).toHaveLength(0)
    expect(gallerySetColorMap).not.toHaveBeenCalled()
  })

  it('fades the other colours in a part preview, dark outlines to their own shade', async () => {
    await renderPanel()

    // Only the highlighted source keeps a real colour; the dark outline colour
    // fades to #ECECEC and everything else to #FAFAFA.
    const expected: ColorMap = {
      '#F9A85F': '#F9A85F',
      '#FCD9B3': '#FAFAFA',
      '#522210': '#ECECEC',
      '#123456': '#FAFAFA',
    }
    expect(screen.getByAltText(BODY_FUR).getAttribute('src'))
      .toBe(toDataUri(applySvgColorMap(IDLE_SVG, expected)))
  })

  it('keeps a stored custom preset, drops malformed entries, and only customs get delete', async () => {
    presetsLoadCustom.mockResolvedValue(
      [null, 'not-a-preset', customPreset()] as unknown as CatPreset[],
    )
    await renderPanel()

    expect(within(card('Sunset Kitty')).getByRole('button', { name: DELETE })).toBeTruthy()
    expect(within(card(builtInLabel('tuxedo'))).queryByRole('button', { name: DELETE })).toBeNull()
    // The two malformed entries added no cards of their own.
    expect(screen.queryAllByRole('button', { name: DELETE })).toHaveLength(1)
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

    fireEvent.click(card(builtInLabel('tuxedo')))

    expect(gallerySetColorMap).toHaveBeenCalledWith(PACK_ID, builtInMap('tuxedo'))
    expect(card(builtInLabel('tuxedo')).getAttribute('aria-pressed')).toBe('true')
    expect(card(builtInLabel('calico')).getAttribute('aria-pressed')).toBe('false')
    // The manual editors follow the preset rather than keeping the source colour.
    expect(colorInput(BODY_FUR).value.toLowerCase())
      .toBe(String(builtInMap('tuxedo')['#F9A85F']).toLowerCase())
  })

  it('activates a card with Enter and with Space, and ignores other keys', async () => {
    await renderPanel()

    fireEvent.keyDown(card(builtInLabel('siamese')), { key: 'Enter' })
    expect(lastPersistedMap()).toEqual(builtInMap('siamese'))
    expect(card(builtInLabel('siamese')).getAttribute('aria-pressed')).toBe('true')

    fireEvent.keyDown(card(builtInLabel('calico')), { key: ' ' })
    expect(lastPersistedMap()).toEqual(builtInMap('calico'))
    expect(card(builtInLabel('calico')).getAttribute('aria-pressed')).toBe('true')
    expect(card(builtInLabel('siamese')).getAttribute('aria-pressed')).toBe('false')

    const before = gallerySetColorMap.mock.calls.length
    fireEvent.keyDown(card(builtInLabel('ragdoll')), { key: 'a' })
    expect(gallerySetColorMap.mock.calls).toHaveLength(before)
  })

  it('merges a manual edit into the map and drops the active preset mark', async () => {
    await renderPanel()
    fireEvent.click(card(builtInLabel('tuxedo')))

    fireEvent.change(colorInput(TUMMY), { target: { value: '#010203' } })

    const persisted = lastPersistedMap()
    expect(persisted['#FCD9B3']).toBe('#010203')
    // Everything the preset set is still there — the edit is a merge, not a reset.
    expect(persisted['#F9A85F']).toBe(builtInMap('tuxedo')['#F9A85F'])
    expect(card(builtInLabel('tuxedo')).getAttribute('aria-pressed')).toBe('false')
    expect(colorInput(TUMMY).value.toLowerCase()).toBe('#010203')
  })

  it('Reset clears the map, the active mark and the previews', async () => {
    presetsGetColorMap.mockResolvedValue({ '#F9A85F': '#00FF00' })
    await renderPanel()
    fireEvent.click(card(builtInLabel('tuxedo')))

    fireEvent.click(screen.getByRole('button', { name: RESET }))
    await flush()

    expect(lastPersistedMap()).toEqual({})
    expect(card(builtInLabel('tuxedo')).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByAltText('final').getAttribute('src')).toBe(toDataUri(IDLE_SVG))
    expect(colorInput(BODY_FUR).value.toLowerCase()).toBe('#f9a85f')
  })
})

/* ── saving a custom preset ───────────────────────────────────── */

describe('ColorCustomizerPanel — saving the current colours as a preset', () => {
  it('opens the form on demand and writes nothing for a whitespace-only name', async () => {
    await renderPanel()
    expect(screen.queryByPlaceholderText(NAME_PLACEHOLDER)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    const nameField = screen.getByPlaceholderText(NAME_PLACEHOLDER)
    const confirm = screen.getByRole('button', { name: SAVE_PRESET }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)

    fireEvent.change(nameField, { target: { value: '   ' } })
    expect(confirm.disabled).toBe(true)

    fireEvent.keyDown(nameField, { key: 'Enter' })
    await flush()
    expect(presetsSaveCustom).not.toHaveBeenCalled()
    // The form stays open so the name can be corrected.
    expect(screen.getByPlaceholderText(NAME_PLACEHOLDER)).toBeTruthy()
  })

  it('saves the edited colours under the typed name and shows the new card', async () => {
    await renderPanel()
    fireEvent.change(colorInput(BODY_FUR), { target: { value: '#00ff00' } })

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    fireEvent.change(screen.getByPlaceholderText(NAME_PLACEHOLDER), {
      target: { value: 'Sunset Kitty' },
    })
    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    await formClosed()

    const saved = presetsSaveCustom.mock.calls[0][0]
    expect(saved).toHaveLength(1)
    expect(saved[0].name).toBe('Sunset Kitty')
    expect(saved[0].builtIn).toBe(false)
    expect(saved[0].colorMap).toEqual({ '#F9A85F': '#00ff00' })
    // One edited colour yields one swatch, and the pad brings it up to two — by
    // repeating the same colour, which is what the pad actually has to hand.
    expect(saved[0].swatches).toEqual(['#00ff00', '#00ff00'])

    // The preset joins the grid once the form has closed.
    expect(screen.getByAltText('Sunset Kitty')).toBeTruthy()
  })

  it('keeps the swatches as they are when two colours already differ', async () => {
    await renderPanel()
    fireEvent.change(colorInput(BODY_FUR), { target: { value: '#00ff00' } })
    fireEvent.change(colorInput(TUMMY), { target: { value: '#0000ff' } })

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    fireEvent.change(screen.getByPlaceholderText(NAME_PLACEHOLDER), {
      target: { value: 'Two Tone' },
    })
    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    await formClosed()

    // Two distinct values already satisfy the 2-swatch floor, so nothing is padded.
    expect(presetsSaveCustom.mock.calls[0][0][0].swatches).toEqual(['#00ff00', '#0000ff'])
  })

  it('saves an untouched palette as a preset with no swatches at all', async () => {
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    fireEvent.change(screen.getByPlaceholderText(NAME_PLACEHOLDER), {
      target: { value: 'Nothing Changed' },
    })
    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    await formClosed()

    // Nothing was edited, so there is no colour to pad from and the stored preset
    // carries an empty swatch list.
    expect(presetsSaveCustom.mock.calls[0][0][0].swatches).toEqual([])
    expect(presetsSaveCustom.mock.calls[0][0][0].colorMap).toEqual({})
  })

  it('saves on Enter, and Escape closes the form while keeping the typed name', async () => {
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    fireEvent.change(screen.getByPlaceholderText(NAME_PLACEHOLDER), {
      target: { value: 'Via Enter' },
    })
    fireEvent.keyDown(screen.getByPlaceholderText(NAME_PLACEHOLDER), { key: 'Enter' })
    await formClosed()
    expect(presetsSaveCustom.mock.calls[0][0][0].name).toBe('Via Enter')

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    fireEvent.change(screen.getByPlaceholderText(NAME_PLACEHOLDER), {
      target: { value: 'Abandoned' },
    })
    fireEvent.keyDown(screen.getByPlaceholderText(NAME_PLACEHOLDER), { key: 'Escape' })
    expect(screen.queryByPlaceholderText(NAME_PLACEHOLDER)).toBeNull()
    expect(presetsSaveCustom).toHaveBeenCalledTimes(1)

    // Escape only hides the form: reopening still holds what was typed.
    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    expect((screen.getByPlaceholderText(NAME_PLACEHOLDER) as HTMLInputElement).value)
      .toBe('Abandoned')
  })

  it('Cancel closes the form and discards the typed name', async () => {
    await renderPanel()

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    fireEvent.change(screen.getByPlaceholderText(NAME_PLACEHOLDER), {
      target: { value: 'Discarded' },
    })
    fireEvent.click(screen.getByRole('button', { name: CANCEL }))
    expect(screen.queryByPlaceholderText(NAME_PLACEHOLDER)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: SAVE_PRESET }))
    expect((screen.getByPlaceholderText(NAME_PLACEHOLDER) as HTMLInputElement).value).toBe('')
    expect(presetsSaveCustom).not.toHaveBeenCalled()
  })
})

/* ── deleting a custom preset ─────────────────────────────────── */

describe('ColorCustomizerPanel — deleting a custom preset', () => {
  it('persists the shorter list and does not also apply the preset it sat on', async () => {
    presetsLoadCustom.mockResolvedValue([customPreset()])
    await renderPanel()

    fireEvent.click(within(card('Sunset Kitty')).getByRole('button', { name: DELETE }))
    await waitFor(() => expect(presetsSaveCustom).toHaveBeenCalledWith([]), { timeout: 5_000 })

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

    fireEvent.click(within(card('Ocean Kitty')).getByRole('button', { name: DELETE }))
    await waitFor(() => expect(presetsSaveCustom).toHaveBeenCalled(), { timeout: 5_000 })

    expect(presetsSaveCustom.mock.calls[0][0].map(p => p.id)).toEqual(['custom-1'])
    expect(screen.queryByAltText('Ocean Kitty')).toBeNull()
    expect(card('Sunset Kitty').getAttribute('aria-pressed')).toBe('true')
  })

  it('clears the active mark when the ACTIVE preset is deleted', async () => {
    presetsLoadCustom.mockResolvedValue([customPreset()])
    await renderPanel()

    fireEvent.click(card('Sunset Kitty'))
    expect(card('Sunset Kitty').getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(within(card('Sunset Kitty')).getByRole('button', { name: DELETE }))
    await waitFor(() => expect(screen.queryByAltText('Sunset Kitty')).toBeNull(), {
      timeout: 5_000,
    })

    for (const preset of BUILT_IN_CAT_PRESETS) {
      expect(card(i18nT(preset.name)).getAttribute('aria-pressed')).toBe('false')
    }
    // The colours the deleted preset applied are still in effect — delete removes
    // the entry, not the look.
    expect(colorInput(BODY_FUR).value.toLowerCase()).toBe('#ff0000')
  })
})
