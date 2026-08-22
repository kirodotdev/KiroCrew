import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillPickerMenu from '../components/SkillPickerMenu'

const SKILLS = [
  { key: 'WorkforceEmploymentKnowledgeBase/oncall-handover', name: 'oncall-handover', description: 'Handover report', source: 'package' },
  { key: 'ticket-pull', name: 'ticket-pull', description: 'Pull tickets', source: 'kirocrew' },
  { key: 'grill', name: 'grill', description: 'Structured questioning', source: 'kirocrew' },
]

/** Harness: gives the menu a real anchored element (it reads getBoundingClientRect)
 *  and a QueryClientProvider (the menu reads the shared ['skills'] cache). */
function Harness({ query, open, onSelect = vi.fn(), onClose = vi.fn(), slotKey, onTrustRequest }: {
  query: string; open: boolean; onSelect?: (i: { leaf: string; key: string }) => void; onClose?: () => void
  slotKey?: string; onTrustRequest?: (i: { leaf: string; key: string }) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SkillPickerMenu
          query={query} anchorRef={ref} open={open} onSelect={onSelect} onClose={onClose}
          slotKey={slotKey} onTrustRequest={onTrustRequest}
        />
      </div>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.skills.mockResolvedValue(SKILLS)
})

describe('SkillPickerMenu', () => {
  it('renders nothing when closed', () => {
    render(<Harness query="" open={false} />)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('fetches and lists all skills on open with empty query, $-prefixed', async () => {
    render(<Harness query="" open />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalledOnce())
    expect(await screen.findByText('$oncall-handover')).toBeInTheDocument()
    expect(screen.getByText('$ticket-pull')).toBeInTheDocument()
    expect(screen.getByText('$grill')).toBeInTheDocument()
  })

  it('filters by leaf-name substring', async () => {
    render(<Harness query="hand" open />)
    expect(await screen.findByText('$oncall-handover')).toBeInTheDocument()
    expect(screen.queryByText('$ticket-pull')).not.toBeInTheDocument()
    expect(screen.queryByText('$grill')).not.toBeInTheDocument()
  })

  it('shows the description as secondary text', async () => {
    render(<Harness query="grill" open />)
    expect(await screen.findByText('Structured questioning')).toBeInTheDocument()
  })

  it('shows a source badge for non-kirocrew skills', async () => {
    render(<Harness query="handover" open />)
    await screen.findByText('$oncall-handover')
    expect(screen.getByText('package')).toBeInTheDocument()
  })

  it('shows "No matching skills" when filter excludes everything', async () => {
    render(<Harness query="zzznope" open />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(await screen.findByText('No matching skills')).toBeInTheDocument()
  })

  it('calls onSelect with leaf + key on click', async () => {
    const onSelect = vi.fn()
    render(<Harness query="handover" open onSelect={onSelect} />)
    const opt = await screen.findByText('$oncall-handover')
    fireEvent.mouseDown(opt)
    expect(onSelect).toHaveBeenCalledWith({
      leaf: 'oncall-handover',
      key: 'WorkforceEmploymentKnowledgeBase/oncall-handover',
    })
  })

  it('Enter selects the highlighted item', async () => {
    const onSelect = vi.fn()
    render(<Harness query="" open onSelect={onSelect} />)
    await screen.findByText('$oncall-handover')
    fireEvent.keyDown(document, { key: 'Enter' })
    // first item (index 0) is selected by default
    expect(onSelect).toHaveBeenCalledWith({
      leaf: 'oncall-handover',
      key: 'WorkforceEmploymentKnowledgeBase/oncall-handover',
    })
  })

  it('ArrowDown then Enter selects the second item', async () => {
    const onSelect = vi.fn()
    render(<Harness query="" open onSelect={onSelect} />)
    await screen.findByText('$ticket-pull')
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith({ leaf: 'ticket-pull', key: 'ticket-pull' })
  })

  it('Escape calls onClose', async () => {
    const onClose = vi.fn()
    render(<Harness query="" open onClose={onClose} />)
    await screen.findByText('$grill')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('dedupes skills that share a leaf name', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'kirocrew/grill', name: 'grill', description: 'local grill', source: 'kirocrew' },
      { key: 'AIMPkg/grill', name: 'grill', description: 'aim grill', source: 'package' },
    ])
    render(<Harness query="grill" open />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    const matches = await screen.findAllByText('$grill')
    expect(matches).toHaveLength(1)
  })
})

/* ── Project-skills trust gate ──
 * A workspace skill is listed before consent but must not behave like a usable
 * one: its $token cannot resolve until the operator trusts the directory, so
 * choosing it has to route to consent rather than insert a dead token. */
describe('SkillPickerMenu — project-skills trust', () => {
  const UNTRUSTED = [
    {
      key: 'kiro-workspace/oncall-handover', name: 'oncall-handover',
      description: 'Handover report', source: 'kiro-workspace', trusted: false,
    },
  ]
  const TRUSTED = [
    {
      key: 'kiro-workspace/oncall-handover', name: 'oncall-handover',
      description: 'Handover report', source: 'kiro-workspace', trusted: true,
    },
  ]

  it('sends the real slot key so the server resolves THIS chat project', async () => {
    render(<Harness query="" open slotKey="dashboard:chat-7" />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalledWith('dashboard:chat-7'))
  })

  it('marks an untrusted project skill as needing trust', async () => {
    mockApi.skills.mockResolvedValue(UNTRUSTED)
    render(<Harness query="" open slotKey="dashboard:chat-7" onTrustRequest={vi.fn()} />)
    expect(await screen.findByText('Needs trust')).toBeInTheDocument()
    // The raw source badge is replaced by the trust state, not shown alongside.
    expect(screen.queryByText('kiro-workspace')).not.toBeInTheDocument()
  })

  it('routes an untrusted project skill to consent instead of inserting a token', async () => {
    mockApi.skills.mockResolvedValue(UNTRUSTED)
    const onSelect = vi.fn()
    const onTrustRequest = vi.fn()
    render(<Harness query="" open slotKey="dashboard:chat-7" onSelect={onSelect} onTrustRequest={onTrustRequest} />)
    fireEvent.mouseDown(await screen.findByText('$oncall-handover'))
    expect(onTrustRequest).toHaveBeenCalledWith({
      leaf: 'oncall-handover', key: 'kiro-workspace/oncall-handover',
    })
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('inserts normally once the project is trusted', async () => {
    mockApi.skills.mockResolvedValue(TRUSTED)
    const onSelect = vi.fn()
    const onTrustRequest = vi.fn()
    render(<Harness query="" open slotKey="dashboard:chat-7" onSelect={onSelect} onTrustRequest={onTrustRequest} />)
    fireEvent.mouseDown(await screen.findByText('$oncall-handover'))
    expect(onSelect).toHaveBeenCalledWith({
      leaf: 'oncall-handover', key: 'kiro-workspace/oncall-handover',
    })
    expect(onTrustRequest).not.toHaveBeenCalled()
    expect(screen.queryByText('Needs trust')).not.toBeInTheDocument()
  })

  it('Enter on an untrusted row also routes to consent', async () => {
    mockApi.skills.mockResolvedValue(UNTRUSTED)
    const onSelect = vi.fn()
    const onTrustRequest = vi.fn()
    render(<Harness query="" open slotKey="dashboard:chat-7" onSelect={onSelect} onTrustRequest={onTrustRequest} />)
    await screen.findByText('$oncall-handover')
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onTrustRequest).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })
})
