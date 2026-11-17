/**
 * Settings ▸ Chat ▸ Sessions ▸ Sessions per creator.
 *
 * Load-bearing assertions:
 *   (1) the field shows the saved limit, and 50 when none is saved;
 *   (2) a number in range saves to `session.max_per_creator` on blur or Enter;
 *   (3) a number outside 10-450 is saved as the nearest bound, and the row says
 *       which number it saved, once the save succeeded or when that bound is
 *       already saved and nothing is sent;
 *   (4) text that is not a whole number saves nothing and the saved limit comes back;
 *   (5) a failed save keeps the typed number, says the save failed, and shows no
 *       "Saved" line.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const server = vi.hoisted(() => ({ cap: undefined as number | undefined }))
const mocks = vi.hoisted(() => ({ patchConfig: vi.fn() }))
vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: () => Promise.resolve({ session: server.cap === undefined ? {} : { max_per_creator: server.cap } }),
    patchConfig: mocks.patchConfig,
  },
}))

import { SessionsPerCreatorSetting } from '../pages/settings/SessionsPerCreatorSetting'

function renderSetting() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><SessionsPerCreatorSetting /></QueryClientProvider>)
}

async function field(): Promise<HTMLInputElement> {
  const el = await screen.findByRole('spinbutton', { name: 'Sessions per creator' }) as HTMLInputElement
  await waitFor(() => expect(el).not.toBeDisabled())
  return el
}

function type(el: HTMLInputElement, text: string) {
  fireEvent.change(el, { target: { value: text } })
  fireEvent.blur(el)
}

beforeEach(() => {
  server.cap = undefined
  mocks.patchConfig.mockReset()
  mocks.patchConfig.mockImplementation((_path: string, value: number) => {
    server.cap = value
    return Promise.resolve({ ok: true })
  })
})

describe('SessionsPerCreatorSetting', () => {
  it('shows 50 when no limit is saved, and the saved limit otherwise', async () => {
    const first = renderSetting()
    await waitFor(async () => expect((await field()).value).toBe('50'))
    first.unmount()

    server.cap = 120
    renderSetting()
    await waitFor(async () => expect((await field()).value).toBe('120'))
  })

  it('saves a number in range on blur', async () => {
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('50'))
    type(el, '120')
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('session.max_per_creator', 120))
    await waitFor(() => expect(el).not.toBeDisabled())
    expect(el.value).toBe('120')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('saves on Enter', async () => {
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('50'))
    fireEvent.change(el, { target: { value: '75' } })
    fireEvent.keyDown(el, { key: 'Enter' })
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('session.max_per_creator', 75))
  })

  it.each([
    ['9000', 450],
    ['3', 10],
  ])('saves %s as the nearest bound and says so', async (typed, stored) => {
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('50'))
    type(el, typed)
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('session.max_per_creator', stored))
    await waitFor(() => expect(el).not.toBeDisabled())
    expect(el.value).toBe(String(stored))
    expect(screen.getByRole('status').textContent).toContain(`Saved ${stored}.`)
    expect(screen.getByRole('status').textContent).toContain('(10 to 450)')
  })

  it.each(['', 'abc', '60.5'])('saves nothing for %j and restores the saved limit', async (typed) => {
    server.cap = 80
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('80'))
    type(el, typed)
    await waitFor(() => expect(el.value).toBe('80'))
    expect(mocks.patchConfig).not.toHaveBeenCalled()
  })

  it('does not save when the field was never edited', async () => {
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('50'))
    fireEvent.blur(el)
    expect(mocks.patchConfig).not.toHaveBeenCalled()
  })

  it('keeps the typed number and reports a failed save', async () => {
    mocks.patchConfig.mockImplementation(() => Promise.reject(new Error('offline')))
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('50'))
    type(el, '200')
    expect(await screen.findByText('Failed to save sessions per creator')).toBeTruthy()
    await waitFor(() => expect(el).not.toBeDisabled())
    expect(el.value).toBe('200')
  })

  it('shows no "Saved" line when a clamped save fails', async () => {
    mocks.patchConfig.mockImplementation(() => Promise.reject(new Error('offline')))
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('50'))
    type(el, '9000')
    expect(await screen.findByText('Failed to save sessions per creator')).toBeTruthy()
    await waitFor(() => expect(el).not.toBeDisabled())
    expect(mocks.patchConfig).toHaveBeenCalledWith('session.max_per_creator', 450)
    expect(el.value).toBe('450')
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('says which number is saved, and sends nothing, when the bound is already saved', async () => {
    server.cap = 450
    renderSetting()
    const el = await field()
    await waitFor(() => expect(el.value).toBe('450'))
    type(el, '9000')
    expect((await screen.findByRole('status')).textContent).toContain('Saved 450.')
    expect(el.value).toBe('450')
    expect(mocks.patchConfig).not.toHaveBeenCalled()
  })
})
