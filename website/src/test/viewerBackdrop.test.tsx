/**
 * The image and SVG viewers' light/dark backdrop button (#25815): it flips only
 * the pane behind the picture, never the dashboard theme, and the choice holds
 * for the rest of this tab through sessionStorage.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ImageViewer, SvgViewer, VIEWER_BACKDROP_KEY } from '../components/FileRenderers'
import { i18nT } from '../i18n/t'

const LIGHT = () => i18nT('components.fileRenderers.light_background')
const DARK = () => i18nT('components.fileRenderers.dark_background')

beforeEach(() => {
  sessionStorage.clear()
})

afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
  sessionStorage.clear()
})

describe('viewer backdrop toggle', () => {
  it('starts on the theme surface with the button not pressed', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    render(<ImageViewer filePath="/zzq/diagram.png" />)
    const btn = screen.getByRole('button', { name: LIGHT() })
    expect(btn.getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('viewer-backdrop').className).toContain('bg-bg-elevated')
  })

  it('on a dark theme gives a white backdrop and leaves the theme alone', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    render(<ImageViewer filePath="/zzq/diagram.png" />)
    fireEvent.click(screen.getByRole('button', { name: LIGHT() }))
    const pane = screen.getByTestId('viewer-backdrop')
    expect(pane.className).toContain('bg-white')
    expect(pane.className).not.toContain('bg-bg-elevated')
    expect(screen.getByRole('button', { name: LIGHT() }).getAttribute('aria-pressed')).toBe('true')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(sessionStorage.getItem(VIEWER_BACKDROP_KEY)).toBe('1')
  })

  it('on a light theme gives a dark backdrop, and a second press goes back', () => {
    document.documentElement.setAttribute('data-theme', 'light')
    render(<SvgViewer content={'<svg><circle r="4" /></svg>'} />)
    const btn = screen.getByRole('button', { name: DARK() })
    fireEvent.click(btn)
    expect(screen.getByTestId('viewer-backdrop').className).toContain('bg-[#1e1e1e]')
    fireEvent.click(btn)
    expect(screen.getByTestId('viewer-backdrop').className).toContain('bg-bg-elevated')
    expect(btn.getAttribute('aria-pressed')).toBe('false')
    expect(sessionStorage.getItem(VIEWER_BACKDROP_KEY)).toBe('0')
  })

  it('keeps the choice for the next file opened in this tab', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    const first = render(<ImageViewer filePath="/zzq/a.png" />)
    fireEvent.click(screen.getByRole('button', { name: LIGHT() }))
    first.unmount()
    render(<SvgViewer content={'<svg><rect width="2" height="2" /></svg>'} />)
    expect(screen.getByTestId('viewer-backdrop').className).toContain('bg-white')
    expect(screen.getByTestId('viewer-backdrop').querySelector('rect')).toBeTruthy()
  })
})
