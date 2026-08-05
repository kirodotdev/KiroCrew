import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'

// sigma renders via WebGL, which jsdom doesn't provide. Mock Sigma so the
// component's UI shell (filters, search, counts) can be tested headlessly.
// graphology is pure JS (no WebGL) so it does not need mocking.
vi.mock('sigma', () => {
  const MockSigma = vi.fn().mockImplementation(() => ({
    on: vi.fn(),
    refresh: vi.fn(),
    kill: vi.fn(),
  }))
  return { default: MockSigma }
})

import MemoryGraphTab from '../src/pages/overview/MemoryGraphTab'

describe('MemoryGraphTab Integration Tests', () => {
  it('renders graph container with nodes after loading', async () => {
    renderWithProviders(<MemoryGraphTab />)

    // Should show node count in filter buttons
    await waitFor(() => {
      expect(screen.getByText(/All \(7\)/)).toBeInTheDocument()
    })
  })

  it('displays filter buttons with correct group counts', async () => {
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/Preferences \(2\)/)).toBeInTheDocument()
    })
    expect(screen.getByText(/Projects \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Semantic \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/Lessons \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/History \(1\)/)).toBeInTheDocument()
  })

  it('filters nodes when clicking a group button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/All \(7\)/)).toBeInTheDocument()
    })

    await user.click(screen.getByText(/Projects \(2\)/))
    // Active filter button gets accent styling
    expect(screen.getByText(/Projects \(2\)/).className).toContain('!border-accent')
  })

  it('toggles filter off when clicking same group again', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/All \(7\)/)).toBeInTheDocument()
    })

    const btn = screen.getByText(/Lessons \(1\)/)
    await user.click(btn)
    expect(btn.className).toContain('!border-accent')
    await user.click(btn)
    expect(btn.className).not.toContain('!border-accent')
  })

  it('has a working search input', async () => {
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Search nodes…')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByPlaceholderText('Search nodes…'), { target: { value: 'typescript' } })
    expect(screen.getByPlaceholderText('Search nodes…')).toHaveValue('typescript')
  })

  it('shows empty state when API returns no data', async () => {
    server.use(
      http.get('/api/memory/graph', () => HttpResponse.json({ nodes: [], edges: [] }))
    )
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/No memory data to visualize/)).toBeInTheDocument()
    })
  })

  it('shows loading state initially', async () => {
    server.use(
      http.get('/api/memory/graph', () => new Promise(() => {})) // never resolves
    )
    renderWithProviders(<MemoryGraphTab />)
    expect(screen.getByText(/Loading graph data/)).toBeInTheDocument()
  })

  it('handles API error gracefully', async () => {
    server.use(
      http.get('/api/memory/graph', () => HttpResponse.error())
    )
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/No memory data to visualize/)).toBeInTheDocument()
    })
  })

  it('refresh button reloads data', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/Refresh/)).toBeInTheDocument()
    })

    await user.click(screen.getByText(/Refresh/))
    // After refresh, data should still be visible
    await waitFor(() => {
      expect(screen.getByText(/All \(7\)/)).toBeInTheDocument()
    })
  })

  it('renders the graph title and info tooltip', async () => {
    renderWithProviders(<MemoryGraphTab />)

    await waitFor(() => {
      expect(screen.getByText(/Memory Graph/)).toBeInTheDocument()
    })
  })
})
