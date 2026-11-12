import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'

// Served command names can hold a hyphen (/security-review, /list-dirs) or a
// digit. The menu keeps filtering while the typed name has one, instead of
// closing the moment the hyphen is typed.
const CMDS = [
  { name: '/review', description: 'Review code changes' },
  { name: '/security-review', description: 'Run a security review' },
  { name: '/list-dirs', description: 'List directories' },
]

function Harness({ input, onSelect = vi.fn() }: { input: string; onSelect?: (c: string) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={vi.fn()} />
      </div>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.slashCommands.mockResolvedValue(CMDS)
})

describe('SlashCommandMenu: names with a hyphen', () => {
  it('stays open and keeps filtering after the hyphen is typed', async () => {
    render(<Harness input="/security-" />)
    expect(await screen.findByText('/security-review')).toBeInTheDocument()
    expect(screen.queryByText('/review')).not.toBeInTheDocument()
  })

  it('picks the hyphenated command with Enter', async () => {
    const onSelect = vi.fn()
    render(<Harness input="/list-d" onSelect={onSelect} />)
    expect(await screen.findByText('/list-dirs')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/list-dirs ')
  })

  it('closes once a space follows the name', async () => {
    const { rerender } = render(<Harness input="/security-review" />)
    expect(await screen.findByText('/security-review')).toBeInTheDocument()
    rerender(<Harness input="/security-review " />)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})
