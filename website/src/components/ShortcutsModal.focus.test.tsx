/**
 * ShortcutsModal is aria-modal, so it keeps Tab inside and gives focus back to
 * the opener on close. The search box still takes the first focus, and Escape
 * stays on the modal's own listener: it clears a search before it closes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { i18nT } from '../i18n/t'
import ShortcutsModal from './ShortcutsModal'

// jsdom has no layout, so every element's offsetParent is null and the trap's
// visibility filter would see no focusable controls. Give each one a parent.
const offsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent')

let trigger: HTMLButtonElement

beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get() { return (this as HTMLElement).parentElement },
  })
  trigger = document.createElement('button')
  trigger.textContent = 'zzq trigger'
  document.body.appendChild(trigger)
  trigger.focus()
})

afterEach(() => {
  if (offsetParent) Object.defineProperty(HTMLElement.prototype, 'offsetParent', offsetParent)
  trigger.remove()
})

function open(onClose = vi.fn()) {
  const view = renderWithProviders(<ShortcutsModal onClose={onClose} />)
  return { view, onClose, dialog: screen.getByRole('dialog') }
}

describe('ShortcutsModal focus', () => {
  it('focuses the search box when it opens', () => {
    open()
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: i18nT('components.shortcutsModal.search') }))
  })

  it('pulls a Tab from behind the dialog back inside, and wraps Tab and Shift+Tab', () => {
    const { dialog } = open()
    const first = screen.getByRole('button', { name: i18nT('components.shortcutsModal.close') })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    const last = document.activeElement as HTMLElement
    expect(last).not.toBe(first)
    expect(dialog.contains(last)).toBe(true)
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
  })

  it('gives focus back to the opener when it closes', () => {
    const { view } = open()
    expect(document.activeElement).not.toBe(trigger)
    view.unmount()
    expect(document.activeElement).toBe(trigger)
  })

  it('keeps page shortcuts from keys inside the dialog, but lets Escape and its own chord through', () => {
    open()
    const search = screen.getByRole('textbox', { name: i18nT('components.shortcutsModal.search') })
    const pageKeys = vi.fn()
    window.addEventListener('keydown', pageKeys)
    try {
      fireEvent.keyDown(search, { key: 'k', code: 'KeyK', ctrlKey: true })
      expect(pageKeys).not.toHaveBeenCalled()
      fireEvent.keyDown(search, { key: '/', code: 'Slash', ctrlKey: true })
      expect(pageKeys).toHaveBeenCalledTimes(1)
      fireEvent.keyDown(search, { key: 'Escape' })
      expect(pageKeys).toHaveBeenCalledTimes(2)
    } finally {
      window.removeEventListener('keydown', pageKeys)
    }
  })

  it('still clears the search on the first Escape and closes on the second', () => {
    const { onClose } = open()
    const search = screen.getByRole('textbox', { name: i18nT('components.shortcutsModal.search') })
    fireEvent.change(search, { target: { value: 'zzq' } })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(search).toHaveValue('')
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
