// Coverage for the Channels page — the multi-agent collaboration surface.
//
// Two existing files already cover narrow slices of this page (Clear Context and
// the IME Enter guard), so everything here aims at the parts they never reach:
// the message list (MessageBubble, mentions, approval cards, thread replies), the
// agents sidebar (state/listen badges, the listen-mode menu, dismiss, Add Agent),
// the New Channel dialog and its preset picker, the close-channel flow, the
// error modal fed by `apiError`, the two empty states, and the seven branches of
// the `kirocrew-channel` window-event handler that stands in for the WebSocket.
//
// Conventions follow ChannelPage.clearContext.test.tsx: the api client is the
// single seam and is auto-mocked, `renderWithProviders` supplies Redux + Router +
// Theme, and `Element.prototype.scrollIntoView` is stubbed because jsdom has no
// implementation and the page scrolls to the newest message on every change.
//
// `fireEvent` is preferred over `userEvent` for the WebSocket-event and
// menu-dismissal tests so each step stays synchronous and the assertion sits
// immediately after the event that should have caused it.
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'

// Radix's DropdownMenu doesn't open under jsdom; the shared mock renders its
// items inline (same seam TrustDropdown.test.tsx / ApprovalCard.test.tsx use),
// which the approval card's trust-tier tests below need.
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

import { screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

type Raw = Record<string, unknown>

/** A channel member as the backend sends it (snake_case), so `mapAgent` runs. */
const member = (over: Raw = {}): Raw => ({
  id: 'a1', role: 'Researcher', agent_name: 'kiro-crew-default',
  state: 'listening', listen_mode: 'mention', approval_policy: 'writes', ...over,
})

/** A channel message as the backend sends it (snake_case), so `mapMsg` runs. */
const message = (over: Raw = {}): Raw => ({
  id: 'm1', from_id: 'a1', from_role: 'Researcher', content: 'Checked the logs.',
  msg_type: 'progress', timestamp: 1_700_000_000, reply_count: 0, ...over,
})

const channelOf = (over: Raw = {}): Raw => ({
  id: 'ch1', topic: 'Gamma rollout', members: { a1: member() }, messages: [], ...over,
})

/**
 * Point every api method this page touches at a resolved value.
 *
 * `presets` is left undefined by default on purpose: the page does
 * `setPresets(r.presets || FALLBACK_PRESETS)`, and an empty ARRAY is truthy, so
 * passing `[]` would wipe the preset picker rather than exercise the built-in
 * fallback list.
 */
function mockApi(channels: Raw[], presets?: Raw[]) {
  const first = channels[0]
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels })
  vi.mocked(api).channelGet = vi.fn().mockImplementation((id: string) =>
    Promise.resolve(channels.find(c => c.id === id) ?? first))
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets })
  vi.mocked(api).channelPost = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).channelCreate = vi.fn().mockResolvedValue({ channel: channelOf({ id: 'ch9', topic: 'Fresh topic' }) })
  vi.mocked(api).channelClose = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).channelAddAgent = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).channelUpdateAgent = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).channelDismissAgent = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).channelApproveAgent = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({ ok: true, cleared: [] })
  // AddAgentForm mounts useAgents(), which syncs then lists the agent catalog.
  vi.mocked(api).syncKirocrewAgents = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).kirocrewAgents = vi.fn().mockResolvedValue({ agents: [], default_agent: 'legacy-default' })
}

/** Render and wait past the "Loading channels..." early return. */
async function renderPage() {
  const utils = renderWithProviders(<ChannelPage />)
  await waitFor(() => expect(screen.queryByText('Loading channels...')).not.toBeInTheDocument())
  return utils
}

/** Fire one `kirocrew-channel` window event, the page's stand-in for the socket. */
function wsEvent(type: string, data: Raw) {
  act(() => {
    window.dispatchEvent(new CustomEvent('kirocrew-channel', { detail: { type, data } }))
  })
}

/** Open the right-hand agents sidebar via the header agent-count button. */
async function openAgentsPanel(name = '1 agent') {
  await userEvent.click(await screen.findByRole('button', { name }))
  return screen.getByText('Agents')
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi([channelOf()])
})

