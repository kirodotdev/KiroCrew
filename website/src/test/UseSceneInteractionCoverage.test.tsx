// Behaviour coverage for `useSceneInteraction` — the shared hover / mini-thread
// layer every Kiro Crew "Worlds" scene mounts on top of its pixel canvas.
//
// `agentStatusLine.test.ts` already pins the exported status-line helper. This
// file drives the hook itself through a tiny harness canvas, mocking exactly
// three seams: the api client, the typed dispatch, and the router's navigate.
// Everything in between — hit-testing, the tooltip, the thread popover and its
// load / empty / error states, the composer (send vs steer), the approval bar,
// the "New session" sign, header dragging, and the 2s live-refresh poll — is
// the shipped code.
//
// happy-dom reports a zero-size rect for every element, so the canvas rect is
// stubbed 1:1 with the logical scene size: a mouse at clientX/Y N lands on
// scene coordinate N.

import React, { useRef } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'

import {
  useSceneInteraction,
  messagePreview,
  type SceneAgent,
  type SceneTooltipTheme,
} from '../hooks/useSceneInteraction'
import type { AgentSource } from '../hooks/useAgentSync'
import { i18nT } from '../i18n/t'

const apiMocks = vi.hoisted(() => ({
  chatSlotDetail: vi.fn(),
  sendChat: vi.fn(),
  steerChat: vi.fn(),
  resolveApproval: vi.fn(),
  createChatSlot: vi.fn(),
}))

const navigateSpy = vi.hoisted(() => vi.fn())
const dispatchSpy = vi.hoisted(() => vi.fn())

vi.mock('../api/client', () => ({ api: apiMocks }))

// The hook only needs the typed dispatch and the switchSlot action creator;
// mocking both keeps the real store (and the whole chat slice) out of the run.
vi.mock('../store', () => ({ useAppDispatch: () => dispatchSpy }))
vi.mock('../store/chatSlice', () => ({
  switchSlot: (key: string) => ({ type: 'chat/switchSlot', payload: key }),
}))

vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateSpy,
}))

const W = 600
const H = 400

const THEME: SceneTooltipTheme = { active: 'Grinding PRs', idle: 'Waiting for CR approval' }

const ALPHA: SceneAgent = {
  id: 'slot-a', name: 'Alpha', x: 100, y: 100, running: true, detail: '3 msgs', kind: 'slot',
  color: '#8cf',
}
const BETA: SceneAgent = {
  id: 'slot-b', name: 'Beta', x: 300, y: 200, running: false, detail: '1 msgs', kind: 'slot',
}
const NIGHTLY: SceneAgent = {
  id: 'cron-c', name: 'Nightly', x: 500, y: 300, running: false, detail: 'every 15m', kind: 'cron',
}

const SCOUT: SceneAgent = {
  id: 'spawn-d', name: 'Scout', x: 500, y: 100, running: true, detail: 'running', kind: 'spawn',
}

const AGENTS = [ALPHA, BETA, NIGHTLY, SCOUT]

const source = (over: Partial<AgentSource> & { id: string }): AgentSource => ({
  name: 'Alpha', label: 'legacy-default', kind: 'slot', running: false, detail: '3 msgs', ...over,
})

interface HarnessProps {
  agents?: SceneAgent[]
  sources?: AgentSource[]
  extraLine?: (agent: SceneAgent) => React.ReactNode
  /** Leave the canvas ref unattached to exercise the "no canvas" guard. */
  detached?: boolean
}

function Harness({ agents = AGENTS, sources, extraLine, detached }: HarnessProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const agentsRef = useRef<SceneAgent[]>(agents)
  agentsRef.current = agents
  const { canvasProps, tooltipEl } = useSceneInteraction(
    canvasRef, agentsRef, W, H, THEME, 10, extraLine, sources,
  )
  return (
    <div data-testid="scene-wrap" style={{ position: 'relative' }}>
      <canvas
        data-testid="scene"
        ref={detached ? undefined : canvasRef}
        width={W}
        height={H}
        {...canvasProps}
      />
      {tooltipEl}
    </div>
  )
}

const renderScene = (props: HarnessProps = {}) => render(<Harness {...props} />)

const canvasEl = () => screen.getByTestId('scene') as HTMLCanvasElement

/** Drain pending microtasks (all api mocks resolve immediately). */
const flush = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

const hover = (x: number, y: number) => {
  fireEvent.mouseMove(canvasEl(), { clientX: x, clientY: y })
}

const clickAt = async (x: number, y: number) => {
  fireEvent.click(canvasEl(), { clientX: x, clientY: y })
  await flush()
}

const rect = (): DOMRect => ({
  left: 0, top: 0, right: W, bottom: H, width: W, height: H, x: 0, y: 0,
  toJSON: () => ({}),
}) as DOMRect

/** happy-dom has no 2D context; MiniGhost needs one to paint its pixel rows. */
const stubCtx = () => ({
  clearRect: vi.fn(), fillRect: vi.fn(), fillStyle: '',
}) as unknown as CanvasRenderingContext2D

