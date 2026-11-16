import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'

// A chat's own claude session advertises an argument hint (`input.hint`) on some
// of its commands. The menu shows it as text beside the name and never types it in.
const INJECTED = '<img src=x onerror=alert(1)>'
const CMDS = [
  { name: '/loop', description: 'Run on an interval', hint: '[interval] [prompt]' },
  { name: '/lint', description: 'Lint the project' },
  { name: '/lurk', description: 'Watch quietly', hint: INJECTED },
]

function Harness({ onSelect = vi.fn() }: { onSelect?: (c: string) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div ref={ref}>anchor</div>
      <SlashCommandMenu input="/l" anchorRef={ref} onSelect={onSelect} onClose={vi.fn()} slotId="s1" />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mockApi.slashCommands.mockReset()
  mockApi.slashCommands.mockResolvedValue(CMDS)
})

describe('SlashCommandMenu: an advertised argument hint', () => {
  it('shows the hint beside its command and nothing beside one without', async () => {
    render(<Harness />)
    const hint = await screen.findByText('[interval] [prompt]')
    expect(hint.closest('[role="option"]')).toHaveTextContent('/loop')
    expect(hint).toHaveAttribute('title', '[interval] [prompt]')

    const lint = (await screen.findByText('/lint')).closest('[role="option"]')!
    expect(lint.querySelectorAll('span')).toHaveLength(2)
  })

  it('renders a hint as literal text', async () => {
    render(<Harness />)
    expect(await screen.findByText(INJECTED)).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })

  it('inserts only the command name when the hinted row is picked', async () => {
    const onSelect = vi.fn()
    render(<Harness onSelect={onSelect} />)
    const row = (await screen.findByText('[interval] [prompt]')).closest('[role="option"]')!
    fireEvent.mouseDown(row)
    expect(onSelect).toHaveBeenCalledWith('/loop ')
  })
})
