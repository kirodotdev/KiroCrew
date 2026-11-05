/**
 * `AppIcon`'s `icon` is an app-declared NAME (a selection action, a file-menu
 * row), so a name only `Object.prototype` has must draw the fallback glyph. A
 * bare `ICON_MAP[name]` finds `constructor` (the `Object` function) and React
 * then throws on what it returns, taking down the surface that drew the row.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import AppIcon from '../components/AppIcon'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))

describe('AppIcon icon names', () => {
  it('draws the fallback glyph for a name that only the object prototype has', () => {
    const fallback = render(<AppIcon icon="not-an-icon" />).container.innerHTML
    cleanup()
    for (const icon of ['constructor', 'toString', 'valueOf', '__proto__', 'hasOwnProperty']) {
      const { container } = render(<AppIcon icon={icon} />)
      expect(container.innerHTML, icon).toBe(fallback)
      cleanup()
    }
  })
})