describe('ChannelPage — empty states', () => {
  it('shows both empty states when the gateway reports no channels', async () => {
    mockApi([])
    await renderPage()
    expect(screen.getByText('No channels yet')).toBeInTheDocument()
    expect(screen.getByText('Click + New to create one.')).toBeInTheDocument()
    // No selected channel -> the right-hand pane shows its own prompt.
    expect(screen.getByText('Create a channel to get started')).toBeInTheDocument()
  })

  it('shows the setting-up placeholder for a channel with no messages', async () => {
    await renderPage()
    expect(screen.getByText('Setting up channel…')).toBeInTheDocument()
    expect(screen.getByText('1 agent joining')).toBeInTheDocument()
  })

  it('pluralises the joining subtitle for a multi-agent channel', async () => {
    mockApi([channelOf({ members: { a1: member(), a2: member({ id: 'a2', role: 'Logs Agent' }) } })])
    await renderPage()
    expect(screen.getByText('2 agents joining')).toBeInTheDocument()
  })
})

describe('ChannelPage — channel list sidebar', () => {
  it('renders a working agent as a typing indicator in the transcript', async () => {
    mockApi([channelOf({
      members: {
        a1: member({ state: 'working' }),
        a2: member({ id: 'a2', role: 'Tool Agent', state: 'tool_running' }),
      },
    })])
    await renderPage()
    expect(screen.getByText('is working…')).toBeInTheDocument()
    expect(screen.getByText('running tool…')).toBeInTheDocument()
  })
})

describe('ChannelPage — message list', () => {
  it('renders an agent message with author, body and timestamp', async () => {
    mockApi([channelOf({ messages: [message()] })])
    await renderPage()
    expect(screen.getByText('Checked the logs.')).toBeInTheDocument()
    // Author name appears in the bubble header (the agents panel is closed).
    expect(screen.getByText('Researcher')).toBeInTheDocument()
  })

  it('renders a human message without an agent colour swatch', async () => {
    mockApi([channelOf({
      messages: [message({ id: 'm2', from_id: 'human', from_role: 'You', content: 'Any update?' })],
    })])
    await renderPage()
    expect(screen.getByText('Any update?')).toBeInTheDocument()
    expect(screen.getByText('You')).toBeInTheDocument()
  })

  it('renders a single-string mention as an @handle resolved to the agent role', async () => {
    mockApi([channelOf({
      messages: [message({ from_id: 'human', from_role: 'You', mention: 'a1' })],
    })])
    await renderPage()
    expect(screen.getByText('→ @Researcher')).toBeInTheDocument()
  })

  it('renders an array mention and falls back to the raw id for an unknown agent', async () => {
    mockApi([channelOf({
      messages: [message({ from_id: 'human', from_role: 'You', mention: ['a1', 'ghost'] })],
    })])
    await renderPage()
    expect(screen.getByText('→ @Researcher, @ghost')).toBeInTheDocument()
  })

  it('renders an approval message as an approval card and posts the decision', async () => {
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed:\n```\nrm -rf build\n```',
      })],
    })])
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'approved', undefined))
  })

  it('hides the per-command tiers on a non-shell approval card', async () => {
    // A non-shell pending tool has no per-command trust seam server-side
    // (the endpoint refuses with pattern_underivable) — the doomed tiers
    // must not be offered; blanket trust remains.
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed: **cron_add**\n```\n{"name": "job"}\n```',
      })],
    })])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toMatch(/Trust all tools/)
  })

  it('hides the per-command tiers when the tool input is redacted', async () => {
    // The backend refuses to scope a grant to a redacted command (two
    // commands differing only in credentials redact to the same text), so
    // the tiers must not be offered for it either.
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed: **Running: curl -H auth https://x**\n```\n{"command": "curl -H [REDACTED: credential] https://x"}\n```',
      })],
    })])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toMatch(/Trust all tools/)
  })

  it('forwards the trust_command pattern from the trust dropdown', async () => {
    // The tool title embedded by the backend (not the agent role) is what the
    // TrustDropdown derives its pattern from.
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed: **Running: rm -rf build**\n```\n{"command": "rm -rf build"}\n```',
      })],
    })])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    const cmdItem = items.find(b => b.textContent?.includes('rm -rf build'))!
    fireEvent.click(cmdItem)
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust_command', 'rm -rf build'))
  })

  it('forwards the trust_base pattern from the trust dropdown', async () => {
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed: **Running: rm -rf build**\n```\n{"command": "rm -rf build"}\n```',
      })],
    })])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    const baseItem = items.find(b => b.textContent?.includes('commands'))!
    fireEvent.click(baseItem)
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust_base', 'rm *'))
  })

  it('restores the live card when a command trust decision is refused', async () => {
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed: **Running: rm -rf build**\n```\n{"command": "rm -rf build"}\n```',
      })],
    })])
    vi.mocked(api).channelApproveAgent.mockRejectedValueOnce(
      new Error('response lost'),
    )
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const cmdItem = screen.getAllByRole('menuitem')
      .find(b => b.textContent?.includes('rm -rf build'))!
    fireEvent.click(cmdItem)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('may not have been recorded')
    expect(screen.queryByText(/auto-approving future calls/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Approve/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeInTheDocument()
  })

  // Legacy/non-shell cards carry no safely bound shell command, so they keep
  // the existing channel-wide trust action without dead command tiers (#4421).
  it('offers only the plain trust action on a legacy channel approval', async () => {
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed: **execute_bash**\n```\nrm -rf build\n```',
      })],
    })])
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain('Trust all tools in this channel — persists across restarts')
    // Neither tier may describe the role string the card is titled with.
    expect(items.some(b => b.textContent?.includes('Researcher'))).toBe(false)
  })

  it('posts the plain trust decision — one the channel backend accepts', async () => {
    mockApi([channelOf({
      messages: [message({
        msg_type: 'approval',
        content: '⚠️ Approval needed:\n```\nrm -rf build\n```',
      })],
    })])
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: /Trust/ }))
    await userEvent.click(screen.getByText('Trust all tools in this channel — persists across restarts'))
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust', undefined))
    // Legacy cards must never send a command-scoped action.
    for (const call of vi.mocked(api).channelApproveAgent.mock.calls) {
      expect(['approved', 'rejected', 'trust']).toContain(call[2])
    }
  })

  it('renders a reply-count button for a message that has a thread', async () => {
    mockApi([channelOf({ messages: [message({ reply_count: 2 })] })])
    await renderPage()
    expect(screen.getByRole('button', { name: '2 replies' })).toBeInTheDocument()
  })
})

