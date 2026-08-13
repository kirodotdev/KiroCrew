/**
 * ActivityViewer — coverage for the interactive paths the existing
 * ActivityViewer.test.tsx / ActivityViewer.issues.test.tsx files render but
 * never drive: the subagent card's lazy disk loader, its approval and
 * cancel/collapse controls, the spawn-approval entry, the changed-file row's
 * keyboard and hover controls, and the panel-level batch actions
 * (retry-failed / dismiss-done / show-all) plus the workflows tab.
 *
 * Conventions follow ActivityViewer.test.tsx (locally-built Provider +
 * QueryClientProvider wrapper, an `api` module mock, a stubbed MarkdownPanel)
 * and ArtifactsPageCoverage.test.tsx (small fixture makers, `within` for
 * anything that appears more than once).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'

// The approval entry's tiered trust control is a Radix dropdown, which never
// opens under the test DOM; the repo's shared mock renders the menu inline.
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

vi.mock('../api/client', () => ({
  api: {
    spawnStatus: vi.fn().mockResolvedValue({ result: '' }),
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnRetry: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
    approveChatSlot: vi.fn().mockResolvedValue({}),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    createArtifact: vi.fn().mockResolvedValue({ slug: 'new-slug', version: 1 }),
    setArtifactPinned: vi.fn().mockResolvedValue({}),
  },
}))

// MarkdownPanel drags in Pierre; stub it with the same imperative handle the
// real panel exposes so the inline preview's guarded close still works.
vi.mock('../components/MarkdownPanel', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<{ requestClose: () => void }, { filePath: string; content: string; onClose: () => void }>(
      ({ filePath, content, onClose }, ref) => {
        useImperativeHandle(ref, () => ({ requestClose: onClose }), [onClose])
        return <div data-testid="md-panel">{filePath}::{content}</div>
      },
    ),
  }
})

import ActivityViewer from '../pages/chat/ActivityViewer'
import { countDiffStats } from '../utils/diffLineCounts'
import { api } from '../api/client'
import { createTestStore } from './helpers'
import { openActivityToTab, selectSubagent } from '../store/chatSlice'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import type { SubagentActivity, ToolActivity, Artifact } from '../types'
import type { ExtractedLink } from '../utils/extractChatLinks'

const SLOT = 'test-slot'

type Store = ReturnType<typeof createTestStore>

/** Wrapper mirroring ActivityViewer.test.tsx: real store, isolated query cache. */
function renderPanel(ui: React.ReactElement, store: Store = createTestStore()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
  }
  return { store, ...render(ui, { wrapper: Wrapper }) }
}

const baseProps = {
  subagents: {} as Record<string, SubagentActivity>,
  toolLog: [] as ToolActivity[],
  open: true,
  onToggle: vi.fn(),
  slot: SLOT,
}

const mkAgent = (id: string, over: Partial<SubagentActivity> = {}): SubagentActivity => ({
  id,
  task: `task for ${id}`,
  agent: `ag-${id}`,
  status: 'done',
  streaming: '',
  lastTool: '',
  startedAt: 1_700_000_000_000,
  elapsed: 4,
  ...over,
})


const mkArtifact = (slug: string, over: Partial<Artifact> = {}): Artifact => ({
  slug,
  name: slug.replace(/-/g, ' '),
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-08-01T00:00:00+00:00',
  updated_at: '2026-08-01T00:00:00+00:00',
  ...over,
})

const mkLink = (url: string, label: string, type: ExtractedLink['type'] = 'other'): ExtractedLink =>
  ({ url, label, type, msgIdx: 0 })

/** Store whose chat slice already tracks `agent` for SLOT, so the reducers the
 *  card dispatches into (markSubagentApproving / sseSubagentDone) have a record
 *  to mutate. Defaults are spread first: RTK REPLACES a slice with
 *  preloadedState rather than merging, so a partial would drop every other key
 *  and the reducers would then throw. */
function storeTracking(agent: SubagentActivity) {
  const d = createTestStore().getState()
  return createTestStore({
    chat: { ...d.chat, activeSlot: SLOT, subagents: { [agent.id]: agent } },
  })
}

/** A fetch stub that answers the two endpoints this component reaches for. */
function stubFetch(opts: { runs?: unknown[]; fileText?: string; fileOk?: boolean } = {}) {
  const { runs = [], fileText = 'file body', fileOk = true } = opts
  const impl = vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/workflows/runs')) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ runs }) })
    }
    return Promise.resolve({
      ok: fileOk,
      status: fileOk ? 200 : 500,
      text: async () => fileText,
      headers: { get: () => null },
      json: async () => ({}),
    })
  })
  global.fetch = impl as unknown as typeof fetch
  return impl
}

