import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act, renderHook } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import { __resetBottomTerminal, useBottomTerminal } from '../../hooks/useBottomTerminal'
import { sseSlots } from '../../store/dashboardSlice'

/* "Run in terminal" from a crewmate chat on the Crewmates page. The page hosts
 * the request (it used to have no receiver, so the button timed out): the run
 * opens a dock terminal in the member slot's project, sends the command once
 * the shell is ready, and answers the button. Only requests stamped with this
 * page's scope are its own. */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    // reuse_current off: the run path, not the copy path.
    kirocrewConfig: vi.fn(() => Promise.resolve({})),
  },
}))

vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
const term = vi.hoisted(() => ({
  ready: new Map<string, () => void>(),
  send: vi.fn(() => true),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
  onTerminalReady: (id: string, cb: () => void) => { term.ready.set(id, cb); return () => term.ready.delete(id) },
  sendToTerminalSession: term.send,
  getTerminalShell: () => undefined,
  getTerminalFenceShells: () => ({}),
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))

/* The thread pane renders a REAL Run-in-terminal button, so the request carries
 * whatever scope the page provides around its chat. */
vi.mock('../../components/ChatPane', async () => {
  const { default: RunInTerminalBtn } = await import('../../components/RunInTerminalBtn')
  return {
    default: ({ slotKey }: { slotKey: string }) => (
      <div data-testid="chat-pane-stub">
        {slotKey}
        <RunInTerminalBtn code="npm test" lang="bash" />
      </div>
    ),
  }
})

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: false, slot_key: '', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

async function openThread() {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row()], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: true })
  const view = renderWithProviders(<MembersPage />)
  fireEvent.click(await screen.findByText('oncall'))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
  // Let the reuse-current read settle: while it is pending the handler copies.
  await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
  await act(async () => {})
  return view
}

function clickRun() {
  fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
  fireEvent.click(screen.getByRole('button', { name: /^Run( anyway)?$/ }))
}

const dockTabs = () => renderHook(() => useBottomTerminal()).result.current.tabs

beforeEach(() => {
  vi.clearAllMocks()
  term.ready.clear()
  localStorage.clear()
  __resetPanelTabs()
  __resetBottomTerminal()
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true })
})

describe('MembersPage Run in terminal', () => {
  it('opens a dock terminal in the member slot\'s project, runs the command, and answers the button', async () => {
    const { store } = await openThread()
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0, project: '/srv/oncall' }] as never))
    })
    clickRun()
    const tabs = dockTabs()
    expect(tabs).toHaveLength(1)
    expect(tabs[0].cwd).toBe('/srv/oncall')
    // The shell comes up; the command is sent to that tab's session.
    act(() => { term.ready.get(tabs[0].id)?.() })
    expect(term.send).toHaveBeenCalledWith(tabs[0].id, expect.stringContaining('npm test'))
    expect(await screen.findByLabelText('Sent to terminal')).toBeInTheDocument()
  })

  it('refuses at once, with a notice, while the slot record (and so the cwd) has not arrived', async () => {
    await openThread()
    const results: unknown[] = []
    const onResult = (e: Event) => { results.push((e as CustomEvent).detail) }
    window.addEventListener('mc:run-in-terminal-result', onResult)
    try {
      clickRun()
      expect(results).toEqual([expect.objectContaining({ ok: false })])
      expect(dockTabs()).toHaveLength(0)
      // Said through the page's ErrorNotice, not only the button's glyph.
      expect(await screen.findByTestId('member-panel-action-error')).toHaveTextContent(/working directory hasn't loaded yet/)
    } finally {
      window.removeEventListener('mc:run-in-terminal-result', onResult)
    }
  })

  it('leaves a request without its scope alone, so another host can own it', async () => {
    const { store } = await openThread()
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0, project: '/srv/oncall' }] as never))
    })
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { code: 'npm test', reqId: 'r1' } }))
      window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { code: 'npm test', reqId: 'r2', scope: 'someone-else' } }))
    })
    expect(dockTabs()).toHaveLength(0)
  })
})
