/**
 * UpdateModal's "update ready" dialog is aria-modal, so it takes keyboard
 * focus when it opens, keeps Tab inside, and gives focus back on close.
 *
 * Focus left on the page behind the scrim, or a Tab that walks out of the
 * dialog, reaches controls the dialog hides. Escape stays on the modal's own
 * window listener and still dismisses it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { i18nT } from '../i18n/t'
import UpdateModal from './UpdateModal'

// jsdom has no layout, so every element's offsetParent is null and the trap's
// visibility filter would see no focusable controls. Give each one a parent.
const offsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')

/** Upper bound on the cache notification that opens the dialog. */
const DIALOG_WAIT_MS = 5000

let trigger: HTMLButtonElement

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get() { return (this as HTMLElement).parentElement },
  })
  ;(window as unknown as { updateAPI?: object }).updateAPI = { install: vi.fn().mockResolvedValue(undefined) }
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

async function openDialog() {
  const rendered = renderWithProviders(<UpdateModal />)
  const push = async (next: Record<string, unknown>) => {
    await act(async () => { rendered.queryClient.setQueryData(['update-state'], next) })
  }
  await push({ state: 'downloaded', version: '9.9.9' })
  // React Query hands the cache write to the component on a later tick.
  return { ...rendered, push, dialog: await screen.findByRole('dialog', undefined, { timeout: DIALOG_WAIT_MS }) }
}

const byName = (key: string) => screen.getByRole('button', { name: i18nT(key) })

describe('UpdateModal focus', () => {
  it('moves focus onto the dialog node, not its dismiss button, when it opens', async () => {
    const { dialog } = await openDialog()
    expect(document.activeElement).toBe(dialog)
  })

  it('keeps keystrokes inside the dialog from page shortcuts, but lets Escape through', async () => {
    const { dialog } = await openDialog()
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

  it('moves focus onto the installing screen, not back behind it, once the install starts', async () => {
    const { push } = await openDialog()
    fireEvent.click(byName('components.updateModal.restart_update'))
    await push({ state: 'installing', version: '9.9.9' })
    const takeover = await screen.findByRole('alert')
    expect(document.activeElement).toBe(takeover)
  })

  it('pulls a Tab from behind the dialog back inside, and wraps Tab and Shift+Tab', async () => {
    await openDialog()
    const first = byName('components.updateModal.dismiss')
    const last = byName('components.updateModal.restart_update')
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
    await openDialog()
    expect(document.activeElement).not.toBe(trigger)
    fireEvent.click(byName('components.updateModal.later'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('still dismisses on Escape', async () => {
    await openDialog()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})