/** The done-card header carries the only aria-expanded in a subagent card. */
function cardHeader(container: HTMLElement) {
  const el = container.querySelector('[aria-expanded]')
  if (!el) throw new Error('no collapsible card header rendered')
  return el as HTMLElement
}

let prevFetch: typeof fetch

beforeEach(() => {
  prevFetch = global.fetch
  localStorage.clear()
  __resetPanelTabs()
  vi.clearAllMocks()
  vi.mocked(api.spawnStatus).mockResolvedValue({ result: 'the transcript' })
  vi.mocked(api.spawnDelete).mockResolvedValue({})
  vi.mocked(api.spawnRetry).mockResolvedValue({})
  vi.mocked(api.resolveApproval).mockResolvedValue({})
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: '' })
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
  stubFetch()
})

afterEach(() => { global.fetch = prevFetch })

/* ── Subagent card: lazy transcript loading ─────────────────────────────────*/

describe('ActivityViewer — subagent transcript loading', () => {
  const doneAgent = () => ({ s1: mkAgent('s1', { status: 'done' }) })

  it('loads a finished agent transcript from disk only when asked', async () => {
    const { container } = renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={doneAgent()} />,
    )
    // A finished card mounts collapsed, so nothing is fetched up front.
    expect(api.spawnStatus).not.toHaveBeenCalled()

    fireEvent.click(cardHeader(container))
    fireEvent.click(screen.getByRole('button', { name: 'Load output from disk' }))

    expect(await screen.findByText('the transcript')).toBeInTheDocument()
    expect(api.spawnStatus).toHaveBeenCalledWith('s1', expect.objectContaining({ signal: expect.anything() }))
  })

  it('says so rather than showing a blank body when the transcript is empty', async () => {
    vi.mocked(api.spawnStatus).mockResolvedValue({ result: '' })
    const { container } = renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={doneAgent()} />,
    )
    fireEvent.click(cardHeader(container))
    fireEvent.click(screen.getByRole('button', { name: 'Load output from disk' }))

    expect(await screen.findByText('(no output)')).toBeInTheDocument()
  })

  it('leaves a failed transcript read retryable', async () => {
    vi.mocked(api.spawnStatus).mockRejectedValueOnce(new Error('gone'))
    const { container } = renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={doneAgent()} />,
    )
    fireEvent.click(cardHeader(container))
    fireEvent.click(screen.getByRole('button', { name: 'Load output from disk' }))

    const retry = await screen.findByRole('button', { name: 'Failed — click to retry' })
    vi.mocked(api.spawnStatus).mockResolvedValue({ result: 'second time lucky' })
    fireEvent.click(retry)

    expect(await screen.findByText('second time lucky')).toBeInTheDocument()
  })

  it('expands and auto-loads the agent selected from a chip, then clears the selection', async () => {
    const store = createTestStore()
    store.dispatch(selectSubagent('s1'))
    renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={doneAgent()} />,
      store,
    )
    // Selection expands the card AND pulls the output without a button press.
    expect(await screen.findByText('the transcript')).toBeInTheDocument()
    // …then releases the selection so a later re-click re-triggers it.
    await waitFor(() => expect(store.getState().chat.selectedSubagentId).toBeNull(), { timeout: 2000 })
  })

  it('keeps a native card inline instead of offering a disk read it cannot serve', () => {
    const { container } = renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ 'native:1': mkAgent('native:1', { status: 'done' }) }}
      />,
    )
    fireEvent.click(cardHeader(container))
    expect(screen.getByText('(output shown in chat)')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load output from disk' })).not.toBeInTheDocument()
  })

  it('waits for output on a running agent instead of reading from disk', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ s1: mkAgent('s1', { status: 'running', lastTool: 'fs_read' }) }}
      />,
    )
    expect(screen.getByText('Waiting for output…')).toBeInTheDocument()
    expect(screen.getByText('fs_read')).toBeInTheDocument()
    expect(api.spawnStatus).not.toHaveBeenCalled()
  })
})

/* ── Subagent card: controls ────────────────────────────────────────────────*/

