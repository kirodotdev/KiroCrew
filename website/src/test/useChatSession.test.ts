import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ── Mocks ──
// IMPORTANT: useAppApi must return a STABLE reference (same as real impl uses useMemo).
// Returning a fresh object each call causes infinite re-render loops because
// the useEffect depends on `api`.

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockPatch = vi.fn()
const mockNavigate = vi.fn()

const stableApi = { get: mockGet, post: mockPost, patch: mockPatch }

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => stableApi,
  useNavigate: () => mockNavigate,
}))

import { useChatSession, type ChatSessionOptions } from '../app-sdk/useChatSession'

// Reproduce the hash function so we can predict slot names
function hashStr(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}

const defaultOpts: ChatSessionOptions = {
  workspacePath: '/ws/test',
  label: 'TestLabel',
  agent: 'test-agent',
  appName: 'workbench',
}

const expectedSlotName = 'workbench-' + hashStr('/ws/test')

let queryClient: QueryClient

function createWrapper() {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children)
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockGet.mockReset()
  mockPost.mockReset()
  mockPatch.mockReset()
  mockNavigate.mockReset()
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
})

describe('useChatSession', () => {
  describe('initial load - slot exists', () => {
    it('transitions to ready when matching slot found', async () => {
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, title: 'My Session', messages: 3, running: false },
          ])
        }
        if (path === '/api/chat/folders') {
          return Promise.resolve([{ id: 'f1', name: 'Workbench' }])
        }
        return Promise.resolve({})
      })
      mockPatch.mockResolvedValue({})

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })

      expect(result.current.status).toBe('loading')

      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })

      expect(result.current.slotKey).toBe(expectedSlotName)
      expect(result.current.slotInfo).toEqual({
        key: expectedSlotName,
        title: 'My Session',
        messages: 3,
        running: false,
      })
      expect(result.current.error).toBeNull()
    })

    it('uses label as fallback title when slot has no title', async () => {
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, messages: 0, running: false },
          ])
        }
        if (path === '/api/chat/folders') return Promise.resolve([])
        return Promise.resolve({})
      })
      mockPost.mockResolvedValue({ id: 'f2', name: 'Workbench' })
      mockPatch.mockResolvedValue({})

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })

      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })
      expect(result.current.slotInfo?.title).toBe('TestLabel')
    })
  })

  describe('initial load - no matching slot', () => {
    it('transitions to no-session when slot not found', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })

      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })
      expect(result.current.slotKey).toBeNull()
      expect(result.current.slotInfo).toBeNull()
    })
  })

  describe('initial load - API error', () => {
    it('transitions to error status on fetch failure', async () => {
      mockGet.mockRejectedValue(new Error('Network error'))

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })

      await waitFor(() => {
        expect(result.current.status).toBe('error')
      })
      expect(result.current.error).toBe('Network error')
    })
  })

  describe('createSession', () => {
    it('creates a slot, sends seed message, and transitions to ready', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })

      // Set up mocks for creation flow
      mockPost.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve({ key: expectedSlotName })
        }
        if (path === '/api/chat?ws=1') {
          return Promise.resolve({ ok: true })
        }
        if (path === '/api/chat/folders') {
          return Promise.resolve({ id: 'f1', name: 'Workbench' })
        }
        return Promise.resolve({})
      })
      // After creation, the query will be invalidated and re-fetched.
      // The re-fetch must find the newly created slot.
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, title: 'TestLabel', messages: 0, running: true },
          ])
        }
        if (path === '/api/chat/folders') return Promise.resolve([{ id: 'f1', name: 'Workbench' }])
        return Promise.resolve({})
      })
      mockPatch.mockResolvedValue({})

      await act(async () => {
        await result.current.createSession()
      })

      // After mutation + query invalidation + refetch, status should become ready
      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })
      expect(result.current.slotKey).toBe(expectedSlotName)
      expect(result.current.slotInfo?.running).toBe(true)

      expect(mockPost).toHaveBeenCalledWith('/api/chat/slots', {
        name: expectedSlotName,
        agent: 'test-agent',
      })

      // The seed rides the shared transport: JSON-receipt endpoint, agent on the wire.
      expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', expect.objectContaining({
        slot: expectedSlotName,
        agent: 'test-agent',
      }))
    })

    // A lookup that never lists the created key must not leave the hook loading.
    it('is ready from the created slot when the lookup never lists it', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })

      mockPost.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') return Promise.resolve({ key: expectedSlotName })
        if (path === '/api/chat/folders') return Promise.resolve({ id: 'f1', name: 'Workbench' })
        return Promise.resolve({})
      })
      mockPatch.mockResolvedValue({})

      await act(async () => {
        await result.current.createSession()
      })

      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })
      expect(result.current.slotKey).toBe(expectedSlotName)
      expect(result.current.error).toBeNull()
    })

    it('transitions to error when the create returns no slot key', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })

      mockPost.mockResolvedValue({})

      await act(async () => {
        try {
          await result.current.createSession()
        } catch {
          // mutateAsync throws on rejection — expected
        }
      })

      await waitFor(() => {
        expect(result.current.status).toBe('error')
      })
      expect(result.current.error).toBeTruthy()
      expect(result.current.slotKey).toBeNull()
      expect(mockPost).not.toHaveBeenCalledWith('/api/chat?ws=1', expect.anything())
    })

    it('transitions to error on create failure', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })

      mockPost.mockRejectedValue(new Error('Create failed'))

      await act(async () => {
        try {
          await result.current.createSession()
        } catch {
          // mutateAsync throws on rejection — expected
        }
      })

      await waitFor(() => {
        expect(result.current.status).toBe('error')
      })
      expect(result.current.error).toBe('Create failed')
      expect(result.current.creating).toBe(false)
    })

    it('tolerates seed send failure (SSE parse error)', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })

      mockPost.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve({ key: expectedSlotName })
        }
        if (path === '/api/chat?ws=1') {
          return Promise.reject(new Error('seed refused'))
        }
        if (path === '/api/chat/folders') {
          return Promise.resolve({ id: 'f1', name: 'Workbench' })
        }
        return Promise.resolve({})
      })
      // After creation, the query will be invalidated and re-fetched — return the slot
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, title: 'TestLabel', messages: 0, running: true },
          ])
        }
        if (path === '/api/chat/folders') return Promise.resolve([{ id: 'f1', name: 'Workbench' }])
        return Promise.resolve({})
      })
      mockPatch.mockResolvedValue({})

      await act(async () => {
        await result.current.createSession()
      })

      // Should still be ready despite seed send failure
      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })
      expect(result.current.slotKey).toBe(expectedSlotName)
    })
  })

  describe('openChat', () => {
    // One host navigation, so one history entry: a single Back returns to the app.
    it('navigates once, straight to /chat?sid=', async () => {
      mockGet.mockResolvedValue([])

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })

      vi.useFakeTimers()
      mockNavigate.mockClear()

      act(() => {
        result.current.openChat()
      })
      act(() => {
        vi.runAllTimers()
      })

      expect(mockNavigate.mock.calls).toEqual([
        ['/chat?sid=' + encodeURIComponent(expectedSlotName)],
      ])

      vi.useRealTimers()
    })
  })

  describe('resetSession', () => {
    it('clears state back to no-session', async () => {
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, title: 'Existing', messages: 5, running: true },
          ])
        }
        if (path === '/api/chat/folders') return Promise.resolve([])
        return Promise.resolve({})
      })
      mockPost.mockResolvedValue({ id: 'f1', name: 'Workbench' })
      mockPatch.mockResolvedValue({})

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })
      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })

      // After reset, the query will be removed and re-fetched — return empty so it goes to no-session
      mockGet.mockResolvedValue([])

      act(() => {
        result.current.resetSession()
      })

      await waitFor(() => {
        expect(result.current.status).toBe('no-session')
      })
      expect(result.current.slotKey).toBeNull()
      expect(result.current.slotInfo).toBeNull()
      expect(result.current.error).toBeNull()
    })
  })

  describe('slot naming', () => {
    it('generates slot name from appName and workspace hash', async () => {
      const customSlotName = 'myapp-' + hashStr('/custom/path')
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: customSlotName, title: 'Custom', messages: 0, running: false },
          ])
        }
        if (path === '/api/chat/folders') return Promise.resolve([])
        return Promise.resolve({})
      })
      mockPost.mockResolvedValue({ id: 'f1', name: 'Myapp' })
      mockPatch.mockResolvedValue({})

      const { result } = renderHook(() =>
        useChatSession({
          workspacePath: '/custom/path',
          label: 'Custom',
          appName: 'myapp',
        }),
        { wrapper: createWrapper() },
      )

      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })
      expect(result.current.slotKey).toBe(customSlotName)
    })
  })

  describe('folder assignment', () => {
    it('creates folder when it does not exist', async () => {
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, title: 'Test', messages: 0, running: false },
          ])
        }
        if (path === '/api/chat/folders') return Promise.resolve([])
        return Promise.resolve({})
      })
      mockPost.mockResolvedValue({ id: 'new-folder', name: 'Workbench' })
      mockPatch.mockResolvedValue({})

      renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })

      await waitFor(() => {
        expect(mockPost).toHaveBeenCalledWith('/api/chat/folders', { name: 'Workbench' })
      })

      await waitFor(() => {
        expect(mockPatch).toHaveBeenCalledWith(
          '/api/chat/slots/' + encodeURIComponent(expectedSlotName) + '/folder',
          { folder_id: 'new-folder' }
        )
      })
    })

    it('tolerates folder assignment failure gracefully', async () => {
      mockGet.mockImplementation((path: string) => {
        if (path === '/api/chat/slots') {
          return Promise.resolve([
            { key: expectedSlotName, title: 'Test', messages: 0, running: false },
          ])
        }
        if (path === '/api/chat/folders') return Promise.reject(new Error('Folder API down'))
        return Promise.resolve({})
      })

      const { result } = renderHook(() => useChatSession(defaultOpts), { wrapper: createWrapper() })

      await waitFor(() => {
        expect(result.current.status).toBe('ready')
      })
      expect(result.current.slotKey).toBe(expectedSlotName)
    })
  })
})
