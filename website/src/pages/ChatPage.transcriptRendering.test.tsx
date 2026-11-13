/**
 * The page's transcript row rendering (pages/chat/page/transcriptRendering.tsx),
 * driven directly: which turn items draw nothing, the content-column wrapper every
 * drawn row gets, the tool-group approval routing, and the off-screen measurement
 * farm's rows (same wrappers, measured with the farm flag up only while it builds).
 */
import { afterEach, describe, it, expect, onTestFinished, vi } from 'vitest'
import { act, cleanup, render as renderDom, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, defaultScheduler, notifyManager, useQuery } from '@tanstack/react-query'
import { isValidElement, type ReactElement, type ReactNode } from 'react'

vi.mock('../api/client', () => ({
  api: {
    artifactSessionDocs: vi.fn(async () => ({ docs: [] })),
    kasLoginStatus: vi.fn(),
  },
}))

import { api, type KasLoginStatus } from '../api/client'
import { ApiError } from '../api/apiError'
import { loadChatConfig } from './chat/ChatSettings'
import type { DisplayItem, TurnItem } from './chat/types'
import type { ChatMessage } from '../types'
import { useTranscriptRendering } from './chat/page/transcriptRendering'
import { UserMessage, AssistantMessage } from './chat'
import { KIRO_SIGN_IN_NOTICE_POLL_MS } from '../components/KiroSignInNotice'
import { __resetGatewayConnectedForTests, setGatewayConnected } from '../utils/errorReport'

type Props = Parameters<typeof useTranscriptRendering>[0]

const msg = (role: string, content: string, ts: string, meta?: Record<string, unknown>) =>
  ({ role, content, ts, cls: '', ...(meta ? { meta } : {}) }) as unknown as ChatMessage

function props(over: Partial<Props> = {}): Props {
  const fn = () => vi.fn()
  return {
    embedded: false, mode: '', popout: false, filteredSlots: [], connected: true, activeSlot: 'slot-a',
    voiceRecoverySlot: null, appToolCallIds: new Set(), mcpAppPanel: false, messages: [], messagesRef: { current: [] },
    slotRunning: false, toolDisclosure: {}, setToolDisclosureFor: fn(), slotState: 'idle' as Props['slotState'], activityOpen: false,
    slotHasMore: false, slotOldestIndex: 0, cursorIsForActiveSlot: true, loadingOlder: false, chatConfig: loadChatConfig(),
    activeSlotRemoteBound: false, automationId: undefined, setAutomationOpen: fn(), handleArtifactOpen: vi.fn(async () => undefined),
    handleFileOpen: fn() as never, handleFolderOpen: fn(), handleOpenDiff: fn(), handleSubagentPanelOpen: fn(), sessionTitles: new Map(),
    handleFork: vi.fn(async () => false), highlightTs: null, selectSessionTab: fn(), linkPreviewsOn: false, socialShareOn: false,
    connectionsUiOn: false, planTaskId: '', showRefusedPress: fn(), decideRow: vi.fn(async () => undefined),
    revealAppInPanel: fn(), lastTextIdx: -1, regenerating: false, handleRegenerate: fn(), replacingMids: new Set<string>(), continuable: false, continuing: false,
    freshSession: { phase: 'idle' } as never, handleContinue: fn(), handleFreshSession: fn(), interrupted: false, openDefaultModelSetting: fn(),
    openKiroSignIn: fn(), openMemberCapabilities: fn(), openModelPickerFromError: fn(), openTurnLimitSetting: fn(), quoteWholeMessage: fn(),
    handleAsk: undefined, handleQuote: fn(), handleEditResend: fn(), editLast: null, farmPassRef: { current: false }, handleEditConsumed: fn(),
    renderUserContentCb: () => <span />, transcriptHot: false, toggleAct: fn(), handleSpeak: fn(), handleApplyPlan: vi.fn(async () => true),
    renderedDisplayItems: [], stableMsgKey: (m: ChatMessage) => String(m.ts), handleLoadEarlier: fn(), isPinned: () => false,
    handleJumpToQuote: fn(), handleTogglePinForMessage: fn(), handleSteerAction: fn(),
    steerActionPending: {}, steerActionRefusals: {}, steerActionRequestFailures: {},
    ...over,
  }
}

function render(over: Partial<Props> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const p = props(over)
  const { result } = renderHook(() => useTranscriptRendering(p), { wrapper })
  return { result, p }
}

const el = (node: ReactNode) => {
  expect(isValidElement(node)).toBe(true)
  return node as ReactElement<{ className?: string; style?: { maxWidth?: string }; children?: ReactNode; [k: string]: unknown }>
}


