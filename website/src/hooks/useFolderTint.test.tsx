import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useFolderTint } from './useFolderTint'
import { api } from '../api/client'
import { folderPaneTint } from '../components/folderColorPaint'
import type { ChatFolder } from '../types'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function wrapperWith(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })
const folders: ChatFolder[] = [
  { id: 'proj', name: 'Project', order: 0, color: '#ef4444' },
  { id: 'sub', name: 'Sub', order: 1, parent_id: 'proj' },
  { id: 'plain', name: 'Plain', order: 2 },
]

describe('useFolderTint', () => {
  it('tints a session filed in a colored folder, inheriting from an ancestor', async () => {
    vi.spyOn(api, 'chatFolders').mockResolvedValue(folders)
    const { result } = renderHook(() => useFolderTint('sub'), { wrapper: wrapperWith(newClient()) })
    await waitFor(() => expect(result.current).not.toBeNull())
    expect(result.current).toEqual({ color: '#ef4444', style: { background: folderPaneTint('#ef4444') } })
  })

  it('does not tint an uncolored folder', async () => {
    const spy = vi.spyOn(api, 'chatFolders').mockResolvedValue(folders)
    const { result } = renderHook(() => useFolderTint('plain'), { wrapper: wrapperWith(newClient()) })
    await waitFor(() => expect(spy).toHaveBeenCalled())
    expect(result.current).toBeNull()
  })

  it('asks for nothing while the session is unfiled', () => {
    const spy = vi.spyOn(api, 'chatFolders').mockResolvedValue(folders)
    const { result } = renderHook(() => useFolderTint(undefined), { wrapper: wrapperWith(newClient()) })
    expect(result.current).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('reads the sidebar\'s cached folder list without a request of its own', () => {
    const client = newClient()
    client.setQueryData(['chat-folders'], folders)
    const spy = vi.spyOn(api, 'chatFolders').mockResolvedValue(folders)
    const { result } = renderHook(() => useFolderTint('proj'), { wrapper: wrapperWith(client) })
    expect(result.current?.color).toBe('#ef4444')
    expect(spy).not.toHaveBeenCalled()
  })

  it('redefines --bg for the pane subtree against the theme\'s resolved background', () => {
    document.documentElement.style.setProperty('--bg', '#fafafa')
    try {
      const client = newClient()
      client.setQueryData(['chat-folders'], folders)
      const { result } = renderHook(() => useFolderTint('proj'), { wrapper: wrapperWith(client) })
      expect(result.current?.style).toEqual({
        '--bg': folderPaneTint('#ef4444', '#fafafa'),
        background: 'var(--bg)',
      })
      // A pane that redefines --bg cannot mix against the variable it is defining.
      expect(String(result.current?.style['--bg' as keyof typeof result.current.style])).not.toContain('var(--bg)')
    } finally {
      document.documentElement.style.removeProperty('--bg')
    }
  })

  it('stays untinted when the folder payload is not a list', () => {
    const client = newClient()
    client.setQueryData(['chat-folders'], { error: 'nope' })
    const { result } = renderHook(() => useFolderTint('proj'), { wrapper: wrapperWith(client) })
    expect(result.current).toBeNull()
  })
})
