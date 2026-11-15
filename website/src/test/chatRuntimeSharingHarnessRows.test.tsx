/**
 * ACP runtime sharing's per-harness switches: shown only under the preview's own
 * switch while it is on, each reading and writing
 * `agent.chat_runtime_sharing_harnesses.<name>`, KAS on when nothing is stored.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../api/client'
import { __resetPreviewFlagServerSyncForTests } from '../hooks/usePreviewFlagServerSync'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_CHAT_RUNTIME_SHARING, setPreviewFlag } from '../utils/previewFlags'

function renderSection() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })}>
      <MemoryRouter initialEntries={['/settings']}>
        <FeaturePreviewsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function mockConfig(harnesses?: Record<string, unknown>) {
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({
    dashboard: { feature_previews: {} },
    agent: harnesses === undefined ? {} : { chat_runtime_sharing_harnesses: harnesses },
  } as never)
}

const kasSwitch = () => screen.getByRole('switch', { name: /Chats using Kiro Agent/i })
const codexSwitch = () => screen.getByRole('switch', { name: /Chats using OpenAI Codex/i })
const claudeSwitch = () => screen.getByRole('switch', { name: /Chats using Claude Code/i })
const gooseSwitch = () => screen.getByRole('switch', { name: /Chats using Goose/i })
const deepseekSwitch = () => screen.getByRole('switch', { name: /Chats using DeepSeek Harness/i })

beforeEach(() => {
  localStorage.clear()
  __resetPreviewFlagServerSyncForTests()
  vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
  vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
    enabled: false, endpoint: '', configured_endpoint: 'https://api.typesafe.ai/v1/systemone', permits: false,
  })
})

afterEach(() => {
  cleanup()
  act(() => { __resetPreviewFlagServerSyncForTests() })
  vi.restoreAllMocks()
})

describe('ACP runtime sharing per-harness switches', () => {
  it('are hidden while the preview is off', async () => {
    mockConfig()
    renderSection()
    await waitFor(() => expect(screen.getByRole('switch', { name: /ACP runtime sharing/i })).toBeTruthy())
    expect(screen.queryByTestId('chat-sharing-harnesses')).toBeNull()
  })

  it('show KAS on when nothing is stored, once the preview is on', async () => {
    mockConfig()
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(kasSwitch().getAttribute('aria-checked')).toBe('true'))
  })

  it('show the stored value', async () => {
    mockConfig({ kas: false })
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    await act(async () => {})
    await waitFor(() => expect(kasSwitch().getAttribute('aria-checked')).toBe('false'))
  })

  it('write the harness key on toggle', async () => {
    mockConfig({ kas: true })
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(kasSwitch().getAttribute('aria-checked')).toBe('true'))
    await act(async () => { kasSwitch().click() })
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.chat_runtime_sharing_harnesses.kas', false))
  })

  it('show a load error and an unchecked switch when the config read fails', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockRejectedValue(new Error('boom'))
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(screen.getByTestId('chat-sharing-harness-load-error')).toBeTruthy())
    expect(screen.getByTestId('chat-sharing-harness-load-error').textContent).toMatch(/Couldn't load these switches/)
    expect(kasSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('show codex off when nothing is stored, and write its own key', async () => {
    mockConfig()
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(codexSwitch().getAttribute('aria-checked')).toBe('false'))
    // Enabled once the config read answers.
    await waitFor(() => expect(codexSwitch().hasAttribute('disabled')).toBe(false))
    await act(async () => { codexSwitch().click() })
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.chat_runtime_sharing_harnesses.codex', true))
  })

  it('show claude off when nothing is stored, and write its own key', async () => {
    mockConfig()
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(claudeSwitch().getAttribute('aria-checked')).toBe('false'))
    // Enabled once the config read answers.
    await waitFor(() => expect(claudeSwitch().hasAttribute('disabled')).toBe(false))
    await act(async () => { claudeSwitch().click() })
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.chat_runtime_sharing_harnesses.claude', true))
  })

  it('show goose off when nothing is stored, and write its own key', async () => {
    mockConfig()
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(gooseSwitch().getAttribute('aria-checked')).toBe('false'))
    // Enabled once the config read answers.
    await waitFor(() => expect(gooseSwitch().hasAttribute('disabled')).toBe(false))
    await act(async () => { gooseSwitch().click() })
    await waitFor(() => expect(patch).toHaveBeenCalledWith('agent.chat_runtime_sharing_harnesses.goose', true))
  })

  it('show deepseek off when nothing is stored, and write its own key', async () => {
    mockConfig()
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({} as never)
    act(() => { setPreviewFlag(PREVIEW_CHAT_RUNTIME_SHARING, true) })
    renderSection()
    await waitFor(() => expect(deepseekSwitch().getAttribute('aria-checked')).toBe('false'))
    // Enabled once the config read answers.
    await waitFor(() => expect(deepseekSwitch().hasAttribute('disabled')).toBe(false))
    await act(async () => { deepseekSwitch().click() })
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith('agent.chat_runtime_sharing_harnesses.deepseek', true),
    )
  })
})
