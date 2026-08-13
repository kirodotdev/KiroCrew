/**
 * SessionColorPicker — the small colour dot next to a session title. The
 * behaviour worth pinning: it renders nothing without a slot key, the dot is
 * tinted only when colorIndex actually addresses a palette entry (an
 * out-of-range or negative index must fall back to the untinted dot rather than
 * inline an `undefined` colour), and picking writes optimistically to the store
 * AND persists through api.setSlotColor.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { i18nT } from '../i18n/t'

const mocks = vi.hoisted(() => ({ setSlotColor: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))
// useSessionPalette reads CSS vars through useTheme (needs a ThemeProvider);
// a fixed palette keeps this a focused unit test of the picker itself.
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))

import { store } from '../store'
import SessionColorPicker from '../pages/chat/SessionColorPicker'

const SLOT = 'chat-picker-zzq'
const wrap = (ui: ReactNode) => render(<Provider store={store}>{ui}</Provider>)
const openPicker = () => fireEvent.click(screen.getByLabelText(i18nT('pages.chat.sessionColorPicker.session_color')))

// NB: braces, not a concise arrow body — `mockResolvedValue` RETURNS the mock,
// and vitest treats a function returned from a hook as a teardown callback,
// which would invoke the mock (a phantom no-arg call) after each test.
beforeEach(() => { mocks.setSlotColor.mockResolvedValue({}) })
afterEach(() => vi.clearAllMocks())

describe('SessionColorPicker', () => {
  it('renders nothing without a slot key', () => {
    const { container } = wrap(<SessionColorPicker colorIndex={1} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('tints the dot with the addressed palette colour', () => {
    const { container } = wrap(<SessionColorPicker slotKey={SLOT} colorIndex={1} />)
    const dot = container.querySelector('span.rounded-full') as HTMLElement
    expect(dot.style.background).toBe('#00ff00')
    expect(dot.style.borderColor).toBe('#00ff00')
  })

  it('leaves the dot untinted for a null, negative or out-of-range index', () => {
    for (const idx of [null, -1, 99]) {
      const { container, unmount } = wrap(<SessionColorPicker slotKey={SLOT} colorIndex={idx} />)
      const dot = container.querySelector('span.rounded-full') as HTMLElement
      expect(dot.style.background).toBe('transparent')
      unmount()
    }
  })

  it('opens a swatch group with one button per palette colour plus No color', () => {
    wrap(<SessionColorPicker slotKey={SLOT} colorIndex={null} />)
    openPicker()
    const group = screen.getByRole('group', { name: i18nT('pages.chat.sessionColorPicker.session_colors') })
    expect(group.querySelectorAll('button')).toHaveLength(4)
    expect(screen.getByLabelText(i18nT('pages.chat.sessionColorPicker.no_color'))).toHaveAttribute('aria-pressed', 'true')
  })

  it('persists the picked palette index and closes the popover', async () => {
    wrap(<SessionColorPicker slotKey={SLOT} colorIndex={null} />)
    openPicker()
    const group = screen.getByRole('group', { name: i18nT('pages.chat.sessionColorPicker.session_colors') })
    // Index 0 of the palette is the second button (No color leads the row).
    fireEvent.click(group.querySelectorAll('button')[1])

    await waitFor(() => expect(mocks.setSlotColor).toHaveBeenCalledWith(SLOT, 0))
    await waitFor(() => expect(screen.queryByRole('group')).not.toBeInTheDocument())
  })

  it('persists null when No color is picked', async () => {
    wrap(<SessionColorPicker slotKey={SLOT} colorIndex={2} />)
    openPicker()
    fireEvent.click(screen.getByLabelText(i18nT('pages.chat.sessionColorPicker.no_color')))
    await waitFor(() => expect(mocks.setSlotColor).toHaveBeenCalledWith(SLOT, null))
  })
})