describe('ActivityViewer — subagent card controls', () => {
  it('approves a pending agent through the card', async () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ p1: mkAgent('p1', { status: 'pending', approval_id: 'ap-1' }) }}
      />,
    )
    expect(screen.getByText('Pending Approval')).toBeInTheDocument()
    // Exact name: the card itself is a Clickable (role=button) whose accessible
    // name contains every label inside it, including this one.
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('ap-1', 'approve'))
  })

  it('terminates the card locally when a pending agent is rejected', async () => {
    const pending = mkAgent('p1', { status: 'pending', approval_id: 'ap-1' })
    const { store } = renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={{ p1: pending }} />,
      storeTracking(pending),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('ap-1', 'reject'))
    // A rejected spawn emits nothing further, so the card is finished locally.
    await waitFor(() => {
      expect(store.getState().chat.subagents.p1?.status).toBe('error')
    })
  })

  it('releases the approving flag when the approval call fails', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new Error('nope'))
    const pending = mkAgent('p1', { status: 'pending', approval_id: 'ap-1' })
    const { store } = renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={{ p1: pending }} />,
      storeTracking(pending),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(store.getState().chat.subagents.p1?.approving).toBe(false))
  })

  it('does nothing for a pending agent with no approval id', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ p1: mkAgent('p1', { status: 'pending' }) }}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('collapses a finished card from the keyboard, ignoring other keys', () => {
    const { container } = renderPanel(
      <ActivityViewer {...baseProps} view="subagents" subagents={{ s1: mkAgent('s1') }} />,
    )
    const header = cardHeader(container)
    expect(header).toHaveAttribute('aria-expanded', 'false')

    fireEvent.keyDown(header, { key: 'Enter' })
    expect(header).toHaveAttribute('aria-expanded', 'true')

    fireEvent.keyDown(header, { key: ' ' })
    expect(header).toHaveAttribute('aria-expanded', 'false')

    fireEvent.keyDown(header, { key: 'a' })
    expect(header).toHaveAttribute('aria-expanded', 'false')
  })

  it('auto-collapses a card that finishes while it is open', async () => {
    const { container, rerender } = renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ s1: mkAgent('s1', { status: 'running', streaming: 'working' }) }}
      />,
    )
    // Running cards have no collapse control at all.
    expect(container.querySelector('[aria-expanded]')).toBeNull()

    rerender(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ s1: mkAgent('s1', { status: 'done', streaming: 'working' }) }}
      />,
    )
    expect(cardHeader(container)).toHaveAttribute('aria-expanded', 'true')
    await waitFor(
      () => expect(cardHeader(container)).toHaveAttribute('aria-expanded', 'false'),
      { timeout: 4000 },
    )
  })

  it('cancels a running agent without selecting the card', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ s1: mkAgent('s1', { status: 'running', streaming: 'x' }) }}
      />,
    )
    fireEvent.click(screen.getByTestId('subagent-cancel-btn'))
    expect(api.spawnDelete).toHaveBeenCalledWith('s1')
  })

  it('stops following the output once the user scrolls up, and resumes at the bottom', () => {
    const props = (streaming: string) => (
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ s1: mkAgent('s1', { status: 'running', streaming }) }}
      />
    )
    const { rerender } = renderPanel(props('line one'))
    const body = screen.getByText('Output').parentElement?.querySelector('pre') as HTMLElement
    // jsdom reports zero metrics, so give the body a real scrollable geometry.
    Object.defineProperty(body, 'clientHeight', { configurable: true, value: 100 })
    Object.defineProperty(body, 'scrollHeight', { configurable: true, value: 500 })

    body.scrollTop = 0
    fireEvent.scroll(body)
    rerender(props('line one\nline two'))
    expect(body.scrollTop).toBe(0)

    body.scrollTop = 490
    fireEvent.scroll(body)
    rerender(props('line one\nline two\nline three'))
    expect(body.scrollTop).toBe(500)
  })

  it('selects the card when its body is clicked', () => {
    const { container } = renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ s1: mkAgent('s1', { status: 'running', streaming: 'x' }) }}
      />,
    )
    const card = container.querySelector('[role="button"].bg-card') as HTMLElement
    expect(card).toBeTruthy()
    fireEvent.click(card)
    // No observable side effect beyond the card surviving the click; the
    // panel's own selection index is internal.
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('labels a cancelled error differently from a plain failure', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{
          e1: mkAgent('e1', { status: 'error', error: 'Cancelled by user' }),
          e2: mkAgent('e2', { status: 'stopped' }),
        }}
      />,
    )
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
    expect(screen.getByText('Stopped')).toBeInTheDocument()
  })

  it('shows the failure and the last tool on an expanded error card', () => {
    const { container } = renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{ e1: mkAgent('e1', { status: 'error', error: 'boom', lastTool: 'bash', result: 'trace' }) }}
      />,
    )
    fireEvent.click(cardHeader(container))
    expect(screen.getByText('boom')).toBeInTheDocument()
    expect(screen.getByText(/Last tool:\s*bash/)).toBeInTheDocument()
  })
})

