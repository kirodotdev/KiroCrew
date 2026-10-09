/**
 * Session-control chips (the composer shelf) render their manifest `icon`
 * through AppIcon's own glyph map, not the app tab/page set in apps/appIcons.
 * A voice app's chip must get the audio glyphs instead of the Package box.
 */
import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import AppIcon from '../components/AppIcon'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

describe('AppIcon audio glyphs', () => {
  it('renders AudioLines and Mic instead of the Package fallback', () => {
    const fallback = render(<AppIcon icon="definitely-not-an-icon" size={13} />).container.innerHTML
    expect(render(<AppIcon icon="Package" size={13} />).container.innerHTML).toBe(fallback)
    for (const name of ['AudioLines', 'Mic']) {
      expect(render(<AppIcon icon={name} size={13} />).container.innerHTML).not.toBe(fallback)
    }
  })
})
