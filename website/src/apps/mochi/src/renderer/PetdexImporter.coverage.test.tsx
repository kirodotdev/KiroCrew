/**
 * PetdexImporter: installed pets, fetch-by-name, the error shapes, and the exits.
 * `mochiApi` is the only seam doubled; `Image` is stubbed for the measure step.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { PetdexImporter } from './PetdexImporter'
import { api } from '../mochiApi'
import type { PetdexPet } from '../../petdexImport'
import { i18nT } from '../../../../i18n/t'

vi.mock('../mochiApi', () => ({
  api: { petdexListInstalled: vi.fn(), petdexImport: vi.fn() },
}))

const mocked = api as unknown as {
  petdexListInstalled: ReturnType<typeof vi.fn>
  petdexImport: ReturnType<typeof vi.fn>
}

const NAME_PH = i18nT('apps.mochi.petdex.name_placeholder')
const FETCH = i18nT('apps.mochi.petdex.fetch')
const NONE = i18nT('apps.mochi.petdex.none_installed')

const pet = (): PetdexPet => ({
  slug: 'sprout',
  meta: { id: 'sprout-id', displayName: 'Sprout', description: 'A small green friend' },
  imageMime: 'image/webp',
  imageBase64: 'AAAA',
  source: 'remote',
})

let loadMode: 'ok' | 'fail' = 'ok'
class FakeImage {
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  naturalWidth = 1536
  naturalHeight = 1872
  set src(_value: string) {
    queueMicrotask(() => (loadMode === 'ok' ? this.onload?.() : this.onerror?.()))
  }
}

const renderImporter = async (installed: unknown[] = []) => {
  mocked.petdexListInstalled.mockResolvedValue(installed)
  const fns = { onReady: vi.fn(), onUseFile: vi.fn(), onCancel: vi.fn() }
  render(<PetdexImporter {...fns} />)
  // The installed list loads in an effect; wait for it so no update lands late.
  await screen.findByText(installed.length ? 'Cat' : NONE)
  return fns
}

const typeName = (value: string) =>
  fireEvent.change(screen.getByPlaceholderText(NAME_PH), { target: { value } })

beforeEach(() => {
  loadMode = 'ok'
  mocked.petdexImport.mockReset()
  vi.stubGlobal('Image', FakeImage)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PetdexImporter installed pets', () => {
  it('imports the chosen installed pet from the local source', async () => {
    const fns = await renderImporter([{ slug: 'cat', name: 'Cat', description: 'Sleepy' }])
    expect(screen.getByText('Sleepy')).toBeTruthy()
    mocked.petdexImport.mockResolvedValue({ ok: true, value: pet() })
    fireEvent.click(screen.getByText('Cat'))
    await waitFor(() => expect(fns.onReady).toHaveBeenCalledTimes(1))
    expect(mocked.petdexImport).toHaveBeenCalledWith('cat', 'local')
  })
})

describe('PetdexImporter fetch by name or link', () => {
  it('keeps Fetch disabled until a non-blank name is typed', async () => {
    await renderImporter()
    const btn = screen.getByRole('button', { name: FETCH }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    typeName('   ')
    expect(btn.disabled).toBe(true)
    typeName('sprout')
    expect(btn.disabled).toBe(false)
  })

  it('fetches the trimmed name from the remote source and hands off the prefill', async () => {
    const fns = await renderImporter()
    mocked.petdexImport.mockResolvedValue({ ok: true, value: pet() })
    typeName('  sprout  ')
    fireEvent.click(screen.getByRole('button', { name: FETCH }))
    await waitFor(() => expect(fns.onReady).toHaveBeenCalledTimes(1))
    expect(mocked.petdexImport).toHaveBeenCalledWith('sprout', 'remote')
    expect(fns.onReady.mock.calls[0][0]).toEqual(
      expect.objectContaining({ name: 'Sprout', author: 'petdex.dev' }),
    )
  })

  it('submits on Enter in the name field', async () => {
    const fns = await renderImporter()
    mocked.petdexImport.mockResolvedValue({ ok: true, value: pet() })
    typeName('sprout')
    fireEvent.keyDown(screen.getByPlaceholderText(NAME_PH), { key: 'Enter' })
    await waitFor(() => expect(fns.onReady).toHaveBeenCalledTimes(1))
  })

  it('shows the gateway error text when the fetch is refused', async () => {
    const fns = await renderImporter()
    mocked.petdexImport.mockResolvedValue({ ok: false, error: 'No pet named sprout' })
    typeName('sprout')
    fireEvent.click(screen.getByRole('button', { name: FETCH }))
    expect(await screen.findByText('No pet named sprout')).toBeTruthy()
    expect(fns.onReady).not.toHaveBeenCalled()
  })

  it('reports the bridge as unavailable when no result comes back', async () => {
    await renderImporter()
    mocked.petdexImport.mockResolvedValue(undefined)
    typeName('sprout')
    fireEvent.click(screen.getByRole('button', { name: FETCH }))
    expect(await screen.findByText(i18nT('apps.mochi.petdex.unavailable'))).toBeTruthy()
  })

  it('reports a sheet that will not decode instead of handing it off', async () => {
    loadMode = 'fail'
    const fns = await renderImporter()
    mocked.petdexImport.mockResolvedValue({ ok: true, value: pet() })
    typeName('sprout')
    fireEvent.click(screen.getByRole('button', { name: FETCH }))
    expect(await screen.findByText('could not read that spritesheet')).toBeTruthy()
    expect(fns.onReady).not.toHaveBeenCalled()
  })
})

describe('PetdexImporter exits', () => {
  it('opens the own-file importer and cancels from the header, without a hand-off', async () => {
    const fns = await renderImporter()
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.mochi.petdex.choose_file') }))
    expect(fns.onUseFile).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: i18nT('apps.mochi.petdex.cancel') }))
    expect(fns.onCancel).toHaveBeenCalledTimes(1)
    expect(fns.onReady).not.toHaveBeenCalled()
  })
})