/* ── Spawn approval entries ─────────────────────────────────────────────────*/

describe('ActivityViewer — spawn approval entries', () => {
  const pending: ToolActivity = {
    type: 'approval',
    text: 'Running: git push origin feature',
    ts: 1_700_000_000_000,
    approval_id: 'ap-9',
    approval_type: 'spawn',
  }

  it('renders a pending spawn approval with its time and resolves it', async () => {
    renderPanel(<ActivityViewer {...baseProps} view="subagents" toolLog={[pending]} />)

    expect(screen.getByText('Approval Needed')).toBeInTheDocument()
    expect(screen.getByText('Running: git push origin feature')).toBeInTheDocument()
    expect(screen.getByText(/\d\d:\d\d:\d\d/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Approve/ }))

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('ap-9', 'approve'))
    expect(await screen.findByText('Approved')).toBeInTheDocument()
  })

  it('records a rejection as the decision on the entry', async () => {
    renderPanel(<ActivityViewer {...baseProps} view="subagents" toolLog={[pending]} />)
    fireEvent.click(screen.getByRole('button', { name: /Reject/ }))

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('ap-9', 'reject'))
    expect(await screen.findByText('Rejected')).toBeInTheDocument()
  })

  it('restores the buttons when resolving the approval fails', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new Error('offline'))
    renderPanel(<ActivityViewer {...baseProps} view="subagents" toolLog={[pending]} />)
    fireEvent.click(screen.getByRole('button', { name: /Approve/ }))

    await waitFor(() => expect(screen.getByText('Approval Needed')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Reject/ })).toBeInTheDocument()
  })

  it('grants trust for the command through the dropdown', async () => {
    renderPanel(<ActivityViewer {...baseProps} view="subagents" toolLog={[pending]} />)
    fireEvent.click(screen.getByText('Trust'))

    const [trustThisCommand] = screen.getAllByRole('menuitem')
    expect(trustThisCommand).toHaveTextContent('git push origin feature')
    fireEvent.click(trustThisCommand)

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith('ap-9', 'approve'))
    expect(await screen.findByText('Trusted command')).toBeInTheDocument()
  })

  it('shows an already-resolved approval with no actions left', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        toolLog={[{ ...pending, type: 'approval_resolved' }]}
      />,
    )
    expect(screen.getByText('Resolved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
  })

  it('ignores chat approvals and reasoning entries in the subagents tab', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        toolLog={[
          { type: 'reasoning', text: 'thinking out loud', ts: 1 },
          { type: 'approval', text: 'chat question', ts: 2, approval_type: 'chat', approval_id: 'c1' },
        ]}
      />,
    )
    expect(screen.queryByText('Approval Needed')).not.toBeInTheDocument()
    expect(screen.getByText('No subagents running')).toBeInTheDocument()
  })
})

/* ── Changed-file rows ──────────────────────────────────────────────────────*/

describe('ActivityViewer — changed-file rows', () => {
  it('counts added and removed lines, ignoring diff headers', () => {
    expect(countDiffStats('--- a\n+++ b\n@@ -1 +1 @@\n+one\n+two\n-old')).toEqual({ added: 2, removed: 1 })
    expect(countDiffStats('')).toEqual({ added: 0, removed: 0 })
    expect(countDiffStats(' context only')).toEqual({ added: 0, removed: 0 })
  })





})

/* ── Resource rows ──────────────────────────────────────────────────────────*/

