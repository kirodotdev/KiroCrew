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
// Include /kb so the only extra FRONTEND_COMMANDS row is /plain — a quick prompt
// the backend never reports, asserted below.
const CMDS = [
  { name: '/aa', description: 'Alpha command' },
  { name: '/bb', description: 'Beta command' },
  { name: '/cc', description: 'Gamma command' },
  { name: '/kb', description: 'Search knowledge library' },
]

function Harness({ input, onSelect = vi.fn(), onClose = vi.fn(), sendOnEnter }: {
  input: string; onSelect?: (c: string) => void; onClose?: () => void; sendOnEnter?: 'enter' | 'ctrl-enter' | 'enter-ctrl-newline'
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={onClose} sendOnEnter={sendOnEnter} />
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

  // /plain is a quick prompt: a backend MACRO, so GET /api/slash-commands never
  // reports it. It reaches the menu only through FRONTEND_COMMANDS, which is the
  // one thing that makes it discoverable at all.
  it('offers /plain even though the API does not report it', async () => {
    render(<Harness input="/" />)
    expect(await screen.findByText('/aa')).toBeInTheDocument()
    expect(screen.getByText('/plain')).toBeInTheDocument()
  })

  it('renders each command description from the API', async () => {    render(<Harness input="/" />)
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

describe('SlashCommandMenu offline fallback (blocked commands hidden)', () => {
  // The fallback list mirrors the backend GET /api/slash-commands payload,
  // which excludes _BLOCKED_SLASH_COMMANDS. A blocked command in the fallback
  // would advertise a gesture the dashboard rejects (/tangent regressed this
  // way once), so pin its absence on the API-failure path where the fallback
  // is what the user actually sees.
  const BLOCKED = ['/tangent', '/quit', '/exit', '/q', '/chat', '/paste', '/reply', '/editor']

  it('renders no blocked command when the API query fails', async () => {
    mockApi.slashCommands.mockRejectedValue(new Error('offline'))
    render(<Harness input="/" />)
    // Fallback renders synchronously as the query default; anchor on a
    // known-good fallback command before asserting absences.
    expect(await screen.findByText('/compact')).toBeInTheDocument()
    for (const cmd of BLOCKED) {
      expect(screen.queryByText(cmd)).not.toBeInTheDocument()
    }
  })

  it('filtering to /tan renders the announcing empty state, never an inert /tangent', async () => {
    mockApi.slashCommands.mockRejectedValue(new Error('offline'))
    render(<Harness input="/tan" />)
    // Nothing in the fallback matches the /tan prefix. Once the query settles
    // (error counts as settled), the menu shows the zero-match empty state —
    // announcing that Enter now sends — rather than an inert /tangent row.
    expect(await screen.findByText(/No matching commands — Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText('/tangent')).not.toBeInTheDocument()
    expect(screen.queryByRole('option')).not.toBeInTheDocument()
  })
})

// Regression for #5041 (sibling of #5029): with zero matches the menu used to
// render null while `visible` stayed true, so an INVISIBLE keyboard listener
// swallowed Enter on unmatched slash input like "/xyz" — the message could not
// be sent, with nothing on screen to explain why. Now the settled zero-match
// state announces the mode flip and releases the keys.
describe('SlashCommandMenu zero-match key release', () => {
  it('with zero matches, Enter passes through un-prevented and closes the menu', async () => {
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness input="/xyz" onSelect={onSelect} onClose={onClose} />)
    // The settled-empty state announces the mode flip (Enter now sends);
    // waiting for it also waits out the in-flight swallow window.
    await screen.findByText(/No matching commands — Enter sends the message/)
    // fireEvent returns false when preventDefault was called; the composer's
    // own Enter-to-send only fires when the keystroke is NOT prevented.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('with zero matches, Tab passes through un-prevented and closes the menu', async () => {
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness input="/xyz" onSelect={onSelect} onClose={onClose} />)
    await screen.findByText(/No matching commands — Enter sends the message/)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('while the commands fetch is in flight, Enter stays swallowed and no empty state shows yet', async () => {
    // Before the remote list replaces the synchronous fallback, a
    // server-only command like "/xyz" is transiently a zero-match; releasing
    // there would send the half-typed command as a chat message.
    mockApi.slashCommands.mockImplementation(() => new Promise(() => {}))
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness input="/xyz" onSelect={onSelect} onClose={onClose} />)
    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalled())
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('with matches, Enter is still consumed by the menu (not released)', async () => {
    const onSelect = vi.fn()
    render(<Harness input="/a" onSelect={onSelect} />)
    await screen.findByText('/aa')
    // The inverse of the zero-match release: a populated menu keeps its claim.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onSelect).toHaveBeenCalledWith('/aa ')
  })

  it('in ctrl-enter send mode, the settled-empty copy names Ctrl+Enter (bare Enter is a newline there)', async () => {
    render(<Harness input="/xyz" sendOnEnter="ctrl-enter" />)
    expect(await screen.findByText(/Ctrl\+Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText(/— Enter sends the message/)).not.toBeInTheDocument()
  })
})