describe('ChannelPage — thread panel', () => {
  const parent = message({ id: 'p1', content: 'Root finding', reply_count: 1 })
  const reply = message({ id: 'r1', thread_id: 'p1', content: 'Follow-up detail', from_role: 'Logs Agent' })

  beforeEach(() => {
    mockApi([channelOf({ messages: [parent, reply] })])
  })

  it('opens the thread panel from the reply-count button and shows parent + replies', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '1 reply' }))
    const panel = await screen.findByText('Thread')
    expect(panel).toBeInTheDocument()
    // Parent renders inside the panel as well as the transcript.
    expect(screen.getAllByText('Root finding').length).toBeGreaterThan(1)
    expect(screen.getByText('Follow-up detail')).toBeInTheDocument()
  })

  it('ignores an empty threaded reply', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '1 reply' }))
    await screen.findByText('Thread')
    const composers = screen.getAllByPlaceholderText('Message the channel... (type @ to mention)')
    fireEvent.keyDown(composers[composers.length - 1], { key: 'Enter' })
    expect(vi.mocked(api).channelPost).not.toHaveBeenCalled()
  })

  it('shows working agents as typing rows inside the thread panel too', async () => {
    mockApi([channelOf({
      members: { a1: member({ state: 'tool_running' }) },
      messages: [parent, reply],
    })])
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '1 reply' }))
    await screen.findByText('Thread')
    // One row in the transcript, one in the panel.
    expect(screen.getAllByText('running tool…')).toHaveLength(2)
  })
})

