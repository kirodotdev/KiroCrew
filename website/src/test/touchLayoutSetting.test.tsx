/**
 * The "Touch screen layout" Settings row (Settings -> Display -> View): drawn
 * only on a touch-first device, saved on this browser as `kc-touch-layout`, and
 * applied at once. A touch-first device gets the select's native list, so the
 * pick is a change event on the <select>. The layout rule it feeds is covered
 * in useIsMobile.touchForm.test.ts.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { TOUCH_LAYOUT_KEY, readTouchLayoutChoice, touchFirstForm } from '../lib/touchLayoutForm'
import { TouchLayoutSetting } from '../pages/settings/TouchLayoutSetting'

const LABEL = 'Touch screen layout'

function touchFirst(on: boolean) {
  vi.spyOn(window, 'matchMedia').mockImplementation(
    (query: string) => ({
      matches: on && (query === '(pointer: coarse)' || query === '(hover: none)'),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as MediaQueryList,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.removeItem(TOUCH_LAYOUT_KEY)
})

describe('Settings → Display → Touch screen layout', () => {
  it('is hidden on a device with a mouse', () => {
    touchFirst(false)
    renderWithProviders(<TouchLayoutSetting />)
    expect(screen.queryByRole('combobox', { name: LABEL })).toBeNull()
  })

  it('starts on Automatic, and each pick is saved and changes the form at once', () => {
    touchFirst(true)
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15')
    renderWithProviders(<TouchLayoutSetting />)
    const select = screen.getByRole('combobox', { name: LABEL })
    expect(select).toHaveValue('auto')
    expect(readTouchLayoutChoice()).toBe('auto')
    expect(touchFirstForm()).toBeNull()

    fireEvent.change(select, { target: { value: 'phone' } })
    expect(localStorage.getItem(TOUCH_LAYOUT_KEY)).toBe('phone')
    expect(touchFirstForm()).toBe(true)

    fireEvent.change(select, { target: { value: 'desktop' } })
    expect(localStorage.getItem(TOUCH_LAYOUT_KEY)).toBe('desktop')
    expect(touchFirstForm()).toBeNull()
    expect(select).toHaveValue('desktop')
  })

  it('shows the saved choice', () => {
    touchFirst(true)
    localStorage.setItem(TOUCH_LAYOUT_KEY, 'phone')
    renderWithProviders(<TouchLayoutSetting />)
    expect(screen.getByRole('combobox', { name: LABEL })).toHaveValue('phone')
  })
})
