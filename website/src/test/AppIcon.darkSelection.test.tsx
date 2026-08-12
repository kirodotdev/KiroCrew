/**
 * AppIcon dark-appearance selection.
 *
 * Raster icons have fixed bytes, so an app may ship a dark variant. Resolution
 * mirrors `useHeroArt`: prefer the current theme's art, fall back to the other
 * one in BOTH directions — an app that ships only a dark icon must still render
 * it in light mode rather than dropping to the lucide glyph, which would read as
 * "this app has no icon".
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AppIcon from '../components/AppIcon'

const theme = { current: 'light' as 'light' | 'dark' }
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: theme.current }) }))

const LIGHT = '/api/apps/blob?repo=Demo&path=icon.png'
const DARK = '/api/apps/blob?repo=Demo&path=icon-dark.png'

function srcOf(): string {
  return screen.getByRole('presentation', { hidden: true }).getAttribute('src') || ''
}

describe('AppIcon dark selection', () => {
  beforeEach(() => { theme.current = 'light' })

  it('renders the light icon in light mode', () => {
    render(<AppIcon iconUrl={LIGHT} iconUrlDark={DARK} />)
    expect(srcOf()).toBe(LIGHT)
  })

  it('renders the dark icon in dark mode', () => {
    theme.current = 'dark'
    render(<AppIcon iconUrl={LIGHT} iconUrlDark={DARK} />)
    expect(srcOf()).toBe(DARK)
  })

  it('falls back to the light icon in dark mode when no dark variant exists', () => {
    theme.current = 'dark'
    render(<AppIcon iconUrl={LIGHT} />)
    expect(srcOf()).toBe(LIGHT)
  })

  it('falls back to the dark icon in light mode when only a dark variant exists', () => {
    render(<AppIcon iconUrlDark={DARK} />)
    expect(srcOf()).toBe(DARK)
  })

  it('falls through to the lucide glyph when neither variant exists', () => {
    const { container } = render(<AppIcon icon="Shield" />)
    expect(container.querySelector('svg')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })
})
