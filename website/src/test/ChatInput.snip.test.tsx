import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'

// Toggle capture-support + mobile per test.
const h = vi.hoisted(() => ({ supported: true, mobile: false }))
vi.mock('../hooks/useScreenSnip', () => ({ isScreenSnipSupported: () => h.supported }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => h.mobile }))
vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => vi.fn() }) }))

import ChatInput from '../components/ChatInput'

// Screenshot lives inside the "+" drop-up menu ("Add files & options"),
// which renders only when onUploadFiles is provided (ChatPage always passes both).
const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onUploadFiles: vi.fn() }
const openPlusMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))
const snipItem = () => screen.queryByRole('button', { name: /screenshot/i })

beforeEach(() => {
  h.supported = true
  h.mobile = false
})

describe('ChatInput screenshot action (in + menu)', () => {
  it('shows Screenshot in the + menu and fires onScreenshot when screen capture is supported', () => {
    const onScreenshot = vi.fn()
    renderWithProviders(<ChatInput {...base} onScreenshot={onScreenshot} isMac={false} />)
    openPlusMenu()
    const btn = snipItem()
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn!)
    expect(onScreenshot).toHaveBeenCalledTimes(1)
  })

  it('shows Screenshot as a native macOS fallback when capture is unsupported', () => {
    h.supported = false
    renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={true} />)
    openPlusMenu()
    expect(snipItem()).toBeInTheDocument()
  })

  it('hides Screenshot when capture is unsupported and not macOS', () => {
    h.supported = false
    renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={false} />)
    openPlusMenu()
    expect(snipItem()).toBeNull()
  })

  it('hides Screenshot on mobile even when capture is supported', () => {
    h.mobile = true
    renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={true} />)
    openPlusMenu()
    expect(snipItem()).toBeNull()
  })
})