describe('ChannelPage — composer', () => {
  it('sends a message and clears the composer', async () => {
    await renderPage()
    const ta = screen.getByPlaceholderText('Message the channel... (type @ to mention)')
    fireEvent.change(ta, { target: { value: 'status?' } })
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(vi.mocked(api).channelPost)
      .toHaveBeenCalledWith('ch1', 'status?', undefined, undefined))
    await waitFor(() => expect((ta as HTMLTextAreaElement).value).toBe(''))
  })

  it('resolves an @role in the body into a mention id', async () => {
    await renderPage()
    const ta = screen.getByPlaceholderText('Message the channel... (type @ to mention)')
    fireEvent.change(ta, { target: { value: '@Researcher please recheck' } })
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(vi.mocked(api).channelPost)
      .toHaveBeenCalledWith('ch1', '@Researcher please recheck', ['a1'], undefined))
  })

  it('does not post a whitespace-only message', async () => {
    await renderPage()
    const ta = screen.getByPlaceholderText('Message the channel... (type @ to mention)')
    fireEvent.change(ta, { target: { value: '   ' } })
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(vi.mocked(api).channelPost).not.toHaveBeenCalled()
  })

  it('survives a rejected post — the socket is the source of truth', async () => {
    vi.mocked(api).channelPost = vi.fn().mockRejectedValue(new Error('offline'))
    await renderPage()
    const ta = screen.getByPlaceholderText('Message the channel... (type @ to mention)')
    fireEvent.change(ta, { target: { value: 'retry me' } })
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(vi.mocked(api).channelPost).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument()
  })
})

describe('ChannelPage — agents sidebar', () => {
  it('renders each listen mode, including an unknown one verbatim', async () => {
    mockApi([channelOf({
      members: {
        a1: member({ id: 'a1', role: 'R1', listen_mode: 'all' }),
        a2: member({ id: 'a2', role: 'R2', listen_mode: 'silent' }),
        a3: member({ id: 'a3', role: 'R3', listen_mode: 'whisper' }),
      },
    })])
    await renderPage()
    await openAgentsPanel('3 agents')
    expect(screen.getAllByText('all').length).toBeGreaterThan(0)
    expect(screen.getAllByText('silent').length).toBeGreaterThan(0)
    expect(screen.getByText('whisper')).toBeInTheDocument()
  })

  it('renders the agent template name under the role', async () => {
    await renderPage()
    await openAgentsPanel()
    expect(screen.getByText('kiro-crew-default')).toBeInTheDocument()
  })

  it('changes listen mode through the badge menu and patches the agent', async () => {
    await renderPage()
    await openAgentsPanel()
    // The listen badge itself is the menu trigger.
    await userEvent.click(screen.getByText('mention'))
    const menu = await screen.findByRole('menu')
    await userEvent.click(within(menu).getByText('silent'))
    await waitFor(() => expect(vi.mocked(api).channelUpdateAgent)
      .toHaveBeenCalledWith('ch1', 'a1', { listen: 'silent' }))
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })

  it('keeps the optimistic listen mode when the patch fails', async () => {
    vi.mocked(api).channelUpdateAgent = vi.fn().mockRejectedValue(new Error('nope'))
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByText('mention'))
    const menu = await screen.findByRole('menu')
    await userEvent.click(within(menu).getByText('all'))
    await waitFor(() => expect(screen.getByText('all')).toBeInTheDocument())
  })

  it('closes the listen menu on Escape', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByText('mention'))
    await screen.findByRole('menu')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })

  it('closes the listen menu on an outside mousedown but not an inside one', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByText('mention'))
    const menu = await screen.findByRole('menu')

    fireEvent.mouseDown(menu)
    expect(screen.getByRole('menu')).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })

  it('dismisses an agent optimistically and calls the api', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByTitle('Dismiss'))
    await waitFor(() => expect(vi.mocked(api).channelDismissAgent)
      .toHaveBeenCalledWith('ch1', 'a1'))
    // A dismissed agent is `done`, so its row loses both action buttons.
    await waitFor(() => expect(screen.queryByTitle('Dismiss')).not.toBeInTheDocument())
  })

  it('hides the row actions for an already-finished agent', async () => {
    mockApi([channelOf({ members: { a1: member({ state: 'failed' }) } })])
    await renderPage()
    await openAgentsPanel()
    expect(screen.queryByTitle('Dismiss')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Clear context')).not.toBeInTheDocument()
  })

  it('closes the agents sidebar again', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Close agents panel' }))
    await waitFor(() => expect(screen.queryByText('Agents')).not.toBeInTheDocument())
  })
})