beforeEach(() => {
  vi.useFakeTimers()
  HTMLCanvasElement.prototype.getBoundingClientRect = rect
  HTMLCanvasElement.prototype.getContext = vi.fn(stubCtx) as unknown as HTMLCanvasElement['getContext']
  apiMocks.chatSlotDetail.mockResolvedValue({ messages: [] })
  apiMocks.sendChat.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
  apiMocks.steerChat.mockResolvedValue({})
  apiMocks.resolveApproval.mockResolvedValue({})
  apiMocks.createChatSlot.mockResolvedValue({})
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('useSceneInteraction — hit-testing and tooltip', () => {
  it('shows nothing before the pointer enters an agent', () => {
    renderScene()
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
  })

  it('shows name, working state and the scene mood for a running slot', () => {
    renderScene()
    hover(100, 100)
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Working · 3 msgs')).toBeInTheDocument()
    expect(screen.getByText(THEME.active)).toBeInTheDocument()
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.click_for_thread'))).toBeInTheDocument()
  })

  it('shows the idle mood line for a stopped slot', () => {
    renderScene()
    hover(300, 200)
    expect(screen.getByText('Idle · 1 msgs')).toBeInTheDocument()
    expect(screen.getByText(THEME.idle)).toBeInTheDocument()
  })

  it('omits the click-for-thread affordance for a cron agent', () => {
    renderScene()
    hover(500, 300)
    expect(screen.getByText('Cron · every 15m')).toBeInTheDocument()
    expect(
      screen.queryByText(i18nT('hooks.useSceneInteraction.click_for_thread')),
    ).not.toBeInTheDocument()
  })

  it('reports the subagent state line for a spawn agent', () => {
    renderScene()
    hover(500, 100)
    expect(screen.getByText('Subagent · running')).toBeInTheDocument()
    expect(
      screen.queryByText(i18nT('hooks.useSceneInteraction.click_for_thread')),
    ).not.toBeInTheDocument()
  })

  it('omits the detail separator and reports a running cron', () => {
    renderScene({
      agents: [{ ...ALPHA, detail: '' }, { ...NIGHTLY, x: 300, y: 100, running: true }],
    })
    hover(100, 100)
    expect(screen.getByText('Working')).toBeInTheDocument()
    hover(300, 100)
    expect(screen.getByText('Cron · running')).toBeInTheDocument()
  })

  it('hit-tests within the radius and misses outside it', () => {    renderScene()
    hover(105, 95)
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    hover(140, 100)
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
  })

  it('clears the tooltip when the pointer leaves the canvas', () => {
    renderScene()
    hover(100, 100)
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    fireEvent.mouseLeave(canvasEl())
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
  })

  it('turns the cursor into a pointer only over clickable slots', () => {
    renderScene()
    hover(100, 100)
    expect(canvasEl().style.cursor).toBe('pointer')
    hover(500, 300)
    expect(canvasEl().style.cursor).toBe('default')
  })

  it('renders the scene-specific extra line', () => {
    renderScene({ extraLine: a => <div>orbit {a.name}</div> })
    hover(100, 100)
    expect(screen.getByText('orbit Alpha')).toBeInTheDocument()
  })

  it('adds a needs-approval line when the live source is blocked', () => {
    renderScene({ sources: [source({ id: 'slot-a', pendingApproval: { tool: 'shell', requestId: 'r1' } })] })
    hover(100, 100)
    expect(
      screen.getByText(new RegExp(i18nT('hooks.useSceneInteraction.needs_approval'))),
    ).toBeInTheDocument()
    expect(screen.getByText(/shell/)).toBeInTheDocument()
  })

  it('adds a truncated last-message line from the live source', () => {
    const long = 'a'.repeat(80)
    renderScene({ sources: [source({ id: 'slot-a', lastMessage: long })] })
    hover(100, 100)
    expect(screen.getByText(messagePreview(long))).toBeInTheDocument()
  })

  it('ignores pointer input while the canvas ref is unattached', async () => {
    renderScene({ detached: true })
    hover(100, 100)
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
    await clickAt(100, 100)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(apiMocks.chatSlotDetail).not.toHaveBeenCalled()
  })
})

describe('useSceneInteraction — thread popover lifecycle', () => {
  it('opens on a slot click, shows the loading line, then the messages', async () => {
    let resolveDetail: (v: { messages: { role: string; content: string }[] }) => void = () => {}
    apiMocks.chatSlotDetail.mockReturnValue(
      new Promise(res => { resolveDetail = res }),
    )
    renderScene()
    fireEvent.click(canvasEl(), { clientX: 100, clientY: 100 })

    expect(screen.getByText(i18nT('hooks.useSceneInteraction.loading_thread'))).toBeInTheDocument()
    expect(apiMocks.chatSlotDetail).toHaveBeenCalledWith('a', 24)

    await act(async () => {
      resolveDetail({ messages: [{ role: 'assistant', content: 'shipped it' }] })
      await Promise.resolve()
    })

    expect(
      screen.queryByText(i18nT('hooks.useSceneInteraction.loading_thread')),
    ).not.toBeInTheDocument()
    expect(screen.getByText('shipped it')).toBeInTheDocument()
  })

  it('labels the popover for the agent it belongs to', async () => {
    renderScene()
    await clickAt(100, 100)
    expect(
      screen.getByRole('dialog', {
        name: i18nT('hooks.useSceneInteraction.recent_messages_for', { name: 'Alpha' }),
      }),
    ).toBeInTheDocument()
  })

  it('drops streaming roles, collapses duplicates and keeps the last eight', async () => {
    apiMocks.chatSlotDetail.mockResolvedValue({
      messages: [
        { role: 'user', content: 'oldest-user' },
        { role: 'chunk', content: 'streamed-fragment' },
        { role: 'assistant', content: 'oldest-reply' },
        { role: 'assistant', content: 'oldest-reply' },
        { role: 'done', content: '' },
        { role: 'user', content: 'u2' }, { role: 'assistant', content: 'a2' },
        { role: 'user', content: 'u3' }, { role: 'assistant', content: 'a3' },
        { role: 'user', content: 'u4' }, { role: 'assistant', content: 'a4' },
        { role: 'user', content: 'u5' }, { role: 'assistant', content: 'a5' },
      ],
    })
    renderScene()
    await clickAt(100, 100)

    expect(screen.queryByText('streamed-fragment')).not.toBeInTheDocument()
    expect(screen.queryByText('oldest-user')).not.toBeInTheDocument()
    expect(screen.queryByText('oldest-reply')).not.toBeInTheDocument()
    expect(screen.getAllByText('you')).toHaveLength(4)
    expect(screen.getAllByText('kiro')).toHaveLength(4)
    expect(screen.getByText('a5')).toBeInTheDocument()
  })

  it('shows an empty-thread line when the slot has no history', async () => {
    renderScene()
    await clickAt(100, 100)
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.no_messages_yet'))).toBeInTheDocument()
  })

  it('shows an error line when the history request fails', async () => {
    apiMocks.chatSlotDetail.mockRejectedValue(new Error('offline'))
    renderScene()
    await clickAt(100, 100)
    expect(
      screen.getByText(i18nT('hooks.useSceneInteraction.couldn_t_load_messages')),
    ).toBeInTheDocument()
  })

  it('toggles the popover off when the same agent is clicked again', async () => {
    renderScene()
    await clickAt(100, 100)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await clickAt(100, 100)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('retargets the popover when a different slot is clicked', async () => {
    renderScene()
    await clickAt(100, 100)
    await clickAt(300, 200)
    expect(screen.getByText('Beta')).toBeInTheDocument()
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
  })

  it('never opens for a cron agent and closes an open popover', async () => {
    renderScene()
    await clickAt(100, 100)
    await clickAt(500, 300)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('closes on empty-space clicks', async () => {
    renderScene()
    await clickAt(100, 100)
    await clickAt(20, 20)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('hides the tooltip while the popover is open', async () => {
    renderScene()
    hover(100, 100)
    expect(screen.getByText(THEME.active)).toBeInTheDocument()
    await clickAt(100, 100)
    expect(screen.queryByText(THEME.active)).not.toBeInTheDocument()
  })

  it('closes on Escape and ignores other keys', async () => {
    renderScene()
    await clickAt(100, 100)
    fireEvent.keyDown(document, { key: 'a' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('closes on an outside pointer press but not on one inside it', async () => {
    renderScene()
    await clickAt(100, 100)
    fireEvent.pointerDown(screen.getByRole('dialog'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.pointerDown(canvasEl())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('closes from the header close button', async () => {
    renderScene()
    await clickAt(100, 100)
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.close_thread_view') }),
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('switches slot and routes to the chat page from Open chat', async () => {
    renderScene()
    await clickAt(100, 100)
    fireEvent.click(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.open_chat') }),
    )
    expect(dispatchSpy).toHaveBeenCalledWith({ type: 'chat/switchSlot', payload: 'a' })
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })

  it('shows a working footer line when the live source is mid-turn', async () => {
    renderScene({ sources: [source({ id: 'slot-a', running: true })] })
    await clickAt(100, 100)
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.kiro_is_working'))).toBeInTheDocument()
  })

  it('omits the working footer line for an idle source', async () => {
    renderScene({ sources: [source({ id: 'slot-a', running: false })] })
    await clickAt(100, 100)
    expect(
      screen.queryByText(i18nT('hooks.useSceneInteraction.kiro_is_working')),
    ).not.toBeInTheDocument()
  })

  it('treats a history payload with no messages field as an empty thread', async () => {
    apiMocks.chatSlotDetail.mockResolvedValue({})
    renderScene()
    await clickAt(100, 100)
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.no_messages_yet'))).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.no_messages_yet'))).toBeInTheDocument()
  })

  it('discards a history response that lands after the popover retargets', async () => {
    let resolveAlpha: (v: { messages: { role: string; content: string }[] }) => void = () => {}
    apiMocks.chatSlotDetail
      .mockImplementationOnce(() => new Promise(res => { resolveAlpha = res }))
      .mockResolvedValue({ messages: [{ role: 'assistant', content: 'beta reply' }] })

    renderScene()
    fireEvent.click(canvasEl(), { clientX: 100, clientY: 100 })
    await clickAt(300, 200)
    expect(screen.getByText('beta reply')).toBeInTheDocument()

    await act(async () => {
      resolveAlpha({ messages: [{ role: 'assistant', content: 'alpha reply' }] })
      await Promise.resolve()
    })

    expect(screen.queryByText('alpha reply')).not.toBeInTheDocument()
    expect(screen.getByText('beta reply')).toBeInTheDocument()
  })

  it('discards a failed history response that lands after the popover retargets', async () => {
    let rejectAlpha: (e: Error) => void = () => {}
    apiMocks.chatSlotDetail
      .mockImplementationOnce(() => new Promise((_res, rej) => { rejectAlpha = rej }))
      .mockResolvedValue({ messages: [{ role: 'assistant', content: 'beta reply' }] })

    renderScene()
    fireEvent.click(canvasEl(), { clientX: 100, clientY: 100 })
    await clickAt(300, 200)

    await act(async () => {
      rejectAlpha(new Error('too late'))
      await Promise.resolve()
    })

    expect(
      screen.queryByText(i18nT('hooks.useSceneInteraction.couldn_t_load_messages')),
    ).not.toBeInTheDocument()
    expect(screen.getByText('beta reply')).toBeInTheDocument()
  })

  it('discards a poll response that lands after the popover closes', async () => {
    let resolvePoll: (v: { messages: { role: string; content: string }[] }) => void = () => {}
    apiMocks.chatSlotDetail
      .mockResolvedValueOnce({ messages: [] })
      .mockImplementationOnce(() => new Promise(res => { resolvePoll = res }))

    renderScene()
    await clickAt(100, 100)
    await act(async () => { vi.advanceTimersByTime(2000) })
    fireEvent.keyDown(document, { key: 'Escape' })

    await act(async () => {
      resolvePoll({ messages: [{ role: 'assistant', content: 'late reply' }] })
      await Promise.resolve()
    })

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('late reply')).not.toBeInTheDocument()
  })

  it('still opens when the environment offers no 2D drawing context', async () => {
    HTMLCanvasElement.prototype.getContext =
      vi.fn(() => null) as unknown as HTMLCanvasElement['getContext']
    renderScene()
    await clickAt(100, 100)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})

describe('useSceneInteraction — composer', () => {
  const messageBox = () =>
    screen.getByRole('textbox', { name: i18nT('hooks.useSceneInteraction.message', { name: 'Alpha' }) })

  const sendButton = () =>
    screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.send_message') })

  it('sends to an idle agent, echoes the message and settles back to idle', async () => {
    renderScene({ sources: [source({ id: 'slot-a', running: false })] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    expect(apiMocks.sendChat).toHaveBeenCalledWith('ship it', 'a')
    expect(apiMocks.steerChat).not.toHaveBeenCalled()
    expect(screen.getByText('ship it')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.message_sent') }),
    ).toBeInTheDocument()
    expect(messageBox()).toHaveValue('')

    await act(async () => { vi.advanceTimersByTime(1600) })
    expect(sendButton()).toBeInTheDocument()
  })

  it('steers a running agent instead of starting a turn', async () => {
    renderScene({ sources: [source({ id: 'slot-a', running: true })] })
    await clickAt(100, 100)

    expect(messageBox()).toHaveAttribute(
      'placeholder', i18nT('hooks.useSceneInteraction.steer_this_agent'),
    )
    fireEvent.change(messageBox(), { target: { value: 'stop there' } })
    fireEvent.click(sendButton())
    await flush()

    expect(apiMocks.steerChat).toHaveBeenCalledWith('stop there', 'a')
    expect(apiMocks.sendChat).not.toHaveBeenCalled()
  })

  it('uses the plain message placeholder with no live source', async () => {
    renderScene({ sources: [] })
    await clickAt(100, 100)
    expect(messageBox()).toHaveAttribute(
      'placeholder', i18nT('hooks.useSceneInteraction.message_this_agent'),
    )
  })

  it('offers a retry when the send fails', async () => {
    apiMocks.sendChat.mockRejectedValue(new Error('boom'))
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.retry'))).toBeInTheDocument()
    expect(messageBox()).toHaveValue('ship it')
  })

  it('sends on Enter, ignores Shift+Enter and ignores a blank draft', async () => {
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.keyDown(messageBox(), { key: 'Enter' })
    await flush()
    expect(apiMocks.sendChat).not.toHaveBeenCalled()

    fireEvent.change(messageBox(), { target: { value: 'via keyboard' } })
    fireEvent.keyDown(messageBox(), { key: 'Enter', shiftKey: true })
    await flush()
    expect(apiMocks.sendChat).not.toHaveBeenCalled()

    fireEvent.keyDown(messageBox(), { key: 'Enter' })
    await flush()
    expect(apiMocks.sendChat).toHaveBeenCalledWith('via keyboard', 'a')
  })

  it('disables the send button until the draft has content', async () => {
    renderScene({ sources: [] })
    await clickAt(100, 100)
    expect(sendButton()).toBeDisabled()
    fireEvent.change(messageBox(), { target: { value: '  ' } })
    expect(sendButton()).toBeDisabled()
    fireEvent.change(messageBox(), { target: { value: 'x' } })
    expect(sendButton()).not.toBeDisabled()
  })

  it('clears the draft when the popover retargets to another agent', async () => {
    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'half typed' } })
    await clickAt(300, 200)
    expect(
      screen.getByRole('textbox', {
        name: i18nT('hooks.useSceneInteraction.message', { name: 'Beta' }),
      }),
    ).toHaveValue('')
  })

  it('reaches failed when the server refuses the send — a 4xx/5xx resolves, not rejects (#4198)', async () => {
    // The receipt is the verdict: `{ok:false}` from a refused send RESOLVES.
    // Before the receipt check this fell through to 'sent', asserting the
    // opposite of what happened for precisely the errors that matter.
    apiMocks.sendChat.mockResolvedValue({ ok: false, json: vi.fn().mockResolvedValue({ ok: false, error: 'slot agent mismatch' }) })
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
    // The payload is handed back, and the refused message is NOT echoed into
    // the mini thread as if it were delivered.
    expect(messageBox()).toHaveValue('ship it')
    expect(screen.queryByText('ship it')).not.toBeInTheDocument()
  })

  it('treats an unreadable NON-2xx receipt as a failure, not a success', async () => {
    // An HTML error page (proxy 502) makes json() reject, and the status says
    // no on its own — the send never landed and must not report 'sent'.
    apiMocks.sendChat.mockResolvedValue({ ok: false, json: vi.fn().mockRejectedValue(new Error('not json')) })
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
    expect(messageBox()).toHaveValue('ship it')
  })

  it('gives an unreadable 2xx receipt NEITHER verdict, and keeps the payload out of the box (#4217)', async () => {
    // The counterpart to the case above: the status ACCEPTED the request, so
    // only the answer is mangled and the turn may be running. 'failed' would
    // hand the payload back and invite a retry that duplicates a delivered
    // turn — the composer drops back to idle instead, saying nothing.
    apiMocks.sendChat.mockResolvedValue({ ok: true, json: vi.fn().mockRejectedValue(new Error('unexpected end of JSON input')) })
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    expect(
      screen.queryByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).not.toBeInTheDocument()
    // Neither is it claimed as sent, or echoed into the mini thread.
    expect(
      screen.queryByRole('button', { name: i18nT('hooks.useSceneInteraction.message_sent') }),
    ).not.toBeInTheDocument()
    expect(messageBox()).toHaveValue('')
    expect(screen.queryByText('ship it')).not.toBeInTheDocument()
  })

  it('treats a queued acceptance as sent', async () => {
    apiMocks.sendChat.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ queued: true }) })
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.message_sent') }),
    ).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(1600) })
  })

  it('keeps text typed while awaiting the receipt when the send is accepted', async () => {
    // The payload is cleared at send START; everything typed after that is
    // newer work. An acceptance-time clear would erase it to acknowledge an
    // older send.
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'first' } })
    fireEvent.click(sendButton())
    expect(messageBox()).toHaveValue('')
    fireEvent.change(messageBox(), { target: { value: 'second thought' } })

    await act(async () => {
      finishSend({ ok: true, json: () => Promise.resolve({ ok: true }) })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('second thought')
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.message_sent') }),
    ).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(1600) })
  })

  it('appends the failed payload after a REPLACED draft instead of clobbering it', async () => {
    // The regression class PR #4180 hit: recovering the older payload must not
    // overwrite the newer text typed while the POST was in flight — and must
    // not drop the payload either. Newer text first, failed payload appended.
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'first thought' } })
    fireEvent.click(sendButton())
    fireEvent.change(messageBox(), { target: { value: 'newer thought' } })

    await act(async () => {
      finishSend({ ok: false, json: () => Promise.resolve({ ok: false }) })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('newer thought first thought')
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
  })

  it('appends the payload even when the new draft contains its words', async () => {
    // "do not deploy yet" CONTAINS "deploy", but the failed payload is still
    // a distinct retryable message — any containment heuristic here guesses
    // about intent and silently drops it. Equality-only: append.
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'deploy' } })
    fireEvent.click(sendButton())
    fireEvent.change(messageBox(), { target: { value: 'do not deploy yet' } })

    await act(async () => {
      finishSend({ ok: false, json: () => Promise.resolve({ ok: false }) })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('do not deploy yet deploy')
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
  })

  it('does not duplicate a payload the user deliberately retyped', async () => {
    // The draft is cleared at send start; if it exactly equals the payload
    // again, the user retyped it — appending would double it.
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    fireEvent.change(messageBox(), { target: { value: 'ship it' } })

    await act(async () => {
      finishSend({ ok: false, json: () => Promise.resolve({ ok: false }) })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('ship it')
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
  })

  it('still restores a payload that is only a substring of the new draft', async () => {
    // 'go' is a substring of 'ongoing work' but NOT a whole occurrence: the
    // replacement draft does not contain the payload as typed, so treating it
    // as already-restored would silently lose the message the Retry state is
    // telling the user to retry.
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'go' } })
    fireEvent.click(sendButton())
    fireEvent.change(messageBox(), { target: { value: 'ongoing work' } })

    await act(async () => {
      finishSend({ ok: false, json: () => Promise.resolve({ ok: false }) })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('ongoing work go')
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
  })

  it('appends the payload after a punctuation-extended draft', async () => {
    // 'go' → 'go, please' typed after send start is NEW text, not the
    // payload: equality-only restore appends the retryable 'go' after it.
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'go' } })
    fireEvent.click(sendButton())
    fireEvent.change(messageBox(), { target: { value: 'go, please' } })

    await act(async () => {
      finishSend({ ok: false, json: () => Promise.resolve({ ok: false }) })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('go, please go')
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).toBeInTheDocument()
  })

  it('does not flag the new target when a send fails after the popover retargets', async () => {
    // The failure belongs to the agent the message was typed to. After a
    // retarget the composer is the NEW agent's: no failed flag, no payload
    // spliced into its draft.
    let rejectSend: (e: Error) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise((_res, rej) => { rejectSend = rej }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'in flight' } })
    fireEvent.click(sendButton())
    await clickAt(300, 200)

    await act(async () => {
      rejectSend(new Error('boom'))
      await Promise.resolve()
      await Promise.resolve()
    })

    const betaBox = screen.getByRole('textbox', {
      name: i18nT('hooks.useSceneInteraction.message', { name: 'Beta' }),
    })
    expect(betaBox).toHaveValue('')
    expect(
      screen.queryByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).not.toBeInTheDocument()
  })

  it('leaves a reopened composer alone when a stale outcome lands for the same agent', async () => {
    // The agent id alone cannot tell "same composer" from "closed and
    // reopened": a stale failure matching only on id would splice the old
    // payload into the reopened composer's fresh draft (or a stale success
    // would erase it). The per-open epoch makes the outcome a no-op.
    let rejectSend: (e: Error) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise((_res, rej) => { rejectSend = rej }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'in flight' } })
    fireEvent.click(sendButton())
    // Close (same agent click toggles off) and reopen the SAME popover.
    await clickAt(100, 100)
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'fresh thought' } })

    await act(async () => {
      rejectSend(new Error('boom'))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(messageBox()).toHaveValue('fresh thought')
    expect(
      screen.queryByRole('button', { name: i18nT('hooks.useSceneInteraction.retry_sending_message') }),
    ).not.toBeInTheDocument()
  })

  it('surfaces the framed server reason as a visible status line', async () => {
    apiMocks.sendChat.mockResolvedValue({ ok: false, json: vi.fn().mockResolvedValue({ ok: false, error: 'slot agent mismatch' }) })
    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'ship it' } })
    fireEvent.click(sendButton())
    await flush()

    // Visible (role=status), not a hover-only tooltip, and FRAMED so the raw
    // backend reason reads as a refused send rather than an agent error.
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent(
      i18nT('pages.chatPage.send_failed_with_error', { error: 'slot agent mismatch' }) as string,
    )
  })

  it('lets a stale sent-reset expire without clobbering a later in-flight send', async () => {
    // Send 1 is accepted and arms the 1.5s sent→idle reset; send 2 starts
    // before it fires. An unconditional reset would flip send 2's 'sending'
    // back to 'idle', re-enabling submit while its request is in flight.
    let finishSecond: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat
      .mockResolvedValueOnce({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
      .mockImplementationOnce(() => new Promise(res => { finishSecond = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)

    fireEvent.change(messageBox(), { target: { value: 'first' } })
    fireEvent.click(sendButton())
    await flush()
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.message_sent') }),
    ).toBeInTheDocument()

    // Second send while the first's reset timer is still pending (Enter — the
    // button's accessible name is still 'message sent' at this instant).
    fireEvent.change(messageBox(), { target: { value: 'second' } })
    fireEvent.keyDown(messageBox(), { key: 'Enter' })
    await act(async () => { vi.advanceTimersByTime(1600) })

    // Still 'sending' — the stale timer must not have reset it to idle.
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.sending_message') }),
    ).toBeInTheDocument()

    await act(async () => {
      finishSecond({ ok: true, json: () => Promise.resolve({ ok: true }) })
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(
      screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.message_sent') }),
    ).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(1600) })
  })

  it('drops the optimistic echo when the popover retargets mid-send', async () => {
    let finishSend: (v: { ok: boolean; json: () => Promise<unknown> }) => void = () => {}
    apiMocks.sendChat.mockImplementation(() => new Promise(res => { finishSend = res }))

    renderScene({ sources: [] })
    await clickAt(100, 100)
    fireEvent.change(messageBox(), { target: { value: 'in flight' } })
    fireEvent.click(sendButton())
    await clickAt(300, 200)

    await act(async () => {
      finishSend({ ok: true, json: () => Promise.resolve({ ok: true }) })
      await Promise.resolve()
      await Promise.resolve()
      vi.advanceTimersByTime(1600)
    })

    expect(screen.queryByText('in flight')).not.toBeInTheDocument()
  })
})

