/**
 * Screenshot viewer on the app detail page — a named modal dialog that keeps
 * keyboard focus inside while open and gives it back to the thumbnail that
 * opened it.
 *
 * The viewer is `aria-modal`, so a Tab that walks out of it lands on page
 * controls hidden behind the overlay; and with no accessible name a screen
 * reader announces only "dialog". Gesture and prev/next behaviour live in
 * AppDetailPage.screenshotLightbox.zoom.test.tsx.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ScreenshotGallery } from '../pages/AppDetailPage'

function openViewer() {
  render(<MemoryRouter><ScreenshotGallery screenshots={['/shots/a.png', '/shots/b.png']} /></MemoryRouter>)
  const thumb = screen.getByRole('button', { name: 'Open screenshot 1' })
  thumb.focus()
  fireEvent.click(thumb)
  return { thumb, dialog: screen.getByRole('dialog') }
}

describe('ScreenshotGallery viewer focus', () => {
  it('has an accessible name', () => {
    openViewer()
    expect(screen.getByRole('dialog', { name: 'Screenshot viewer' })).toHaveAttribute('aria-modal', 'true')
  })

  it('wraps Tab from the last control back to the first', () => {
    const { dialog } = openViewer()
    const close = screen.getByRole('button', { name: 'Close' })
    const next = screen.getByRole('button', { name: 'Next' })
    expect(dialog.contains(document.activeElement)).toBe(true)
    next.focus()
    fireEvent.keyDown(next, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(next)
  })

  it('returns focus to the thumbnail on Escape', () => {
    const { thumb } = openViewer()
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(thumb)
  })
})
