import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'

// Commands distinct from the component's FALLBACK set, so findByText waits for
// the resolved query (not the transient FALLBACK render) before we navigate.
// Include /kb so FRONTEND_COMMANDS adds nothing extra → list is exactly these.
const CMDS = [
  { name: '/aa', description: 'Alpha command' },
  { name: '/bb', description: 'Beta command' },
  { name: '/cc', description: 'Gamma command' },
  { name: '/kb', description: 'Search knowledge library' },
]

function Harness({ input, onSelect = vi.fn(), onClose = vi.fn() }: {
  input: string; onSelect?: (c: string) => void; onClose?: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={onClose} />
      </div>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.slashCommands.mockResolvedValue(CMDS)
})

describe('SlashCommandMenu (shared-hook migration)', () => {
  it('renders commands when input is a bare slash', async () => {
    render(<Harness input="/" />)
    expect(await screen.findByText('/aa')).toBeInTheDocument()
    expect(screen.getByText('/bb')).toBeInTheDocument()
    expect(screen.getByText('/cc')).toBeInTheDocument()
  })

  it('renders each command description from the API', async () => {
    render(<Harness input="/" />)
    // Wait for the resolved query, then assert the description column renders.
    expect(await screen.findByText('Alpha command')).toBeInTheDocument()
    expect(screen.getByText('Beta command')).toBeInTheDocument()
    expect(screen.getByText('Gamma command')).toBeInTheDocument()
  })

  it('filters by name prefix', async () => {
    render(<Harness input="/b" />)
    expect(await screen.findByText('/bb')).toBeInTheDocument()
    expect(screen.queryByText('/aa')).not.toBeInTheDocument()
    expect(screen.queryByText('/cc')).not.toBeInTheDocument()
  })

  it('Enter selects the highlighted command (index 0, appends a space)', async () => {
    const onSelect = vi.fn()
    render(<Harness input="/" onSelect={onSelect} />)
    await screen.findByText('/aa')
    // jsdom rects are zero → opens "below" → alphabetical first (/aa) at top.
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/aa ')
  })

  it('ArrowDown then Enter selects the next command', async () => {
    const onSelect = vi.fn()
    render(<Harness input="/" onSelect={onSelect} />)
    await screen.findByText('/bb')
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/bb ')
  })

  it('Escape closes the menu', async () => {
    const onClose = vi.fn()
    render(<Harness input="/" onClose={onClose} />)
    await screen.findByText('/aa')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})
