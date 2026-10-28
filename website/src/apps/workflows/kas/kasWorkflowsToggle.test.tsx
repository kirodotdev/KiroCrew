/**
 * The single gateway opt-in that replaced the per-device preview flag and the
 * separate chat-workflows toggle.
 *
 * Two things under test: the Feature Previews toggle reads `agent.kas_workflows`
 * from the engine probe and writes it through config (never localStorage), and
 * `useKasWorkflowsEnabled` gates purely on the gateway's engine answer with no
 * per-device flag.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { api } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { FeaturePreviewsSection } from '../../../pages/settings/FeaturePreviewsSection'
import { useKasWorkflowsEnabled } from './useKasWorkflows'

type Engine = Awaited<ReturnType<typeof api.workflowEngine>>

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

const stubCard = (engine: Partial<Engine>) => {
  // The other cards' reads, stubbed so only the KAS card is under test.
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: false } as never)
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
  vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
    enabled: false, endpoint: '', configured_endpoint: '', permits: false,
  } as never)
  vi.spyOn(api, 'workflowEngine').mockResolvedValue(engine as Engine)
  return vi.spyOn(api, 'patchConfig').mockResolvedValue({ ok: true } as never)
}

const renderSection = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
    </QueryClientProvider>,
  )

const kasSwitch = () =>
  screen.getByRole('switch', { name: /use kiro workflows when kiro agent server is configured/i })

describe('Kiro Workflows on Kiro Agent — the single Feature Previews toggle', () => {
  it('reflects the stored agent.kas_workflows value from the engine probe', async () => {
    stubCard({ engine: 'kas', backend: 'kas', kas_backend: true, kas_workflows: true })
    renderSection()
    await waitFor(() => expect(kasSwitch().getAttribute('aria-checked')).toBe('true'))
  })

  it('writes agent.kas_workflows (not localStorage) when toggled', async () => {
    const patch = stubCard({ engine: 'crew', backend: 'kas', kas_backend: true, kas_workflows: false })
    renderSection()
    await waitFor(() => expect(kasSwitch().getAttribute('aria-checked')).toBe('false'))
    kasSwitch().click()
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.kas_workflows', true))
    // The opt-in is gateway config, so nothing is written to localStorage.
    expect(localStorage.getItem('mc-preview-kas-workflows')).toBeNull()
  })

  it('says it has no effect until the backend is Kiro Agent', async () => {
    stubCard({ engine: 'crew', backend: 'builtin', kas_backend: false, kas_workflows: false })
    renderSection()
    await waitFor(() =>
      expect(
        screen.getByText(new RegExp(i18nT('pages.developer.featurePreviewsTab.kas_workflows_off_backend'), 'i')),
      ).toBeInTheDocument(),
    )
  })

  it('stays usable off a Kiro Agent backend', async () => {
    stubCard({ engine: 'crew', backend: 'builtin', kas_backend: false, kas_workflows: false })
    renderSection()
    await waitFor(() => expect(api.workflowEngine).toHaveBeenCalled())
    expect(kasSwitch()).not.toHaveAttribute('aria-disabled', 'true')
  })
  it('says open chats pick up the change on their next session (gap 28)', () => {
    expect(i18nT('pages.developer.featurePreviewsTab.kas_workflows_desc')).toMatch(/next session/i)
  })
})

describe('useKasWorkflowsEnabled — gateway answer, no localStorage', () => {
  const wrap = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  )

  it('enables on engine=kas and never reads a preview flag from storage', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas', kas_backend: true, kas_workflows: true } as Engine)
    const { result } = renderHook(() => useKasWorkflowsEnabled(), { wrapper: wrap })
    await waitFor(() => expect(result.current.enabled).toBe(true))
    expect(getItem.mock.calls.some(c => String(c[0]).includes('kas-workflows'))).toBe(false)
  })

  it('stays disabled on any non-kas engine', async () => {
    vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'crew', backend: '', kas_backend: false, kas_workflows: false } as Engine)
    const { result } = renderHook(() => useKasWorkflowsEnabled(), { wrapper: wrap })
    await waitFor(() => expect(api.workflowEngine).toHaveBeenCalled())
    expect(result.current.enabled).toBe(false)
  })
})