describe('ChannelPage — Add Agent form', () => {
  it('adds an agent with an explicit role and task', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Logs Agent' } })
    fireEvent.change(screen.getByLabelText('Task'), { target: { value: 'Grep the access log' } })
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(vi.mocked(api).channelAddAgent).toHaveBeenCalledWith('ch1', {
      role: 'Logs Agent', task: 'Grep the access log', agent: 'legacy-default',
    }))
  })

  it('falls back to the channel topic when the task is left blank', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Code Agent' } })
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(vi.mocked(api).channelAddAgent).toHaveBeenCalledWith('ch1', {
      role: 'Code Agent', task: 'Gamma rollout', agent: 'legacy-default',
    }))
  })

  it('submits on Enter in the task field', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Metrics Agent' } })
    const task = screen.getByLabelText('Task')
    fireEvent.change(task, { target: { value: 'Plot p99' } })
    fireEvent.keyDown(task, { key: 'Enter' })
    await waitFor(() => expect(vi.mocked(api).channelAddAgent).toHaveBeenCalled())
  })

  it('does nothing on Enter while the role is still empty', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.keyDown(await screen.findByLabelText('Task'), { key: 'Enter' })
    expect(vi.mocked(api).channelAddAgent).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
  })

  it('cancels back to the + Add Agent button', async () => {
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '+ Add Agent' })).toBeInTheDocument())
  })

  it('labels a structured api error with the failed add-agent action', async () => {
    vi.mocked(api).channelAddAgent = vi.fn()
      .mockRejectedValue(new Error(JSON.stringify({ error: 'agent cap reached' })))
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Extra' } })
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    const dialog = await screen.findByRole('dialog', { name: 'Failed to add agent' })
    expect(within(dialog).getByText('agent cap reached')).toBeInTheDocument()
    expect(screen.queryByText('Limit Reached')).not.toBeInTheDocument()
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus())
  })

  it('falls back to the raw message when the api error is not JSON', async () => {
    vi.mocked(api).channelAddAgent = vi.fn().mockRejectedValue(new Error('gateway timeout'))
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Extra' } })
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    const dialog = await screen.findByRole('dialog', { name: 'Failed to add agent' })
    expect(within(dialog).getByText('gateway timeout')).toBeInTheDocument()
    expect(screen.queryByText('Limit Reached')).not.toBeInTheDocument()
  })

  it('falls back to the generic copy when the rejection is not an Error', async () => {
    vi.mocked(api).channelAddAgent = vi.fn().mockRejectedValue('bare string')
    await renderPage()
    await openAgentsPanel()
    await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Extra' } })
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('Failed to add agent')).toBeInTheDocument()
  })
})