describe('renderTurnItem', () => {
  it('draws nothing for a hidden tool completion and for a hidden invisible-only assistant row', () => {
    const { result } = render()
    const done: TurnItem = { kind: 'single', msg: msg('tool', '✅ done', 't1'), idx: 0 }
    expect(result.current.renderTurnItem(done, 0)).toBeNull()
    const invisible: TurnItem = { kind: 'single', msg: msg('assistant', '\u200B', 't2'), idx: 1 }
    expect(result.current.renderTurnItem(invisible, 0)).toBeNull()
  })

  it('wraps a drawn row in the content column, keyed by the turn lead', () => {
    const { result } = render()
    const row = el(result.current.renderTurnItem({ kind: 'single', msg: msg('user', 'hello', 't3'), idx: 2 }, 0))
    expect(row.props.className).toBe('px-4 mx-auto w-full py-1 empty:hidden')
    expect(row.props.style?.maxWidth).toBe('var(--mc-content-width, 900px)')
    expect(row.props['data-content-column']).toBe('')
    expect(row.key).toContain('t3')
  })

  it('draws a permissions-only group as an empty wrapper', () => {
    const { result } = render()
    const group: TurnItem = { kind: 'group', msgs: [msg('permission', 'p', 't4')], startIdx: 3 }
    expect(el(result.current.renderTurnItem(group, 0)).props.children).toBeNull()
  })

  it('decides a group approval through the newest unresolved permission row, and a group with none names no row', async () => {
    const { result, p } = render()
    const pending = { approval_id: 'ap-1', mid: 'm-6' }
    const withId: TurnItem = {
      kind: 'group', startIdx: 4,
      msgs: [msg('tool', '🔧 a', 't5'), msg('permission', 'p', 't6', pending)],
    }
    const group = el(el(result.current.renderTurnItem(withId, 0)).props.children)
    expect(group.props.pendingPermCount).toBe(1)
    expect(group.props.count).toBe(1)
    await (group.props.onApprove as (a: string) => Promise<void>)('approve')
    // Bound to the row the group shows, never a bare id or a bare slot.
    expect(p.decideRow).toHaveBeenCalledTimes(1)
    expect(p.decideRow).toHaveBeenCalledWith(pending, 'approve')

    const noId: TurnItem = { kind: 'group', startIdx: 6, msgs: [msg('tool', '🔧 b', 't7')] }
    await (el(el(result.current.renderTurnItem(noId, 0)).props.children).props.onApprove as (a: string) => Promise<void>)('approve')
    expect(p.decideRow).toHaveBeenLastCalledWith(undefined, 'approve')
  })
})

/** The first element under `node` whose type is `type`, walking `props.children`. */
function findByType(node: ReactNode, type: unknown): ReactElement<Record<string, unknown>> | null {
  if (Array.isArray(node)) {
    for (const child of node) { const hit = findByType(child, type); if (hit) return hit }
    return null
  }
  if (!isValidElement(node)) return null
  const e = node as ReactElement<Record<string, unknown>>
  if (e.type === type) return e
  return findByType(e.props.children as ReactNode, type)
}

