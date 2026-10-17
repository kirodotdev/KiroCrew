import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'

// Toggle capture support and input capability per test.
const h = vi.hoisted(() => ({ supported: true, mobile: false, touch: false }))
vi.mock('../hooks/useScreenSnip', () => ({ isScreenSnipSupported: () => h.supported }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => h.mobile }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => h.touch }))
vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => vi.fn() }) }))

import ChatInput from '../components/ChatInput'

const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onUploadFiles: vi.fn() }
const openPlusMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))
const snipItem = () => screen.queryByRole('button', { name: /screenshot/i })

function expectDirectFilePicker(container: HTMLElement) {
  const fileInput = screen.getByLabelText('Attach files', { selector: 'input[type="file"]' })
  const picker = container.querySelector('label[aria-label="Attach files"]')
  expect(picker).toHaveAttribute('for', fileInput.id)
  expect(container.querySelector('button[title="Add files & options"]')).toBeNull()
}

beforeEach(() => {
  h.supported = true
  h.mobile = false
  h.touch = false
})

describe('ChatInput screenshot action', () => {
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

  it('uses the accessible direct file picker on mobile instead of Screenshot', () => {
    h.mobile = true
    const { container } = renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={true} />)
    expectDirectFilePicker(container)
    expect(snipItem()).toBeNull()
  })

  it('uses the accessible direct file picker on touch devices', () => {
    h.touch = true
    const { container } = renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={true} />)
    expectDirectFilePicker(container)
    expect(snipItem()).toBeNull()
  })
})
