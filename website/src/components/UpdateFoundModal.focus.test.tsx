/**
 * UpdateFoundModal is aria-modal, so it takes keyboard focus when it opens,
 * keeps Tab inside, and gives focus back on close.
 *
 * Focus left on the page behind the scrim, or a Tab that walks out of the
 * dialog, reaches controls the dialog hides. Escape stays on the modal's own
 * window listener: it snoozes a voluntary prompt and does nothing on a
 * required one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseStatus } from '../store/dashboardSlice'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import UpdateFoundModal from './UpdateFoundModal'
import type { StatusData } from '../types'

vi.mock('../api/client', () => {
  class MockApiError extends Error {}
  return {
    ApiError: MockApiError,
    api: {
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn(),
      checkUpdate: vi.fn(),
      applyUpdate: vi.fn(),
      armUpdate: vi.fn(),
      armStatus: vi.fn(),
    },
  }
})

const mockedApi = vi.mocked(api)

/** Upper bound on the chained reads that open the dialog. */
const DIALOG_WAIT_MS = 5000

// jsdom has no layout, so every element's offsetParent is null and the trap's
// visibility filter would see no focusable controls. Give each one a parent.
const offsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')

let trigger: HTMLButtonElement

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get() { return (this as HTMLElement).parentElement },
  })
  mockedApi.kirocrewConfig.mockReset()
  mockedApi.patchConfig.mockReset()
  mockedApi.checkUpdate.mockReset()
  mockedApi.kirocrewConfig.mockResolvedValue({ dashboard: { update_nudge: {} } } as never)
  mockedApi.patchConfig.mockResolvedValue({} as never)
  mockedApi.checkUpdate.mockResolvedValue({ changes: '' } as never)
  ;(window as unknown as { updateAPI?: object }).updateAPI = { download: vi.fn().mockResolvedValue(undefined) }
  trigger = document.createElement('button')
  trigger.textContent = 'zzq trigger'
  document.body.appendChild(trigger)
  trigger.focus()
})

afterEach(() => {
  if (offsetParent) Object.defineProperty(HTMLElement.prototype, 'offsetParent', offsetParent)
  delete (window as unknown as { updateAPI?: unknown }).updateAPI
  trigger.remove()
})

async function openDesktop() {
  const rendered = renderWithProviders(<UpdateFoundModal />)
  await act(async () => {
    rendered.queryClient.setQueryData(['update-state'], { state: 'found', version: '9.9.9', notes: '' })
  })
  // The dialog opens only after two chained reads land (the update-info reread,
  // then the update-nudge record), so the wait gets an explicit bound.
  return screen.findByRole('dialog', undefined, { timeout: DIALOG_WAIT_MS })
}

async function openRequired() {
  const store = createTestStore()
  store.dispatch(sseStatus({
    update_available: true,
    update_latest_version: '8.8.8',
    update_can_apply: true,
    update_required: true,
    update_min_version: '8.0.0',
  } as StatusData))
  renderWithProviders(<UpdateFoundModal />, { store })
  return screen.findByRole('dialog', undefined, { timeout: DIALOG_WAIT_MS })
}

const byName = (key: string) => screen.getByRole('button', { name: i18nT(key) })

describe('UpdateFoundModal focus', () => {
  it('moves focus onto the dialog when it opens', async () => {
    const dialog = await openDesktop()
    await waitFor(() => expect(document.activeElement).toBe(dialog))
  })

  it('pulls a Tab from behind the dialog back inside, and wraps Tab and Shift+Tab', async () => {
    await openDesktop()
    const first = byName('components.updateFoundModal.dismiss')
    const last = byName('components.updateFoundModal.download')
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('gives focus back to the opener when it is dismissed', async () => {
    await openDesktop()
    expect(document.activeElement).not.toBe(trigger)
    fireEvent.click(byName('components.updateFoundModal.remind_me_tomorrow'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps keystrokes inside the dialog from page shortcuts, but lets Escape through', async () => {
    const dialog = await openDesktop()
    const pageKeys = vi.fn()
    document.addEventListener('keydown', pageKeys)
    try {
      fireEvent.keyDown(dialog, { key: 'k', metaKey: true })
      expect(pageKeys).not.toHaveBeenCalled()
      fireEvent.keyDown(dialog, { key: 'Escape' })
      expect(pageKeys).toHaveBeenCalledTimes(1)
    } finally {
      document.removeEventListener('keydown', pageKeys)
    }
  })

  it('still snoozes once on Escape', async () => {
    await openDesktop()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(mockedApi.patchConfig).toHaveBeenCalledTimes(1)
  })

  it('keeps Tab inside a required update, and Escape still does nothing', async () => {
    const dialog = await openRequired()
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Tab' })
    expect(dialog.contains(document.activeElement)).toBe(true)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(mockedApi.patchConfig).not.toHaveBeenCalled()
  })
})