describe('useSceneInteraction — live refresh poll', () => {
  it('re-reads the thread every two seconds while the popover is open', async () => {
    renderScene()
    await clickAt(100, 100)
    expect(apiMocks.chatSlotDetail).toHaveBeenCalledTimes(1)

    apiMocks.chatSlotDetail.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'fresh reply' }],
    })
    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()

    expect(apiMocks.chatSlotDetail).toHaveBeenCalledTimes(2)
    expect(screen.getByText('fresh reply')).toBeInTheDocument()
  })

  it('re-appends a just-sent message the server has not persisted yet', async () => {
    apiMocks.chatSlotDetail.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'earlier reply' }],
    })
    renderScene({ sources: [] })
    await clickAt(100, 100)

    const box = screen.getByRole('textbox', {
      name: i18nT('hooks.useSceneInteraction.message', { name: 'Alpha' }),
    })
    fireEvent.change(box, { target: { value: 'not persisted yet' } })
    fireEvent.click(screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.send_message') }))
    await flush()

    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()

    expect(screen.getByText('not persisted yet')).toBeInTheDocument()
  })

  it('does not duplicate a sent message once the server returns it', async () => {
    renderScene({ sources: [] })
    await clickAt(100, 100)

    const box = screen.getByRole('textbox', {
      name: i18nT('hooks.useSceneInteraction.message', { name: 'Alpha' }),
    })
    fireEvent.change(box, { target: { value: 'persisted' } })
    fireEvent.click(screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.send_message') }))
    await flush()

    apiMocks.chatSlotDetail.mockResolvedValue({
      messages: [{ role: 'user', content: 'persisted' }],
    })
    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()

    expect(screen.getAllByText('persisted')).toHaveLength(1)
  })

  it('swallows a failed refresh and keeps the popover open', async () => {
    renderScene()
    await clickAt(100, 100)
    apiMocks.chatSlotDetail.mockRejectedValue(new Error('flaky'))
    await act(async () => { vi.advanceTimersByTime(2000) })
    await flush()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('stops polling once the popover closes', async () => {
    renderScene()
    await clickAt(100, 100)
    fireEvent.keyDown(document, { key: 'Escape' })
    await act(async () => { vi.advanceTimersByTime(6000) })
    await flush()
    expect(apiMocks.chatSlotDetail).toHaveBeenCalledTimes(1)
  })
})

describe('useSceneInteraction — pending approval bar', () => {
  const withApproval = () => ({
    sources: [source({ id: 'slot-a', pendingApproval: { tool: 'execute_bash', requestId: 'req-9' } })],
  })

  it('names the blocked tool and approves it', async () => {
    renderScene(withApproval())
    await clickAt(100, 100)

    expect(screen.getByText(/execute_bash/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.approve') }))
    await flush()

    expect(apiMocks.resolveApproval).toHaveBeenCalledWith('req-9', 'approve')
    expect(screen.queryByText(i18nT('hooks.useSceneInteraction.failed'))).not.toBeInTheDocument()
  })

  it('rejects from the deny button', async () => {
    renderScene(withApproval())
    await clickAt(100, 100)
    fireEvent.click(screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.deny') }))
    await flush()
    expect(apiMocks.resolveApproval).toHaveBeenCalledWith('req-9', 'reject')
  })

  it('surfaces a failed resolution inline', async () => {
    apiMocks.resolveApproval.mockRejectedValue(new Error('gone'))
    renderScene(withApproval())
    await clickAt(100, 100)
    fireEvent.click(screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.approve') }))
    await flush()
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.failed'))).toBeInTheDocument()
  })

  it('omits the bar when nothing is waiting on the user', async () => {
    renderScene({ sources: [source({ id: 'slot-a' })] })
    await clickAt(100, 100)
    expect(
      screen.queryByRole('button', { name: i18nT('hooks.useSceneInteraction.approve') }),
    ).not.toBeInTheDocument()
  })
})

describe('useSceneInteraction — New session sign', () => {
  const signName = i18nT('hooks.useSceneInteraction.new_session')

  it('is absent when the scene wires no live sources', () => {
    renderScene()
    expect(screen.queryByRole('button', { name: signName })).not.toBeInTheDocument()
  })

  it('creates a slot and shows a summoning label while in flight', async () => {
    let finish: () => void = () => {}
    apiMocks.createChatSlot.mockReturnValue(new Promise<void>(res => { finish = res }))
    renderScene({ sources: [] })

    fireEvent.click(screen.getByRole('button', { name: signName }))
    expect(screen.getByText(i18nT('hooks.useSceneInteraction.summoning'))).toBeInTheDocument()

    await act(async () => { finish(); await Promise.resolve() })
    expect(apiMocks.createChatSlot).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: signName })).not.toBeDisabled()
  })

  it('re-enables the sign when slot creation fails', async () => {
    apiMocks.createChatSlot.mockRejectedValue(new Error('no room'))
    renderScene({ sources: [] })
    fireEvent.click(screen.getByRole('button', { name: signName }))
    await flush()
    expect(screen.getByRole('button', { name: signName })).not.toBeDisabled()
  })

  it('is disabled with an occupancy hint once the world is full', () => {
    const full = Array.from({ length: 8 }, (_, i) => source({ id: 'slot-' + i }))
    renderScene({ sources: full })
    const sign = screen.getByRole('button', { name: signName })
    expect(sign).toBeDisabled()
    expect(sign).toHaveAttribute('title', i18nT('hooks.useSceneInteraction.all_slots_are_occupied'))
  })
})

describe('useSceneInteraction — draggable popover', () => {
  const header = () => screen.getByRole('dialog').firstElementChild as HTMLElement

  const boundPopover = () => {
    const wrap = screen.getByTestId('scene-wrap')
    Object.defineProperty(wrap, 'clientWidth', { value: W, configurable: true })
    Object.defineProperty(wrap, 'clientHeight', { value: H, configurable: true })
    Object.defineProperty(screen.getByRole('dialog'), 'offsetParent', {
      value: wrap, configurable: true,
    })
  }

  it('anchors the popover next to the clicked agent', async () => {
    renderScene()
    await clickAt(100, 100)
    const dialog = screen.getByRole('dialog')
    expect(dialog.style.left).toBe('112px')
    expect(dialog.style.top).toBe('40px')
  })

  it('follows the pointer when the header is dragged', async () => {
    renderScene()
    await clickAt(100, 100)
    boundPopover()

    fireEvent.pointerDown(header(), { clientX: 200, clientY: 200 })
    fireEvent.pointerMove(window, { clientX: 240, clientY: 230 })
    const dialog = screen.getByRole('dialog')
    expect(dialog.style.left).toBe('152px')
    expect(dialog.style.top).toBe('70px')

    fireEvent.pointerUp(window)
    fireEvent.pointerMove(window, { clientX: 400, clientY: 400 })
    expect(dialog.style.left).toBe('152px')
  })

  it('clamps the drag inside the scene bounds', async () => {
    renderScene()
    await clickAt(100, 100)
    boundPopover()

    fireEvent.pointerDown(header(), { clientX: 200, clientY: 200 })
    fireEvent.pointerMove(window, { clientX: 9000, clientY: 9000 })
    const dialog = screen.getByRole('dialog')
    expect(dialog.style.left).toBe(W - 90 + 'px')
    expect(dialog.style.top).toBe(H - 40 + 'px')

    fireEvent.pointerMove(window, { clientX: -9000, clientY: -9000 })
    expect(dialog.style.left).toBe('-180px')
    expect(dialog.style.top).toBe('0px')
    fireEvent.pointerUp(window)
  })

  it('drags unbounded when the popover has no positioned ancestor', async () => {
    renderScene()
    await clickAt(100, 100)
    fireEvent.pointerDown(header(), { clientX: 200, clientY: 200 })
    fireEvent.pointerMove(window, { clientX: 9000, clientY: 9000 })
    expect(screen.getByRole('dialog').style.left).toBe(112 + 8800 + 'px')
    fireEvent.pointerUp(window)
  })

  it('leaves header buttons clickable instead of starting a drag', async () => {
    renderScene()
    await clickAt(100, 100)
    boundPopover()
    const openChat = screen.getByRole('button', { name: i18nT('hooks.useSceneInteraction.open_chat') })

    fireEvent.pointerDown(openChat, { clientX: 200, clientY: 200 })
    fireEvent.pointerMove(window, { clientX: 400, clientY: 400 })
    expect(screen.getByRole('dialog').style.left).toBe('112px')
  })

  it('resets a dragged position when the popover retargets', async () => {
    renderScene()
    await clickAt(100, 100)
    boundPopover()
    fireEvent.pointerDown(header(), { clientX: 200, clientY: 200 })
    fireEvent.pointerMove(window, { clientX: 260, clientY: 200 })
    fireEvent.pointerUp(window)
    expect(screen.getByRole('dialog').style.left).toBe('172px')

    await clickAt(300, 200)
    expect(screen.getByRole('dialog').style.left).toBe('312px')
  })
})

describe('messagePreview', () => {
  it('collapses runs of whitespace onto one line', () => {
    expect(messagePreview('a \n\t b   c')).toBe('a b c')
  })

  it('leaves a short message untouched', () => {
    expect(messagePreview('short')).toBe('short')
  })

  it('truncates past the limit with an ellipsis', () => {
    expect(messagePreview('x'.repeat(70))).toBe('x'.repeat(63) + '…')
  })

  it('honours a caller-supplied limit', () => {
    expect(messagePreview('abcdef', 4)).toBe('abc…')
  })
})
