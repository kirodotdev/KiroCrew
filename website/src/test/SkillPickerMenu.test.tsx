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
 *  and a QueryClientProvider (the menu reads the shared ['skills'] cache).
 *  Pass `client` to drive the cache from the test (e.g. trigger a refetch). */
function Harness({ query, open, onSelect = vi.fn(), onClose = vi.fn(), client, sendOnEnter, slotKey, project, agent, onTrustRequest }: {
  query: string; open: boolean; onSelect?: (i: { leaf: string; key: string }) => void; onClose?: () => void
  client?: QueryClient; sendOnEnter?: 'enter' | 'ctrl-enter' | 'enter-ctrl-newline'
  slotKey?: string; project?: string; agent?: string
  onTrustRequest?: (i: { leaf: string; key: string }) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SkillPickerMenu
          query={query} anchorRef={ref} open={open} onSelect={onSelect} onClose={onClose}
          sendOnEnter={sendOnEnter} slotKey={slotKey} project={project} agent={agent}
          onTrustRequest={onTrustRequest}
        />
      </div>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
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

  it('forwards the active agent to api.skills() and uses an agent-scoped cache key', async () => {
    render(<Harness query="" open slotKey="dashboard:chat-1" agent="custom-template" />)
    await waitFor(() => expect(mockApi.skills)
      .toHaveBeenLastCalledWith('dashboard:chat-1', 'custom-template', expect.any(AbortSignal)))
  })

  it('omits the agent argument when none is active', async () => {
    render(<Harness query="" open slotKey="dashboard:chat-1" />)
    await waitFor(() => expect(mockApi.skills)
      .toHaveBeenLastCalledWith('dashboard:chat-1', undefined, expect.any(AbortSignal)))
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

  it('shows "No matching skills" when filter excludes everything, announcing that Enter sends', async () => {
    render(<Harness query="zzznope" open />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(await screen.findByText(/No matching skills/)).toBeInTheDocument()
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

  // Regression for #5041 (sibling of #5029): a $token that matches no skill
  // used to swallow Enter — the message could not be sent while the empty
  // picker was open.
  it('with zero matches, Enter passes through un-prevented and closes the menu', async () => {
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="zzznope" open onSelect={onSelect} onClose={onClose} />)
    // The settled-empty state announces the mode flip (Enter now sends).
    await screen.findByText(/No matching skills — Enter sends the message/)
    // fireEvent returns false when preventDefault was called; the composer's
    // own Enter-to-send only fires when the keystroke is NOT prevented.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('with zero matches, Tab passes through un-prevented and closes the menu', async () => {
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="zzznope" open onSelect={onSelect} onClose={onClose} />)
    await screen.findByText(/No matching skills — Enter sends the message/)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('with matches, Enter is still consumed by the menu (not released)', async () => {
    const onSelect = vi.fn()
    render(<Harness query="grill" open onSelect={onSelect} />)
    await screen.findByText('$grill')
    // The inverse of the zero-match release: a populated menu keeps its claim.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(onSelect).toHaveBeenCalledTimes(1))
  })

  it('while the skills list is still loading, Enter stays swallowed (no premature send)', async () => {
    // A never-settling fetch models the loading window: matches are
    // transiently unknowable, and releasing Enter here would send a draft
    // whose $token the user was still completing.
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="grill" open onSelect={onSelect} onClose={onClose} />)
    expect(await screen.findByText(/Loading skills…/)).toBeInTheDocument()
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('while a background refetch is in flight over a cached empty list, Enter stays swallowed', async () => {
    // A cached-but-refetching list is not settled: isLoading is false (data
    // exists) but the authoritative answer is still arriving, so releasing
    // Enter here would send a draft on stale knowledge. The gate must key on
    // isFetching, not isLoading. Seed a stale empty cache, then mount with a
    // never-settling fetch — the mount refetch is the in-flight window.
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    qc.setQueryData(['skills', null, null, null], [])
    await qc.invalidateQueries({ queryKey: ['skills'], refetchType: 'none' })
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="grill" open onSelect={onSelect} onClose={onClose} client={qc} />)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    // Cached [] renders the plain empty state; the release stays un-armed.
    expect(await screen.findByText('No matching skills')).toBeInTheDocument()
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('after the skills fetch settles in an ERROR, Enter is released (trap must not survive the error path)', async () => {
    // A failed fetch leaves an empty list; keeping the swallow there would recreate
    // the trap on any transient failure. The release is what this test pins.
    mockApi.skills.mockRejectedValue(new Error('boom'))
    const onSelect = vi.fn()
    const onClose = vi.fn()
    render(<Harness query="grill" open onSelect={onSelect} onClose={onClose} />)
    expect(await screen.findByText(/Couldn't load skills — Enter sends the message/)).toBeInTheDocument()
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('in ctrl-enter send mode, the settled-empty copy names Ctrl+Enter (bare Enter is a newline there)', async () => {
    render(<Harness query="zzznope" open sendOnEnter="ctrl-enter" />)
    expect(await screen.findByText(/Ctrl\+Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText(/— Enter sends the message/)).not.toBeInTheDocument()
  })
})

/* ── Agent scope cue (#6028) ──
 * When the server actually applied the active agent's skill:// mapping it
 * answers with {skills, agent_scoped: true, agent} instead of the bare array.
 * The picker must (a) attribute a scoped-empty catalog to the MAPPING, and
 * (b) mark a scoped non-empty list with a footer — and render NEITHER cue
 * for the legacy bare-array shape. Wording is mapping/scope only ("mapped",
 * "scoped"), never availability, per the #3820 semantics ruling. */
describe('SkillPickerMenu — agent scope cue', () => {
  const SCOPED = { skills: SKILLS, agent_scoped: true, agent: 'custom-template' }

  it('renders a "Scoped to agent" footer on a scoped non-empty list', async () => {
    mockApi.skills.mockResolvedValue(SCOPED)
    render(<Harness query="" open slotKey="dashboard:chat-1" agent="custom-template" />)
    await screen.findByText('$grill')
    expect(screen.getByText(/Scoped to agent custom-template/i)).toBeInTheDocument()
  })

  it('renders no footer for the legacy bare-array shape, even with an agent prop', async () => {
    // An agent with no skill:// mapping of its own keeps the unfiltered
    // listing — the cue must come from the server flag, not the prop.
    render(<Harness query="" open slotKey="dashboard:chat-1" agent="plain" />)
    await screen.findByText('$grill')
    expect(screen.queryByText(/Scoped to agent/i)).not.toBeInTheDocument()
  })

  it('attributes a scoped-EMPTY catalog to the mapping, preserving the Enter announcement', async () => {
    mockApi.skills.mockResolvedValue({ skills: [], agent_scoped: true, agent: 'custom-template' })
    render(<Harness query="" open slotKey="dashboard:chat-1" agent="custom-template" />)
    expect(await screen.findByText(
      /No skills mapped to custom-template — Enter sends the message/,
    )).toBeInTheDocument()
    // The mapped copy already names the agent, so the footer would be
    // redundant here — it must not render alongside.
    expect(screen.queryByText(/Scoped to agent/i)).not.toBeInTheDocument()
    // Settled-empty still releases Enter to the composer (#5029/#5041).
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
  })

  it('names Ctrl+Enter in the mapped-empty copy when that is the send binding', async () => {
    mockApi.skills.mockResolvedValue({ skills: [], agent_scoped: true, agent: 'custom-template' })
    render(<Harness query="" open agent="custom-template" sendOnEnter="ctrl-enter" />)
    expect(await screen.findByText(
      /No skills mapped to custom-template — Ctrl\+Enter sends the message/,
    )).toBeInTheDocument()
  })

  it('keeps the generic copy on a query-miss over a scoped list, but keeps the scope footer', async () => {
    // The typed filter (not the mapping) emptied the rows: the mapped copy
    // would be wrong, but the footer must stay — "no match HERE" and
    // "no match anywhere" are exactly the ambiguity the cue removes.
    mockApi.skills.mockResolvedValue(SCOPED)
    render(<Harness query="zzznope" open agent="custom-template" />)
    expect(await screen.findByText(/No matching skills — Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText(/No skills mapped to/)).not.toBeInTheDocument()
    expect(screen.getByText(/Scoped to agent custom-template/i)).toBeInTheDocument()
  })

  it('keeps the generic empty copy for a bare-array empty catalog (nothing exists ≠ nothing mapped)', async () => {
    mockApi.skills.mockResolvedValue([])
    render(<Harness query="" open agent="plain" />)
    expect(await screen.findByText(/No matching skills — Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText(/No skills mapped to/)).not.toBeInTheDocument()
  })

  it('rows from the scoped envelope stay selectable (unwrap does not break choose)', async () => {
    mockApi.skills.mockResolvedValue(SCOPED)
    const onSelect = vi.fn()
    render(<Harness query="grill" open agent="custom-template" onSelect={onSelect} />)
    fireEvent.mouseDown(await screen.findByText('$grill'))
    expect(onSelect).toHaveBeenCalledWith({ leaf: 'grill', key: 'grill' })
  })

  it('announces the scope through a role="status" live region outside the listbox', async () => {
    // A listbox owns only option/group children, and THIS listbox is never
    // the focus/AT-current node (rows are tabIndex={-1}, the composer keeps
    // focus) — so the cue must be a live region, the same mechanism the
    // empty state uses, not a description attached to the listbox.
    mockApi.skills.mockResolvedValue(SCOPED)
    render(<Harness query="" open agent="custom-template" />)
    await screen.findByText('$grill')
    const listbox = screen.getByRole('listbox')
    const footer = screen.getByText(/Scoped to agent custom-template/i)
    expect(listbox.contains(footer)).toBe(false)
    expect(footer.getAttribute('role')).toBe('status')
    expect(footer.getAttribute('aria-hidden')).not.toBe('true')
  })

  it('renders no scope status region for an unscoped list', async () => {
    render(<Harness query="" open agent="plain" />)
    await screen.findByText('$grill')
    expect(screen.queryByText(/Scoped to agent/i)).not.toBeInTheDocument()
  })

  it('renders the cue from a cache seeded with the RAW envelope (prefetch interchangeability)', async () => {
    // ChatInput's focus-prefetch stores api.skills' raw payload under the
    // same query key. The picker must render the scope cue from that cached
    // envelope without its own fetch — pin it by never letting the queryFn
    // settle.
    mockApi.skills.mockImplementation(() => new Promise(() => {}))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    qc.setQueryData(
      ['skills', 'dashboard:chat-1', '/work/project-a', 'custom-template'],
      { skills: SKILLS, agent_scoped: true, agent: 'custom-template' },
    )
    render(
      <Harness
        query="" open client={qc}
        slotKey="dashboard:chat-1" project="/work/project-a" agent="custom-template"
      />,
    )
    expect(await screen.findByText('$grill')).toBeInTheDocument()
    expect(screen.getByText(/Scoped to agent custom-template/i)).toBeInTheDocument()
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
    await waitFor(() => expect(mockApi.skills)
      .toHaveBeenCalledWith('dashboard:chat-7', undefined, expect.any(AbortSignal)))
  })

  it('refetches when the same slot switches projects', async () => {
    mockApi.skills
      .mockResolvedValueOnce([{ ...TRUSTED[0], key: 'kiro-workspace/project-a' }])
      .mockResolvedValueOnce([{ ...TRUSTED[0], key: 'kiro-workspace/project-b' }])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    const view = render(
      <Harness
        query=""
        open
        slotKey="dashboard:chat-7"
        project="/work/project-a"
        client={client}
      />,
    )
    expect(await screen.findByText('$project-a')).toBeInTheDocument()

    view.rerender(
      <Harness
        query=""
        open
        slotKey="dashboard:chat-7"
        project="/work/project-b"
        client={client}
      />,
    )

    expect(await screen.findByText('$project-b')).toBeInTheDocument()
    expect(mockApi.skills).toHaveBeenCalledTimes(2)
  })

  it('refetches on reopen when the caller cannot provide project identity', async () => {
    mockApi.skills
      .mockResolvedValueOnce([{ ...TRUSTED[0], key: 'kiro-workspace/project-a' }])
      .mockResolvedValueOnce([{ ...TRUSTED[0], key: 'kiro-workspace/project-b' }])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
    const view = render(
      <Harness query="" open slotKey="dashboard:chat-7" client={client} />,
    )
    expect(await screen.findByText('$project-a')).toBeInTheDocument()

    view.rerender(
      <Harness query="" open={false} slotKey="dashboard:chat-7" client={client} />,
    )
    view.rerender(
      <Harness query="" open slotKey="dashboard:chat-7" client={client} />,
    )

    expect(await screen.findByText('$project-b')).toBeInTheDocument()
    expect(mockApi.skills).toHaveBeenCalledTimes(2)
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
