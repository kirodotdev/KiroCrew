import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement } from 'react'
import type { ChatMessage } from '../types'
import { useChatNavigation } from '../hooks/useChatNavigation'

vi.mock('../api/client', () => ({
  api: {
    resolveNavLinks: vi.fn().mockResolvedValue({ summaries: ['Pull Request', 'Design Doc'] }),
  },
}))

function msg(role: string, content: string): ChatMessage {
  return { role, content, ts: '2026-01-01T00:00:00Z' } as ChatMessage
}

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useChatNavigation', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('returns sections from user messages', () => {
    const messages = [msg('user', 'Hello world'), msg('assistant', 'Hi'), msg('user', 'Second question')]
    const map = new Map([[0, 0], [2, 2]])
    const { result } = renderHook(() => useChatNavigation(messages, map), { wrapper })
    expect(result.current.sections).toHaveLength(2)
    expect(result.current.sections[0].label).toBe('Hello world')
    expect(result.current.sections[1].label).toBe('Second question')
  })

  it('truncates long section labels at 60 chars with ellipsis', () => {
    const long = 'a'.repeat(80)
    const messages = [msg('user', long)]
    const map = new Map([[0, 0]])
    const { result } = renderHook(() => useChatNavigation(messages, map), { wrapper })
    expect(result.current.sections[0].label).toBe('a'.repeat(60) + '…')
  })

  it('skips assistant messages for sections', () => {
    const messages = [msg('assistant', 'I am a bot')]
    const map = new Map([[0, 0]])
    const { result } = renderHook(() => useChatNavigation(messages, map), { wrapper })
    expect(result.current.sections).toHaveLength(0)
  })

  it('resolves bare URL links via single batched API call', async () => {
    const { api } = await import('../api/client')
    ;(api.resolveNavLinks as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ summaries: ['Pull Request', 'Design Doc'] })
    const messages = [msg('user', 'check https://git.example.com/reviews/CR-123 and https://docs.example.com/abc')]
    const map = new Map([[0, 0]])
    const { result } = renderHook(() => useChatNavigation(messages, map), { wrapper })

    await waitFor(() => expect(result.current.resolving).toBe(false))
    expect(api.resolveNavLinks).toHaveBeenCalledTimes(1)
    const payload = (api.resolveNavLinks as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(payload).toHaveLength(2)
    expect(payload[0].url).toBe('https://git.example.com/reviews/CR-123')
    expect(payload[1].url).toBe('https://docs.example.com/abc')
    // /reviews/<id> classifies as a code-review link; the plain doc URL as 'other'
    expect(result.current.links[0].type).toBe('cr')
    expect(result.current.links[1].type).toBe('other')
    expect(result.current.links[0].label).toBe('Pull Request')
    expect(result.current.links[1].label).toBe('Design Doc')
  })

  it('does not resolve markdown links', async () => {
    const { api } = await import('../api/client')
    const messages = [msg('user', '[My Doc](https://docs.example.com/abc)')]
    const map = new Map([[0, 0]])
    const { result } = renderHook(() => useChatNavigation(messages, map), { wrapper })

    // Wait a tick to ensure no query fires
    await new Promise(r => setTimeout(r, 50))
    expect(api.resolveNavLinks).not.toHaveBeenCalled()
    expect(result.current.links[0].label).toBe('My Doc')
    expect(result.current.resolving).toBe(false)
  })

  it('extracts context around URL in batched call', async () => {
    const { api } = await import('../api/client')
    ;(api.resolveNavLinks as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ summaries: ['Example Path'] })
    const messages = [msg('user', 'line1\nline2\nhttps://example.com/path\nline4\nline5')]
    const map = new Map([[0, 0]])
    renderHook(() => useChatNavigation(messages, map), { wrapper })

    await waitFor(() => expect(api.resolveNavLinks).toHaveBeenCalled())
    expect(api.resolveNavLinks).toHaveBeenCalledTimes(1)
    const call = (api.resolveNavLinks as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(call[0].context).toContain('line2')
    expect(call[0].context).toContain('line4')
  })
})
