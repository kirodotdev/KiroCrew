// Customize no longer has a Connections tab. Its old links (`?tab=mcp`, with
// or without the MCP Servers sub-tab) are replaced with the pages they became,
// before the tab layout mounts. The mapping itself is pinned in
// McpManagementMove.test.tsx; this pins the wiring.
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <p>crews pane</p> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))

import CapabilitiesPage from '../pages/CapabilitiesPage'

function Where() {
  const loc = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output data-testid="where">{loc.pathname + loc.search + loc.hash}</output>
      <button type="button" onClick={() => navigate(-1)}>back</button>
    </>
  )
}

function mount(entries: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route path="/capabilities" element={<CapabilitiesPage />} />
          <Route path="*" element={null} />
        </Routes>
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const where = () => screen.getByTestId('where').textContent

describe('old Customize Connections links', () => {
  it('replace the MCP Servers sub-tab with the MCP Servers page', async () => {
    mount(['/before', '/capabilities?tab=mcp&view=mcp-servers#mcp-management'])
    await waitFor(() => expect(where()).toBe('/mcp?view=sharing'))
    fireEvent.click(screen.getByRole('button', { name: 'back' }))
    await waitFor(() => expect(where()).toBe('/before'))
  })

  it('replace the Connections tab with Settings → Connections', async () => {
    mount(['/capabilities?tab=mcp&provider=github'])
    await waitFor(() => expect(where()).toBe('/settings/connections?provider=github'))
  })

  it('leave every other tab alone', async () => {
    mount(['/capabilities?tab=crews'])
    expect(await screen.findByText('crews pane')).toBeInTheDocument()
    expect(where()).toBe('/capabilities?tab=crews')
  })
})
