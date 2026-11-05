// Closing the color customizer's "Save preset" name field never strands focus
// on document.body. Enter (save), Escape, and the field's own confirm and
// cancel buttons all unmount the control that had focus, so each returns focus
// to the "Save preset" button that opened the field.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { i18nT } from '../i18n/t'
import type { CatPreset } from '../apps/crew-companion/catPresets'
import type { ColorMap } from '../apps/crew-companion/colorCustomizer'

const presetsGetColorMap = vi.fn<(packId: string) => Promise<ColorMap | undefined>>()
const presetsLoadCustom = vi.fn<() => Promise<CatPreset[]>>()
const presetsSaveCustom = vi.fn<(presets: CatPreset[]) => Promise<void>>()
const gallerySetColorMap = vi.fn<(packId: string, map: ColorMap) => Promise<void>>()
const bridge = { presetsGetColorMap, presetsLoadCustom, presetsSaveCustom, gallerySetColorMap }

vi.mock('../apps/crew-companion/petBridge', () => ({ petBridge: bridge, galleryApi: bridge }))

const { ColorCustomizerPanel } = await import('../apps/crew-companion/ColorCustomizerPanel')

const IDLE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><path fill="#F9A85F" d="M0 0h8v8H0z" /></svg>'
const SAVE_PRESET = i18nT('apps.crewCompanion.color.savePreset')
const CANCEL = i18nT('apps.crewCompanion.gallery.cancel')
const NAME = i18nT('apps.crewCompanion.color.promptName')

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })
const nameField = () => screen.queryByPlaceholderText(NAME)
// While the field is open the confirm button shares the trigger's name, so the
// trigger is the only match once the field has closed.
const trigger = () => screen.getByRole('button', { name: SAVE_PRESET })

beforeEach(() => {
  vi.clearAllMocks()
  presetsGetColorMap.mockResolvedValue({})
  presetsLoadCustom.mockResolvedValue([])
  presetsSaveCustom.mockResolvedValue(undefined)
  gallerySetColorMap.mockResolvedValue(undefined)
})

afterEach(cleanup)

async function openField(): Promise<HTMLElement> {
  render(
    <>
      <ColorCustomizerPanel idleSvgContent={IDLE_SVG} />
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </>,
  )
  await act(async () => { for (let i = 0; i < 5; i += 1) await Promise.resolve() })
  fireEvent.click(trigger())
  const input = screen.getByPlaceholderText(NAME)
  await waitFor(() => expect(input).toHaveFocus())
  return input
}

describe('Save preset field focus', () => {
  it('Enter saves and returns focus to the Save preset button', async () => {
    const input = await openField()
    fireEvent.change(input, { target: { value: 'Sunset Kitty' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(nameField()).toBeNull())
    await waitFor(() => expect(trigger()).toHaveFocus())
    expect(presetsSaveCustom).toHaveBeenCalledTimes(1)
  })

  it('Escape closes and returns focus to the Save preset button', async () => {
    const input = await openField()
    fireEvent.change(input, { target: { value: 'Abandoned' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(nameField()).toBeNull()
    await waitFor(() => expect(trigger()).toHaveFocus())
    expect(presetsSaveCustom).not.toHaveBeenCalled()
  })

  it('Enter on an empty name keeps the field open with focus in it', async () => {
    const input = await openField()
    fireEvent.keyDown(input, { key: 'Enter' })
    await nextFrame()
    expect(input).toHaveFocus()
    expect(presetsSaveCustom).not.toHaveBeenCalled()
  })

  it('the confirm button saves and returns focus to the Save preset button', async () => {
    const input = await openField()
    fireEvent.change(input, { target: { value: 'Sunset Kitty' } })
    // The confirm button is the one in the open field's row.
    const row = input.parentElement as HTMLElement
    const confirm = within(row).getByRole('button', { name: SAVE_PRESET })
    act(() => { confirm.focus() })
    fireEvent.click(confirm)
    await waitFor(() => expect(nameField()).toBeNull())
    await waitFor(() => expect(trigger()).toHaveFocus())
  })

  it('the cancel button closes and returns focus to the Save preset button', async () => {
    await openField()
    const cancel = screen.getByRole('button', { name: CANCEL })
    act(() => { cancel.focus() })
    fireEvent.click(cancel)
    expect(nameField()).toBeNull()
    await waitFor(() => expect(trigger()).toHaveFocus())
  })

  it('a save that resolves after the user moved on leaves focus where they put it', async () => {
    let resolve!: () => void
    presetsSaveCustom.mockImplementation(() => new Promise<void>((r) => { resolve = r }))
    const input = await openField()
    fireEvent.change(input, { target: { value: 'Slow Save' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await act(async () => { resolve() })
    await waitFor(() => expect(nameField()).toBeNull())
    await nextFrame()
    expect(elsewhere).toHaveFocus()
  })
})
