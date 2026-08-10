/**
 * The tab rail of Agent Capabilities has to follow the active language.
 *
 * Its labels come from `i18nT`, which reads the active language at call time but
 * does not subscribe to changes, and they are built inside a `useMemo`. Keyed on
 * the provider alone, that memo pins whichever language it first computed: the
 * page title re-evaluated on the next render while the seven tab labels stayed
 * in the old language, so the whole rail rendered raw English under every other
 * locale. A catalog assertion cannot see this — the keys and their translations
 * are all present — so this mounts the page and switches the language under it.
 */

import { describe, it, expect, vi, afterAll } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

// The tab bodies are irrelevant here and each pulls its own fetches; the rail is
// what is under test.
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/AgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))
vi.mock('../components/RestartButton', () => ({ default: () => <div /> }))

import CapabilitiesPage from '../pages/CapabilitiesPage'
import { i18next } from '../i18n/index'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/capabilities']}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('CapabilitiesPage — the tab rail follows the active language', () => {
  it('relabels the tabs when the language changes under a mounted page', async () => {
    await i18next.changeLanguage('en')
    wrap(<CapabilitiesPage />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Agents' })).toBeTruthy(),
    )

    await i18next.changeLanguage('ja')

    // The Japanese label for the same tab. Without the language in the memo's
    // dependencies the rail keeps rendering "Agents" here.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'エージェント' })).toBeTruthy(),
    )
    expect(screen.queryByRole('button', { name: 'Agents' })).toBeNull()
  })
})
