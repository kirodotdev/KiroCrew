import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef, useState } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn(), workflowDefinitions: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'

function Harness({ input, qc }: { input: string; qc: QueryClient }) {
  const ref = useRef<HTMLDivElement>(null)
  const [onSelect] = useState(() => vi.fn())
  const [onClose] = useState(() => vi.fn())
  return (
    <QueryClientProvider client={qc}>
      <div ref={ref}>anchor</div>
      <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={onClose} />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mockApi.slashCommands.mockReset()
  mockApi.workflowDefinitions.mockReset()
})

describe('SlashCommandMenu: reopening the menu re-reads the command list', () => {
  it('shows a saved prompt that opted in after the page loaded', async () => {
    // The app-wide default: data never goes stale on its own.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    mockApi.slashCommands.mockResolvedValueOnce([{ name: '/compact', description: 'Compact' }])
    const { rerender } = render(<Harness input="/" qc={qc} />)
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    expect(screen.queryByText('/standup')).not.toBeInTheDocument()

    mockApi.slashCommands.mockResolvedValue([
      { name: '/compact', description: 'Compact' },
      { name: '/standup', description: 'My saved standup prompt' },
    ])
    rerender(<Harness input="hello" qc={qc} />)
    rerender(<Harness input="/" qc={qc} />)

    expect(await screen.findByText('/standup')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalledTimes(2))
  })

  it('names a failed re-read above the cached rows, which stay pickable', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    mockApi.slashCommands.mockResolvedValueOnce([{ name: '/compact', description: 'Compact' }])
    const { rerender } = render(<Harness input="/" qc={qc} />)
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    mockApi.slashCommands.mockRejectedValue(new Error('offline'))
    rerender(<Harness input="hello" qc={qc} />)
    rerender(<Harness input="/" qc={qc} />)

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('/compact')).toBeInTheDocument()
  })

  it('does not re-read while the menu stays open', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    mockApi.slashCommands.mockResolvedValue([{ name: '/compact', description: 'Compact' }])
    const { rerender } = render(<Harness input="/" qc={qc} />)
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    rerender(<Harness input="/c" qc={qc} />)
    rerender(<Harness input="/co" qc={qc} />)
    expect(mockApi.slashCommands).toHaveBeenCalledTimes(1)
  })
})