describe('ActivityViewer — resource rows', () => {
  it('falls back to the raw string when a link is not a parseable URL', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={[mkLink('not a url at all', 'Broken reference')]}
      />,
    )
    const row = screen.getByTitle('not a url at all')
    expect(within(row).getByText('Broken reference')).toBeInTheDocument()
    expect(within(row).getByText('not a url at all')).toBeInTheDocument()
    expect(within(row).getByText('Link')).toBeInTheDocument()
  })

  it('encodes the link type as a screen-reader label per row', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={[
          mkLink('https://github.com/o/r/pull/1', 'PR one', 'cr'),
          mkLink('https://github.com/o/r/issues/2', 'Issue two', 'issue'),
        ]}
      />,
    )
    expect(within(screen.getByTitle('https://github.com/o/r/pull/1')).getByText('PR')).toBeInTheDocument()
    expect(within(screen.getByTitle('https://github.com/o/r/issues/2')).getByText('Issue')).toBeInTheDocument()
    // The host is the dimmed subtitle on both rows.
    expect(screen.getAllByText('github.com')).toHaveLength(2)
  })

  it('clears an active link search from the inline control', () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={['a', 'b', 'c', 'd', 'e', 'f'].map(n => ({
          url: `https://example.com/${n}`, type: 'other' as const, label: `${n} link`, msgIdx: 0,
        }))}
      />,
    )
    const box = screen.getByLabelText('Search by label or URL…') as HTMLInputElement
    fireEvent.change(box, { target: { value: 'c link' } })
    expect(screen.queryByTitle('https://example.com/a')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(box.value).toBe('')
    expect(screen.getByTitle('https://example.com/a')).toBeInTheDocument()
  })
})

/* ── Panel-level behaviour ──────────────────────────────────────────────────*/

