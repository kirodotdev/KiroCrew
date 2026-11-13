// Customize → MCP Servers reads `?view=` (Sharing), as Skills does (budget).
// Both tabs declare it as their drill-in, so a rail pick opens a tab at its
// root: leaving the Sharing view and coming back lands on Servers, not Sharing.
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/McpPage', () => ({ default: () => <p>mcp pane</p> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <p>skills pane</p>,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))

import CapabilitiesPage from '../pages/CapabilitiesPage'

function Where() {
  const loc = useLocation()
  return <output data-testid="where">{loc.pathname + loc.search}</output>
}

function mount(entry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route path="/capabilities" element={<CapabilitiesPage />} />
        </Routes>
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const where = () => screen.getByTestId('where').textContent
const railButton = (name: RegExp) => screen.getAllByRole('button', { name }).at(0)!

describe('Customize → MCP Servers ?view=', () => {
  it('is dropped when the rail leaves the Sharing view, so coming back opens Servers', async () => {
    mount('/capabilities?tab=mcp-servers&view=sharing')
    expect(await screen.findByText('mcp pane')).toBeInTheDocument()
    fireEvent.click(railButton(/^Skills/))
    await waitFor(() => expect(where()).toBe('/capabilities?tab=skills'))
    fireEvent.click(railButton(/^MCP Servers/))
    await waitFor(() => expect(where()).toBe('/capabilities?tab=mcp-servers'))
  })

  it('is dropped when the rail leaves Skills\u2019 budget view', async () => {
    mount('/capabilities?tab=skills&view=budget')
    expect(await screen.findByText('skills pane')).toBeInTheDocument()
    fireEvent.click(railButton(/^MCP Servers/))
    await waitFor(() => expect(where()).toBe('/capabilities?tab=mcp-servers'))
  })
})
