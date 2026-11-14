import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor, cleanup } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cachePreviewFlag, PREVIEW_CHAT_PAGE_COLORS } from '../../utils/previewFlags'
import { useChatPageTheme } from './useChatPageTheme'

const memberDashboard = vi.fn()
vi.mock('../../api/client', () => ({
  api: { memberDashboard: (...a: unknown[]) => memberDashboard(...a) },
}))

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  memberDashboard.mockReset()
  cachePreviewFlag(PREVIEW_CHAT_PAGE_COLORS, false)
})
afterEach(() => cleanup())

describe('useChatPageTheme', () => {
  it('returns {} and fetches nothing while the preview is off — today\'s chat, unchanged', async () => {
    memberDashboard.mockResolvedValue({ page_theme: { '--bg': '#101418' } })
    const { result } = renderHook(() => useChatPageTheme('slug', 'Bot'), { wrapper: wrap() })
    // The flag is off, so the query stays disabled: the result settles at {} and
    // the read is never issued. Settling on the positive ({}) is what proves the
    // disabled query produced no vars; the fetch count is then read off the mock.
    await waitFor(() => expect(result.current).toEqual({}))
    expect(memberDashboard).not.toHaveBeenCalled()
  })

  it('derives readable chat vars from the page theme once the preview is on', async () => {
    cachePreviewFlag(PREVIEW_CHAT_PAGE_COLORS, true)
    // A light violet accent reads on the dark page's surfaces (as text and as a
    // filled-button label), so the chat follows both the bg and the accent.
    memberDashboard.mockResolvedValue({ page_theme: { '--bg': '#101418', '--accent': '#a78bfa' } })
    const { result } = renderHook(() => useChatPageTheme('slug', 'Bot'), { wrapper: wrap() })
    await waitFor(() => expect((result.current as Record<string, string>)['--bg']).toBe('#101418'))
    // Reads through the tab's own locale-carrying call (slug, member, locale),
    // so it shares the tab's cache entry and arms no second live page.
    expect(memberDashboard).toHaveBeenCalledWith('slug', 'Bot', expect.any(String))
    const vars = result.current as Record<string, string>
    expect(vars['--text']).toBe('#ffffff')
    expect(vars['--accent']).toBe('#a78bfa')
    // Inherited text re-reads the derived `--text`: overriding the variable alone
    // leaves an already-computed app foreground on chrome with no text class.
    expect(vars.color).toBe('var(--text)')
  })

  it('is {} with the preview on but no page colours', async () => {
    cachePreviewFlag(PREVIEW_CHAT_PAGE_COLORS, true)
    memberDashboard.mockResolvedValue({ state: 'live' })
    const { result } = renderHook(() => useChatPageTheme('slug', 'Bot'), { wrapper: wrap() })
    // The read resolves with no page_theme; once it has landed the mapper has
    // nothing to apply, so the result stays {}. waitFor settles on the arrived
    // read (the call) and the derived {} together.
    await waitFor(() => {
      expect(memberDashboard).toHaveBeenCalled()
      expect(result.current).toEqual({})
    })
  })

  it('never re-reads on mount or focus: the tab owns refresh, a re-read would re-arm a live page', async () => {
    cachePreviewFlag(PREVIEW_CHAT_PAGE_COLORS, true)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    memberDashboard.mockResolvedValue({ page_theme: { '--bg': '#101418' } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const first = renderHook(() => useChatPageTheme('slug', 'Bot'), { wrapper })
    await waitFor(() => expect(memberDashboard).toHaveBeenCalledTimes(1))
    first.unmount()
    // Age the entry past any finite staleTime, then mount again.
    const key = qc.getQueryCache().getAll()[0].queryKey
    qc.setQueryData(key, { page_theme: { '--bg': '#101418' } }, { updatedAt: 0 })
    renderHook(() => useChatPageTheme('slug', 'Bot'), { wrapper })
    await new Promise(r => setTimeout(r, 20))
    expect(memberDashboard).toHaveBeenCalledTimes(1)
  })
})