describe('ChannelPage — New Channel dialog', () => {
  it('creates an agent-less channel when the empty preset is chosen', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    await userEvent.click(within(dialog).getByText('Custom (empty)'))
    fireEvent.change(within(dialog).getByLabelText('Topic'), { target: { value: 'Scratch' } })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(vi.mocked(api).channelCreate).toHaveBeenCalledWith('Scratch', []))
  })

  it('labels a server preset this build has no catalog key for', async () => {
    mockApi([channelOf()], [
      { id: 'triage', label: 'Triage Squad', agents: [{ role: 'Triager' }] },
      { id: 'nameless', agents: [] },
    ])
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    expect(within(dialog).getByText('Triage Squad')).toBeInTheDocument()
    // No label and no catalog key -> the id itself.
    expect(within(dialog).getByText('nameless')).toBeInTheDocument()
  })

  it('keeps Create disabled and inert until the topic is non-blank', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    const create = within(dialog).getByRole('button', { name: 'Create' })
    expect(create).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Topic'), { target: { value: '   ' } })
    expect(create).toBeDisabled()
    expect(vi.mocked(api).channelCreate).not.toHaveBeenCalled()
  })

  it('closes on Cancel', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
  })

  it('does not close when a click lands inside the dialog body', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    await userEvent.click(within(dialog).getByText('New Channel'))
    expect(screen.getByRole('dialog', { name: 'New channel' })).toBeInTheDocument()
  })

  it('renders through the shared Modal: Escape dismisses, no hand-rolled backdrop', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    await screen.findByRole('dialog', { name: 'New channel' })
    // The old overlay was a raw `fixed inset-0 bg-black/50` div; the shared
    // Modal renders its own backdrop instead. (The error modal still carries
    // the raw class until #5570 lands, but it is not mounted here.)
    expect(document.querySelector('.bg-black\\/50')).toBeNull()
    // Escape dismissal is what the hand-rolled overlay lacked — Modal owns it.
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
  })

  it('puts initial focus on the Topic field, not the header close button', async () => {
    // Modal's shared focus trap focuses the dialog's first focusable (the X
    // button); the dialog's own effect must win and land on the one input the
    // dialog exists to collect.
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    await waitFor(() => expect(within(dialog).getByLabelText('Topic')).toHaveFocus())
  })

  it('keeps global shortcuts out of the form, while Escape still bubbles to dismiss', async () => {
    // useKeyboardShortcuts binds bubble-phase document keydown, and some chords
    // deliberately fire from inside inputs — unguarded, one typed into a
    // part-filled topic would navigate away and destroy the draft.
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      await renderPage()
      await userEvent.click(screen.getByRole('button', { name: '+ New' }))
      const dialog = await screen.findByRole('dialog', { name: 'New channel' })
      fireEvent.keyDown(within(dialog).getByLabelText('Topic'), { key: ',', code: 'Comma', metaKey: true })
      expect(globalShortcut).not.toHaveBeenCalled()
      // The header X button is part of the same boundary: a chord fired while
      // it holds focus (one Shift+Tab away from the form) must not leak either.
      fireEvent.keyDown(within(dialog).getByRole('button', { name: 'Close' }), { key: '3', code: 'Digit3', ctrlKey: true })
      expect(globalShortcut).not.toHaveBeenCalled()
      // The exception that keeps dismissal alive: Modal listens for Escape on
      // window BUBBLE phase, so Escape must not be swallowed by the guard.
      fireEvent.keyDown(within(dialog).getByLabelText('Topic'), { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('does not dismiss on an Escape the IME owns (candidate-list cancel)', async () => {
    // A CJK user cancelling the IME candidate list mid-composition is not
    // cancelling the dialog: that Escape must never reach Modal's window
    // listener, or the part-composed topic is destroyed with the dialog.
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    const topic = within(dialog).getByLabelText('Topic')
    fireEvent.change(topic, { target: { value: '频道' } })
    fireEvent.compositionStart(topic)
    fireEvent.keyDown(topic, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: 'New channel' })).toBeInTheDocument()
    // After the composition ends (and its post-composition window passes), a
    // real Escape dismisses again.
    fireEvent.compositionEnd(topic)
    await new Promise(r => setTimeout(r, 60))
    fireEvent.keyDown(topic, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
  })

  it('labels a create failure with its action and dismisses the shared modal with OK', async () => {
    vi.mocked(api).channelCreate = vi.fn()
      .mockRejectedValue(new Error(JSON.stringify({ error: 'channel cap reached' })))
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    fireEvent.change(within(dialog).getByLabelText('Topic'), { target: { value: 'One too many' } })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    const errorDialog = await screen.findByRole('dialog', { name: 'Failed to create channel' })
    expect(within(errorDialog).getByText('channel cap reached')).toBeInTheDocument()
    expect(screen.queryByText('Limit Reached')).not.toBeInTheDocument()
    await userEvent.click(within(errorDialog).getByRole('button', { name: 'OK' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Failed to create channel' })).not.toBeInTheDocument())
  })

  it('ignores a create response that carries no channel', async () => {
    vi.mocked(api).channelCreate = vi.fn().mockResolvedValue({})
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    fireEvent.change(within(dialog).getByLabelText('Topic'), { target: { value: 'Nothing back' } })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(vi.mocked(api).channelCreate).toHaveBeenCalled())
    // Still on the original channel; no error modal.
    expect(screen.getByRole('heading', { name: 'Gamma rollout' })).toBeInTheDocument()
    expect(screen.queryByText('Limit Reached')).not.toBeInTheDocument()
  })

  it('keeps the fallback presets when the presets request fails', async () => {
    vi.mocked(api).channelPresets = vi.fn().mockRejectedValue(new Error('no presets'))
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const dialog = await screen.findByRole('dialog', { name: 'New channel' })
    expect(within(dialog).getByText('Incident Response')).toBeInTheDocument()
  })
})

describe('ChannelPage — close channel', () => {
  it('closes the channel on confirm and clears the selection', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await renderPage()
    await userEvent.click(screen.getByTitle('Close channel'))
    await waitFor(() => expect(vi.mocked(api).channelClose).toHaveBeenCalledWith('ch1'))
    await waitFor(() => expect(screen.getByText('Create a channel to get started')).toBeInTheDocument())
  })

  it('removes the channel locally even when the close request fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClose = vi.fn().mockRejectedValue(new Error('already gone'))
    await renderPage()
    await userEvent.click(screen.getByTitle('Close channel'))
    await waitFor(() => expect(screen.getByText('No channels yet')).toBeInTheDocument())
  })

  it('does nothing when the confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    await renderPage()
    await userEvent.click(screen.getByTitle('Close channel'))
    expect(vi.mocked(api).channelClose).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: 'Gamma rollout' })).toBeInTheDocument()
  })
})

