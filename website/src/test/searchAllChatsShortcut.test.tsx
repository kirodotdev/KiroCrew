/**
 * ⌘⇧F / Ctrl+Shift+F opens Search all chats, and the shell owns the chord: a
 * page's own window-level ⌘F listener never answers it as well.
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
}))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: '' }) }))
vi.mock('../hooks/useInstanceShortcuts', () => ({ useInstanceShortcuts: () => {} }))
vi.mock('../hooks/useAutoConnectInstances', () => ({ useAutoConnectInstances: () => {} }))
vi.mock('../hooks/useCommandPalette', () => ({ useCommandPalette: () => ({ open: false, setOpen: () => {} }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({}) }))

import { useShellKeyboard } from '../shell/shortcuts/shellKeyboard'
import { closeGlobalChatSearch, useGlobalChatSearchOpen } from '../components/globalChatSearch/globalChatSearchStore'

function harness() {
  const store = createTestStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}><Provider store={store}><MemoryRouter>{children}</MemoryRouter></Provider></QueryClientProvider>
  )
  renderHook(() => useShellKeyboard({
    toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
    isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
  }), { wrapper })
  return renderHook(() => useGlobalChatSearchOpen())
}

afterEach(() => { act(() => closeGlobalChatSearch()) })

describe('⌘⇧F / Ctrl+Shift+F', () => {
  it('opens Search all chats and does not reach a page listener on window', () => {
    const open = harness()
    const pageListener = vi.fn()
    window.addEventListener('keydown', pageListener)
    try {
      act(() => {
        document.dispatchEvent(new KeyboardEvent('keydown', {
          code: 'KeyF', key: 'f', ctrlKey: true, shiftKey: true, cancelable: true, bubbles: true,
        }))
      })
    } finally {
      window.removeEventListener('keydown', pageListener)
    }
    expect(open.result.current).toBe(true)
    expect(pageListener).not.toHaveBeenCalled()
  })
})
