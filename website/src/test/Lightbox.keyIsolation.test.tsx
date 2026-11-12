import { act, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Lightbox } from '../components/markdown/Lightbox'

// The viewer takes focus when it opens, outside the Modal that may hold the
// image (McpBrowserModal, SkillBrowserModal). A key typed there must not reach
// the app's window shortcuts (`useKeyboardShortcuts`, bubble phase), or a
// navigating chord unmounts the Modal's host and drops its unsaved state.

const open = () => window.dispatchEvent(new CustomEvent('lightbox', {
  detail: { images: [{ src: '/x.png', alt: 'x' }, { src: '/y.png', alt: 'y' }], index: 0 },
}))

const appShortcut = vi.fn()
const listen = (e: KeyboardEvent) => appShortcut(e.key)

afterEach(() => {
  window.removeEventListener('keydown', listen)
  appShortcut.mockReset()
})

function openViewer() {
  render(<Lightbox />)
  window.addEventListener('keydown', listen)
  act(() => { open() })
  const zoomIn = document.querySelector('[aria-label="Zoom in (+)"]') as HTMLElement
  zoomIn.focus()
  return zoomIn
}

describe('Lightbox key isolation', () => {
  it('keeps a shortcut typed in the viewer from reaching the app', () => {
    const zoomIn = openViewer()
    fireEvent.keyDown(zoomIn, { key: ',', metaKey: true })
    fireEvent.keyDown(zoomIn, { key: 'k', ctrlKey: true })
    expect(appShortcut).not.toHaveBeenCalled()
  })

  it('still handles its own keys and closes on Escape', () => {
    const zoomIn = openViewer()
    fireEvent.keyDown(zoomIn, { key: 'ArrowRight' })
    expect(document.querySelector('img')?.getAttribute('src')).toBe('/y.png')
    fireEvent.keyDown(zoomIn, { key: 'Escape' })
    expect(document.querySelector('[data-testid="lightbox-toolbar"]')).toBeNull()
  })
})
