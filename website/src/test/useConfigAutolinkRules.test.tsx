/**
 * Regression for #15047: Text Link Patterns must apply on EVERY surface, not
 * only after a chat page has rendered. The autolink registry
 * (`dashboard.link_patterns`) is written by the app shell's
 * `useConfigAutolinkRules`, which reads the ['dashboardConfig'] query — so a
 * `MarkdownRenderer` on an app page linkifies even when no chat page mounted.
 *
 * The existing MarkdownRenderer.linkPatterns suite pre-registers rules in a
 * helper, which is exactly why it never caught the missing shell registration.
 * This test deliberately does NOT call setConfigAutolinkRules itself: the only
 * thing that populates the registry here is the shell hook running its query.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { createElement } from 'react'
import { renderHook, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { useConfigAutolinkRules } from '../hooks/useConfigAutolinkRules'
import { getAutolinkRules, resetAutolinkRulesForTest } from '../utils/autolinkRules'

const dashboardConfig = vi.fn()
vi.mock('../api/client', () => ({
  api: { dashboardConfig: () => dashboardConfig() },
}))

afterEach(() => {
  resetAutolinkRulesForTest()
  dashboardConfig.mockReset()
})

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children)
}

describe('useConfigAutolinkRules (shell-owned link_patterns registration)', () => {
  it('registers config rules from the shell so a fresh MarkdownRenderer linkifies', async () => {
    dashboardConfig.mockResolvedValue({
      link_patterns: [{ pattern: '\\bPROJ-\\d+\\b', url: 'https://tracker.example.com/browse/{match}' }],
    })

    // No prior chat page, no manual setConfigAutolinkRules: the registry is
    // empty until the shell hook runs its query.
    expect(getAutolinkRules()).toHaveLength(0)

    renderHook(() => useConfigAutolinkRules(), { wrapper: makeWrapper() })

    // The shell hook resolves the query and registers the rule. Wait on the
    // registry itself — not on a re-render — so the assertion sees the shell's
    // write, with no chat page ever mounted.
    await waitFor(() => expect(getAutolinkRules()).toHaveLength(1))

    // A MarkdownRenderer rendered AFTER the shell registered now linkifies.
    render(<MarkdownRenderer content="see PROJ-123 for details" />)
    const link = screen.getByRole('link')
    expect(link.textContent).toBe('PROJ-123')
    expect(link).toHaveAttribute('href', 'https://tracker.example.com/browse/PROJ-123')
  })

  it('registers an empty rule set when the config has no link_patterns', async () => {
    dashboardConfig.mockResolvedValue({})
    renderHook(() => useConfigAutolinkRules(), { wrapper: makeWrapper() })
    await waitFor(() => expect(dashboardConfig).toHaveBeenCalled())

    render(<MarkdownRenderer content="see PROJ-123 for details" />)
    expect(screen.queryByRole('link')).toBeNull()
  })
})