describe('message row handlers', () => {
  const user = msg('user', 'question', 'r1', { mid: 'm-user' })
  const reply = msg('assistant', 'answer', 'r2', { mid: 'm-reply' })

  function mount() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const first = props({ lastTextIdx: 1, messages: [user, reply], messagesRef: { current: [user, reply] } })
    const hook = renderHook((p: Props) => useTranscriptRendering(p), { wrapper, initialProps: first })
    const rows = () => ({
      user: findByType(hook.result.current.renderMessage(0, user), UserMessage)!,
      reply: findByType(hook.result.current.renderMessage(1, reply), AssistantMessage)!,
    })
    return { hook, first, rows }
  }

  it('keeps every row handler identity when a chunk flush rebuilds the page callbacks', () => {
    const { hook, first, rows } = mount()
    const before = rows()
    // A flush replaces `messages`, which mints new handleEditResend and
    // handleRegenerate; the pin and quote sources may change with it.
    hook.rerender({
      ...first, messages: [user, reply], handleEditResend: vi.fn(), handleRegenerate: vi.fn(),
      quoteWholeMessage: vi.fn(), handleTogglePinForMessage: vi.fn(), handleJumpToQuote: vi.fn(),
    })
    const after = rows()
    for (const key of ['onEditResend', 'onTogglePin', 'onQuoteMessage', 'onJumpToQuote']) {
      expect(after.user.props[key], key).toBeTypeOf('function')
      expect(after.user.props[key], key).toBe(before.user.props[key])
    }
    for (const key of ['onTogglePin', 'onQuoteMessage', 'onRegenerate']) {
      expect(after.reply.props[key], key).toBeTypeOf('function')
      expect(after.reply.props[key], key).toBe(before.reply.props[key])
    }
  })

  it('calls the latest page callback with the row it was made for', () => {
    const { hook, first, rows } = mount()
    const latest = {
      handleEditResend: vi.fn(), handleRegenerate: vi.fn(), quoteWholeMessage: vi.fn(),
      handleTogglePinForMessage: vi.fn(), handleJumpToQuote: vi.fn(),
    }
    hook.rerender({ ...first, ...latest })
    const { user: u, reply: r } = rows()
    ;(u.props.onTogglePin as () => void)()
    ;(r.props.onTogglePin as () => void)()
    ;(u.props.onQuoteMessage as (s: string) => void)('shown')
    ;(r.props.onQuoteMessage as (s: string) => void)('shown reply')
    ;(u.props.onEditResend as (i: number, ts: string, c: string) => void)(0, 'r1', 'edited')
    ;(u.props.onJumpToQuote as (q: unknown) => void)({ ts: 'r0' })
    ;(r.props.onRegenerate as () => void)()
    expect(latest.handleTogglePinForMessage.mock.calls).toEqual([['m-user', 'r1', 'user', 'question'], ['m-reply', 'r2', 'assistant', 'answer']])
    expect(latest.quoteWholeMessage.mock.calls).toEqual([['user', 'shown', 'r1', 'm-user'], ['assistant', 'shown reply', 'r2', 'm-reply']])
    expect(latest.handleEditResend).toHaveBeenCalledWith(0, 'r1', 'edited')
    expect(latest.handleJumpToQuote).toHaveBeenCalledWith({ ts: 'r0' })
    expect(latest.handleRegenerate).toHaveBeenCalledTimes(1)
    expect(first.handleTogglePinForMessage).not.toHaveBeenCalled()
  })

  it('forks with the earlier messages and opens the model picker only on the chat the fork opened', async () => {
    const { hook, first, rows } = mount()
    const handleFork = vi.fn(async () => true)
    const openModelPickerFromError = vi.fn()
    hook.rerender({ ...first, handleFork, openModelPickerFromError })
    const forkWithModel = () => (rows().reply.props.onForkWithModel as (i: number, mid?: string) => Promise<void>)(1, 'm-reply')
    await forkWithModel()
    expect(handleFork).toHaveBeenCalledWith(1, 'm-reply', 'head')
    expect(openModelPickerFromError).toHaveBeenCalledTimes(1)
    // The person opened another chat while the fork ran: the picker would act on that one.
    handleFork.mockResolvedValueOnce(false)
    await forkWithModel()
    expect(handleFork).toHaveBeenCalledTimes(2)
    expect(openModelPickerFromError).toHaveBeenCalledTimes(1)
  })

  it('gives a replaced row object handlers over its new content', () => {
    const { hook, first, rows } = mount()
    const before = rows().user
    const edited = msg('user', 'question, edited', 'r1', { mid: 'm-user' })
    const row = findByType(hook.result.current.renderMessage(0, edited), UserMessage)!
    expect(row.props.onTogglePin).not.toBe(before.props.onTogglePin)
    ;(row.props.onTogglePin as () => void)()
    expect(first.handleTogglePinForMessage).toHaveBeenCalledWith('m-user', 'r1', 'user', 'question, edited')
  })
})

describe('renderFarmItem', () => {
  const items: DisplayItem[] = [
    { kind: 'turn', complete: true, items: [{ kind: 'single', msg: msg('user', 'q', 'f1'), idx: 0 }] },
    { kind: 'group', startIdx: 1, msgs: [msg('permission', 'p', 'f2')] },
    { kind: 'group', startIdx: 2, msgs: [msg('tool', '🔧 c', 'f3')] },
    { kind: 'single', msg: msg('assistant', 'a', 'f4'), idx: 3 },
  ]

  it('mirrors the transcript row wrappers, with default disclosure, and lowers the farm flag after each build', async () => {
    const farmPassRef = { current: false }
    const { result, p } = render({ renderedDisplayItems: items, farmPassRef })
    expect(result.current.renderFarmItem(99)).toBeNull()

    const turn = el(result.current.renderFarmItem(0))
    expect(turn.props.disclosureKey).toBe('farm-0')
    expect(turn.props.disclosure).toBeUndefined()
    expect(turn.props.renderItem).toBe(result.current.renderTurnItem)
    expect(farmPassRef.current).toBe(false)

    expect(el(result.current.renderFarmItem(1)).props.children).toBeNull()

    const group = el(el(result.current.renderFarmItem(2)).props.children)
    expect(group.props.disclosureKey).toBe('farm-ctg-2')
    // The farm replica names no row, so a press on it could only be refused.
    await (group.props.onApprove as (a: string) => Promise<void>)('approve')
    expect(p.decideRow).toHaveBeenCalledWith(undefined, 'approve')
    expect(group.props.activityOpen).toBe(false)

    const single = el(result.current.renderFarmItem(3))
    expect(single.props.className).toBe('px-4 mx-auto w-full py-1 empty:hidden')
    expect(farmPassRef.current).toBe(false)
  })
})

