import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { useUIMode, UIModeProvider } from '../hooks/useUIMode'

// Every renderHook here wraps in the provider so useUIMode can resolve its
// context — mirrors how it's consumed in production (main.tsx wraps App in
// <UIModeProvider>).
const wrapper = ({ children }: { children: ReactNode }) => (
  <UIModeProvider>{children}</UIModeProvider>
)

function renderUIModeHook() {
  return renderHook(() => useUIMode(), { wrapper })
}

describe('useUIMode', () => {
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.ui
  })

  it("defaults to 'chat' when nothing is stored", () => {
    const { result } = renderUIModeHook()
    expect(result.current.uiMode).toBe('chat')
  })

  it("reads 'cli' from localStorage", () => {
    localStorage.setItem('mc-ui', 'cli')
    const { result } = renderUIModeHook()
    expect(result.current.uiMode).toBe('cli')
  })

  it("ignores unknown localStorage values, falling back to 'chat'", () => {
    localStorage.setItem('mc-ui', 'bogus')
    const { result } = renderUIModeHook()
    expect(result.current.uiMode).toBe('chat')
  })

  it('writes data-ui to <html> on change', () => {
    const { result } = renderUIModeHook()
    expect(document.documentElement.dataset.ui).toBe('chat')

    act(() => result.current.setUIMode('cli'))
    expect(document.documentElement.dataset.ui).toBe('cli')

    act(() => result.current.setUIMode('chat'))
    expect(document.documentElement.dataset.ui).toBe('chat')
  })

  it('persists changes to localStorage', () => {
    const { result } = renderUIModeHook()
    act(() => result.current.setUIMode('cli'))
    expect(localStorage.getItem('mc-ui')).toBe('cli')
    act(() => result.current.setUIMode('chat'))
    expect(localStorage.getItem('mc-ui')).toBe('chat')
  })

  it('toggleUIMode flips between chat and cli', () => {
    const { result } = renderUIModeHook()
    expect(result.current.uiMode).toBe('chat')
    act(() => result.current.toggleUIMode())
    expect(result.current.uiMode).toBe('cli')
    act(() => result.current.toggleUIMode())
    expect(result.current.uiMode).toBe('chat')
  })

  it('throws when used outside the provider', () => {
    // Suppress React's expected error log for this negative case.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => renderHook(() => useUIMode())).toThrow(
      /useUIMode must be used within UIModeProvider/
    )
    spy.mockRestore()
  })

  it('syncs cross-tab via the storage event', () => {
    const { result } = renderUIModeHook()
    expect(result.current.uiMode).toBe('chat')

    // Simulate another tab writing to localStorage and dispatching the event.
    act(() => {
      window.dispatchEvent(
        new StorageEvent('storage', { key: 'mc-ui', newValue: 'cli', oldValue: 'chat' })
      )
    })
    expect(result.current.uiMode).toBe('cli')

    // Unrelated keys should be ignored.
    act(() => {
      window.dispatchEvent(
        new StorageEvent('storage', { key: 'mc-theme', newValue: 'light' })
      )
    })
    expect(result.current.uiMode).toBe('cli')
  })
})