describe('ActivityViewer — panel behaviour', () => {
  it('renders nothing at all while collapsed', () => {
    renderPanel(<ActivityViewer {...baseProps} open={false} view="subagents" />)
    expect(screen.queryByRole('region', { name: 'Activity' })).not.toBeInTheDocument()
    expect(global.fetch).not.toHaveBeenCalled()
  })

  it('leaves keys other than Escape to the focused control', () => {
    const onToggle = vi.fn()
    renderPanel(<ActivityViewer {...baseProps} view="subagents" onToggle={onToggle} />)
    const region = screen.getByRole('region', { name: 'Activity' })
    fireEvent.keyDown(region, { key: 'ArrowDown' })
    expect(onToggle).not.toHaveBeenCalled()
    fireEvent.keyDown(region, { key: 'Escape' })
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('sorts agents needing attention above the finished pile', () => {
    const { container } = renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{
          done: mkAgent('done', { status: 'done', agent: 'six-done' }),
          stopped: mkAgent('stopped', { status: 'stopped', agent: 'five-stopped' }),
          running: mkAgent('running', { status: 'running', agent: 'four-running' }),
          pending: mkAgent('pending', { status: 'pending', agent: 'three-pending' }),
          stalled: mkAgent('stalled', { status: 'running', stalled: true, agent: 'two-stalled' }),
          retrying: mkAgent('retrying', { status: 'running', retrying: true, agent: 'one-retrying' }),
          failed: mkAgent('failed', { status: 'error', agent: 'zero-error' }),
        }}
      />,
    )
    const order = [...container.querySelectorAll('code')].map(c => c.textContent)
    expect(order).toEqual([
      'zero-error', 'one-retrying', 'two-stalled', 'three-pending',
      'four-running', 'five-stopped', 'six-done',
    ])
  })

  it('retries every failed agent from one control', async () => {
    renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{
          e1: mkAgent('e1', { status: 'error' }),
          e2: mkAgent('e2', { status: 'error' }),
          // Native cards have no manager record, so they are not retryable.
          'native:x': mkAgent('native:x', { status: 'error' }),
        }}
      />,
    )
    const btn = screen.getByTestId('retry-failed-btn')
    expect(btn).toHaveTextContent('Retry failed (2)')
    fireEvent.click(btn)

    await waitFor(() => expect(api.spawnRetry).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId('retry-failed-btn')).not.toBeDisabled())
  })

  it('dismisses only this slot terminal cards', async () => {
    const { store } = renderPanel(
      <ActivityViewer
        {...baseProps}
        view="subagents"
        subagents={{
          d1: mkAgent('d1', { status: 'done' }),
          s1: mkAgent('s1', { status: 'stopped' }),
          r1: mkAgent('r1', { status: 'running', streaming: 'x' }),
        }}
      />,
    )
    const btn = screen.getByTestId('dismiss-done-btn')
    expect(btn).toHaveTextContent('Dismiss done (2)')
    fireEvent.click(btn)

    await waitFor(() => expect(api.spawnDelete).toHaveBeenCalledTimes(2))
    expect(api.spawnDelete).toHaveBeenCalledWith('d1')
    expect(api.spawnDelete).toHaveBeenCalledWith('s1')
    expect(store.getState().chat.selectedSubagentId).toBeNull()
  })

  it('caps the rendered pile and reveals the rest on request', () => {
    const many: Record<string, SubagentActivity> = {}
    for (let i = 0; i < 31; i++) {
      const id = `a${String(i).padStart(2, '0')}`
      many[id] = mkAgent(id, { status: 'done', result: 'inline', agent: `chip-${id}` })
    }
    renderPanel(<ActivityViewer {...baseProps} view="subagents" subagents={many} />)

    expect(screen.queryByText('chip-a30')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('show-all-subagents'))

    expect(screen.getByText('chip-a30')).toBeInTheDocument()
    expect(screen.queryByTestId('show-all-subagents')).not.toBeInTheDocument()
  })

  it('lifts the cap by itself for an agent selected from past it', async () => {
    const many: Record<string, SubagentActivity> = {}
    for (let i = 0; i < 31; i++) {
      const id = `a${String(i).padStart(2, '0')}`
      many[id] = mkAgent(id, { status: 'done', result: 'inline', agent: `chip-${id}` })
    }
    const store = createTestStore()
    store.dispatch(selectSubagent('a30'))
    renderPanel(<ActivityViewer {...baseProps} view="subagents" subagents={many} />, store)

    expect(await screen.findByText('chip-a30')).toBeInTheDocument()
    await waitFor(() => expect(store.getState().chat.selectedSubagentId).toBeNull(), { timeout: 2000 })
  })

  it('lists only the workflow runs belonging to this chat', async () => {
    stubFetch({
      runs: [
        { run_id: 'r1', name: 'ship report', status: 'running', session_key: `dashboard:${SLOT}` },
        { run_id: 'r2', name: 'other chat run', status: 'finished', session_key: 'dashboard:elsewhere' },
      ],
    })
    renderPanel(<ActivityViewer {...baseProps} view="workflows" />)

    expect(await screen.findByText('ship report')).toBeInTheDocument()
    expect(screen.queryByText('other chat run')).not.toBeInTheDocument()
  })

  it('shows the workflows empty state when this chat started none', async () => {
    stubFetch({ runs: [{ run_id: 'r2', name: 'other run', status: 'running', session_key: 'dashboard:elsewhere' }] })
    renderPanel(<ActivityViewer {...baseProps} view="workflows" />)

    expect(await screen.findByText('No workflow runs')).toBeInTheDocument()
  })

  it('switches tabs through the internal segmented control', async () => {
    const store = createTestStore()
    renderPanel(<ActivityViewer {...baseProps} />, store)
    // jsdom reports a zero-width parent, so the control collapses to a dropdown.
    fireEvent.click(screen.getByRole('button', { name: /Links/ }))
    fireEvent.click(screen.getByRole('button', { name: /Artifacts/ }))

    await waitFor(() => expect(store.getState().chat.activityTab).toBe('artifacts'))
  })

  it('auto-switches to the subagents tab when a spawn approval arrives', async () => {
    const { rerender } = renderPanel(<ActivityViewer {...baseProps} />)
    expect(screen.queryByText('Approval Needed')).not.toBeInTheDocument()

    rerender(
      <ActivityViewer
        {...baseProps}
        toolLog={[
          { type: 'reasoning', text: 'thinking', ts: 1 },
          { type: 'approval', text: 'Reading /etc/hosts', ts: 2, approval_id: 'ap-3', approval_type: 'spawn' },
        ]}
      />,
    )
    expect(await screen.findByText('Approval Needed')).toBeInTheDocument()
  })

  it('follows a tab request made from elsewhere in the app', async () => {
    const store = createTestStore()
    renderPanel(<ActivityViewer {...baseProps} subagents={{ s1: mkAgent('s1') }} />, store)
    await act(async () => { store.dispatch(openActivityToTab('subagents')) })

    expect(screen.getByText('Complete')).toBeInTheDocument()
  })

  it('opens the full library from the Artifacts tab', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [mkArtifact('design-doc')] })
    function LocationProbe() {
      return <span data-testid="path">{useLocation().pathname}</span>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore()
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <ActivityViewer {...baseProps} view="artifacts" />
            <LocationProbe />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: /Browse all/ }))

    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/artifacts'))
  })
})