describe('a signed-out row on the dashboard and in an embed or popout frame (#34690)', () => {
  const user = msg('user', 'What can you do for me today?', 'k1')
  const signedOut = msg('error', 'Not signed in to Kiro.', 'k2', { mid: 'm-out', kind: 'auth_required', notice: 'kas_signed_out' })
  const signedIn: KasLoginStatus = {
    authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
    expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true, refresh_rejected: false, usable: true,
  }
  const readFailed = () => new ApiError(500, 'Could not read the stored sign-in.', '{"error":"Could not read the stored sign-in.","code":"token_store_failed"}')

  afterEach(() => {
    vi.useRealTimers()
    cleanup()
  })

  function renderRow(over: Partial<Props>) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const rows = [user, signedOut]
    const p = props({ interrupted: true, continuable: true, messages: rows, messagesRef: { current: rows }, ...over })
    function Row() {
      const row = useTranscriptRendering(p).renderMessage(1, signedOut)
      // The row's read as this same render saw it, so a test can wait on a
      // drawn failure before asserting what the row left out.
      const read = useQuery({ queryKey: ['kas-login', 'm-out'], queryFn: api.kasLoginStatus, enabled: false })
      return <><span data-testid="row-read" data-status={read.status} />{row}</>
    }
    renderDom(<QueryClientProvider client={client}><Row /></QueryClientProvider>)
    return client
  }

  it('withdraws Resume and shows the failure once a later sign-in read fails', async () => {
    const status = vi.mocked(api.kasLoginStatus)
    status.mockResolvedValue(signedIn)
    const client = renderRow({ embedded: true })

    await waitFor(() => expect(screen.getByTestId('error-card-continue')).toBeInTheDocument())
    expect(screen.queryByTestId('error-card-sign-in-check-failed')).toBeNull()

    status.mockRejectedValue(readFailed())
    await client.refetchQueries({ queryKey: ['kas-login'] })
    await waitFor(() => expect(screen.getByTestId('error-card-sign-in-check-failed')).toBeInTheDocument())
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
  })

  it('leaves a failed read on the dashboard to the app-level notice, keeping Sign in and withdrawing Resume', async () => {
    const status = vi.mocked(api.kasLoginStatus)
    status.mockRejectedValue(readFailed())
    renderRow({ embedded: false, popout: false })

    await waitFor(() => expect(screen.getByTestId('row-read')).toHaveAttribute('data-status', 'error'))
    expect(screen.getByTestId('error-card-sign-in')).toBeInTheDocument()
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    expect(screen.queryByTestId('error-card-sign-in-check-failed')).toBeNull()
  })

  it('leaves a read that failed while the gateway was down to the offline state, then re-reads once it is back', async () => {
    const status = vi.mocked(api.kasLoginStatus)
    status.mockRejectedValue(new TypeError('Failed to fetch'))
    setGatewayConnected(false)
    onTestFinished(() => __resetGatewayConnectedForTests())
    renderRow({ embedded: true })

    await waitFor(() => expect(screen.getByTestId('row-read')).toHaveAttribute('data-status', 'error'))
    expect(screen.queryByTestId('error-card-sign-in-check-failed')).toBeNull()
    expect(screen.queryByTestId('error-card-continue')).toBeNull()

    status.mockResolvedValue(signedIn)
    act(() => setGatewayConnected(true))
    expect(await screen.findByTestId('error-card-continue')).toBeInTheDocument()
    expect(screen.queryByTestId('error-card-sign-in-check-failed')).toBeNull()
  })

  it('re-reads on its own clock, so a frame signed in from elsewhere gains Resume', async () => {
    // Fake timers go on before the render so the query's interval is a fake
    // one, and no waitFor runs under them. Notifications run on microtasks, so
    // the `act` around each advance flushes the re-render.
    vi.useFakeTimers({ shouldAdvanceTime: false })
    notifyManager.setScheduler(queueMicrotask)
    onTestFinished(() => notifyManager.setScheduler(defaultScheduler))
    const status = vi.mocked(api.kasLoginStatus)
    status.mockResolvedValue({ ...signedIn, authenticated: false, usable: false })
    const client = renderRow({ embedded: true })

    await act(() => vi.advanceTimersByTimeAsync(0))
    expect(client.getQueryState(['kas-login', 'm-out'])?.status).toBe('success')
    expect(screen.queryByTestId('error-card-continue')).toBeNull()

    status.mockResolvedValue(signedIn)
    await act(() => vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_NOTICE_POLL_MS))
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
  })
})
