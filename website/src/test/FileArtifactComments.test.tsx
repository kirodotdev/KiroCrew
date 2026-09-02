import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor, act } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useFileArtifactComments } from '../components/FileArtifactComments'
import { api } from '../api/client'
import type { ArtifactComment } from '../types'

vi.mock('../api/client')

function refs() {
  return { previewRef: { current: null } as any, scrollRef: { current: null } as any }
}

function mk(id: string): ArtifactComment {
  return {
    id, origin: 'local', scope: 'private', author: 'alex', is_agent: false,
    body: 'x', thread_id: id, status: 'open', sync_state: 'local_only',
    created_at: '', updated_at: '',
  }
}

describe('useFileArtifactComments', () => {
  beforeEach(() => vi.clearAllMocks())

  it('is inert when slug is null', () => {
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: null, ...refs() }))
    expect(result.current.commentCount).toBe(0)
    expect(result.current.overlay).toBeNull()
    expect(result.current.sidebar).toBeNull()
    expect(result.current.popovers).toBeNull()
  })

  it('loads comments for a slug and reflects the count', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [mk('a'), mk('b')] } as any)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    await waitFor(() => expect(result.current.commentCount).toBe(2))
    expect(result.current.sidebar).not.toBeNull()
    expect(result.current.overlay).not.toBeNull()
  })

  it('toggles the sidebar open state', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [] } as any)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    expect(result.current.sidebarOpen).toBe(true)
    act(() => result.current.toggleSidebar())
    expect(result.current.sidebarOpen).toBe(false)
  })

  it('requestAnchoredComment is a safe no-op without a selection', async () => {
    vi.mocked(api.artifactComments).mockResolvedValue({ comments: [] } as any)
    const { result } = renderHookWithProviders(() => useFileArtifactComments({ slug: 'doc', ...refs() }))
    expect(() => act(() => result.current.requestAnchoredComment())).not.toThrow()
  })
})
