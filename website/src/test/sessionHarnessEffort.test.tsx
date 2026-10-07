/**
 * A session's effort control offers what its harness takes before the session reports.
 *
 * Until then the composer judged the session by the model name its chip showed, so a
 * crew on Claude Code whose chip read `auto` got no control, and a codex session whose
 * build takes no effort got one. The server now answers for the session's own harness.
 */
import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn().mockResolvedValue({}),
    projectGit: vi.fn(),
    updateKirocrewAgent: vi.fn(),
  },
}))

import { useComposerChips } from '../pages/chat/page/composerChips'
import type { ChatSlot } from '../types'

type Caps = { known: boolean; effort_supported?: boolean | null; effort_levels?: string[] }

function chips(caps: Caps, model = '') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  return renderHook(() => useComposerChips({
    currentSlot: { key: 'slot-a', agent: 'writer', model } as ChatSlot,
    defaultAgent: 'default',
    pendingAgent: '',
    installedAgents: [],
    provider: { id: 'acp', capabilities: { reasoningEffort: true }, resolveModel: vi.fn().mockResolvedValue(''), resolveDefaultEffort: vi.fn().mockResolvedValue('') } as never,
    availableModels: [{ name: 'auto', description: '' }, { name: 'claude-opus-5-5', description: '' }, { name: 'openai.gpt-6.1-sol', description: '' }],
    codexPairModels: false,
    selectionCapabilities: undefined,
    selectionCapabilitiesQ: { isError: false, data: caps },
    remoteCrew: { isRemote: false } as never,
    dispatch: vi.fn() as never,
    queryClient: qc,
    showActionError: vi.fn(),
  }), { wrapper })
}

describe('useComposerChips — effort before the session reports', () => {
  it('offers the levels the session\'s harness takes while its chip still reads auto', () => {
    const { result } = chips({ known: false, effort_supported: true, effort_levels: ['low', 'high'] })

    expect(result.current.effortSupported).toBe(true)
    expect(result.current.effortLevelsOverride).toEqual(['low', 'high'])
  })

  it('judges the model where no session on the harness has answered yet, as before a session reports', () => {
    // A cold codex thread: unknown is not "takes none", so its pick is not lost before its first turn.
    const { result } = chips({ known: false, effort_supported: null, effort_levels: [] }, 'openai.gpt-6.1-sol')

    expect(result.current.effortSupported).toBe(true)
    expect(result.current.effortLevelsOverride).toBeUndefined()
  })

  it('offers no control where the harness takes no effort, whatever the model name says', () => {
    const { result } = chips({ known: false, effort_supported: false, effort_levels: [] }, 'claude-opus-5-5')

    expect(result.current.effortSupported).toBe(false)
  })
})
