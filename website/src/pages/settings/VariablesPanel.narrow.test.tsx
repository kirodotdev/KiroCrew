/**
 * Narrow-layout coverage for Settings > Environment Variables.
 *
 * The repo's narrow-viewport rule is blocking and explicitly rejects hiding a
 * surface below a breakpoint: the table is the only host of the edit and delete
 * actions, so a phone must keep both. These assert the stacked branch renders the
 * same capability, not merely that it renders.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const isMobile = vi.fn(() => false)
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => isMobile() }))

vi.mock('../../api/client', () => ({
  api: {
    variables: vi.fn(async () => ({
      global: { BASE_URL: 'https://api.dev.test', ORG: 'Acme' },
      workspaces: { ops: { QUEUE: 'oncall' } },
      effective: { BASE_URL: 'https://api.dev.test', ORG: 'Acme', QUEUE: 'oncall' },
      winning_scope: { BASE_URL: 'global', ORG: 'global', QUEUE: 'workspace' },
    })),
    saveVariables: vi.fn(async () => ({ ok: true })),
  },
}))

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { VariablesPanel } from './VariablesPanel'

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <VariablesPanel />
    </QueryClientProvider>,
  )
}

describe('VariablesPanel narrow layout', () => {
  beforeEach(() => {
    isMobile.mockReturnValue(false)
    vi.clearAllMocks()
  })

  it('renders the four-column table on a desktop width', async () => {
    renderPanel()
    expect(await screen.findAllByRole('columnheader')).not.toHaveLength(0)
  })

  it('drops the table in favour of stacked rows on a phone', async () => {
    isMobile.mockReturnValue(true)
    renderPanel()
    await screen.findByText('BASE_URL')
    // No column headers means no four-column table competing for 320px.
    expect(screen.queryAllByRole('columnheader')).toHaveLength(0)
  })

  it('keeps every variable visible when stacked', async () => {
    isMobile.mockReturnValue(true)
    renderPanel()
    expect(await screen.findByText('BASE_URL')).toBeInTheDocument()
    expect(screen.getByText('ORG')).toBeInTheDocument()
  })

  it('keeps the delete action on a phone — hiding it would remove the capability', async () => {
    isMobile.mockReturnValue(true)
    renderPanel()
    await screen.findByText('BASE_URL')
    expect(screen.getByRole('button', { name: /BASE_URL/ })).toBeInTheDocument()
  })

  it('keeps every value editable, with a real label association', async () => {
    isMobile.mockReturnValue(true)
    renderPanel()
    await screen.findByText('BASE_URL')
    // Two global pairs plus the add row, each with a labelled Value field.
    const values = screen.getAllByLabelText('Value')
    expect(values.length).toBeGreaterThanOrEqual(2)
    expect(values[0]).toBeEnabled()
  })

  it('still shows which scope won on a phone', async () => {
    isMobile.mockReturnValue(true)
    renderPanel()
    await screen.findByText('BASE_URL')
    expect(screen.getAllByText('Global').length).toBeGreaterThan(0)
  })
})