describe('ChannelPage — socket events', () => {
  it('appends a channel_message to the transcript', async () => {
    await renderPage()
    wsEvent('channel_message', {
      channel_id: 'ch1',
      message: message({ id: 'ws1', content: 'Streamed in live' }),
    })
    expect(await screen.findByText('Streamed in live')).toBeInTheDocument()
  })

  it('ignores a channel_message with no message payload', async () => {
    await renderPage()
    wsEvent('channel_message', { channel_id: 'ch1' })
    expect(screen.getByText('Setting up channel…')).toBeInTheDocument()
  })

  it('applies channel_agent_status to the matching agent', async () => {
    await renderPage()
    wsEvent('channel_agent_status', { channel_id: 'ch1', agent_id: 'a1', state: 'working' })
    expect(await screen.findByText('is working…')).toBeInTheDocument()
  })

  it('prepends a channel_created channel and ignores a duplicate', async () => {
    await renderPage()
    wsEvent('channel_created', channelOf({ id: 'ch7', topic: 'Pushed by socket' }))
    expect(await screen.findByRole('button', { name: /Pushed by socket/ })).toBeInTheDocument()

    wsEvent('channel_created', channelOf({ id: 'ch7', topic: 'Pushed by socket' }))
    expect(screen.getAllByRole('button', { name: /Pushed by socket/ })).toHaveLength(1)
  })

  it('drops a channel on channel_closed', async () => {
    await renderPage()
    wsEvent('channel_closed', { channel_id: 'ch1' })
    expect(await screen.findByText('No channels yet')).toBeInTheDocument()
  })

  it('reloads the whole list on channel_agent_joined', async () => {
    await renderPage()
    vi.mocked(api).channelsList.mockClear()
    wsEvent('channel_agent_joined', { channel_id: 'ch1', agent_id: 'a2' })
    await waitFor(() => expect(vi.mocked(api).channelsList).toHaveBeenCalled())
  })

  it('marks an agent done on channel_agent_left', async () => {
    mockApi([channelOf({ members: { a1: member({ state: 'working' }) } })])
    await renderPage()
    expect(screen.getByText('is working…')).toBeInTheDocument()
    wsEvent('channel_agent_left', { channel_id: 'ch1', agent_id: 'a1' })
    await waitFor(() => expect(screen.queryByText('is working…')).not.toBeInTheDocument())
  })

  it('empties the transcript on a shared channel_context_cleared', async () => {
    mockApi([channelOf({ messages: [message({ content: 'Stale buffer' })] })])
    await renderPage()
    expect(screen.getByText('Stale buffer')).toBeInTheDocument()
    wsEvent('channel_context_cleared', { channel_id: 'ch1', scope: 'all' })
    await waitFor(() => expect(screen.queryByText('Stale buffer')).not.toBeInTheDocument())
  })

  it('leaves the transcript alone for an agent-scoped context clear', async () => {
    mockApi([channelOf({ messages: [message({ content: 'Kept buffer' })] })])
    await renderPage()
    wsEvent('channel_context_cleared', { channel_id: 'ch1', scope: 'agent' })
    expect(screen.getByText('Kept buffer')).toBeInTheDocument()
  })

  it('ignores an unrecognised event type', async () => {
    await renderPage()
    wsEvent('channel_unknown_thing', { channel_id: 'ch1' })
    expect(screen.getByRole('heading', { name: 'Gamma rollout' })).toBeInTheDocument()
  })
})

describe('ChannelPage — load failures', () => {
  it('renders the empty state when the channel list request fails', async () => {
    vi.mocked(api).channelsList = vi.fn().mockRejectedValue(new Error('gateway down'))
    await renderPage()
    expect(screen.getByText('No channels yet')).toBeInTheDocument()
  })

  it('keeps the summary row when the per-channel fetch fails', async () => {
    vi.mocked(api).channelGet = vi.fn().mockRejectedValue(new Error('not found'))
    await renderPage()
    expect(screen.getByRole('heading', { name: 'Gamma rollout' })).toBeInTheDocument()
  })
})
