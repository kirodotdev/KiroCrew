/**
 * The page's transcript row rendering (pages/chat/page/transcriptRendering.tsx),
 * driven directly: which turn items draw nothing, the content-column wrapper every
 * drawn row gets, the tool-group approval routing, and the off-screen measurement
 * farm's rows (same wrappers, measured with the farm flag up only while it builds).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { isValidElement, type ReactElement, type ReactNode } from 'react'

vi.mock('../api/client', () => ({
  api: {
    artifactSessionDocs: vi.fn(async () => ({ docs: [] })),
    resolveApproval: vi.fn(async () => undefined),
  },
}))

import { api } from '../api/client'
import { loadChatConfig } from './chat/ChatSettings'
import type { DisplayItem, TurnItem } from './chat/types'
import type { ChatMessage } from '../types'
import { useTranscriptRendering } from './chat/page/transcriptRendering'

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
    handleFork: vi.fn(async () => undefined), highlightTs: null, selectSessionTab: fn(), linkPreviewsOn: false, socialShareOn: false,
    connectionsUiOn: false, planTaskId: '', showRefusedPress: fn(), approve: vi.fn(async () => undefined), dismissApproval: fn(),
    revealAppInPanel: fn(), lastTextIdx: -1, regenerating: false, handleRegenerate: fn(), continuable: false, continuing: false,
    freshSession: { phase: 'idle' } as never, handleContinue: fn(), handleFreshSession: fn(), interrupted: false, openDefaultModelSetting: fn(),
    openKiroSignIn: fn(), openMemberCapabilities: fn(), openModelPickerFromError: fn(), openTurnLimitSetting: fn(), quoteWholeMessage: fn(),
    handleAsk: undefined, handleQuote: fn(), handleEditResend: fn(), editLast: null, farmPassRef: { current: false }, handleEditConsumed: fn(),
    renderUserContentCb: () => <span />, transcriptHot: false, toggleAct: fn(), handleSpeak: fn(), handleApplyPlan: vi.fn(async () => true),
    renderedDisplayItems: [], stableMsgKey: (m: ChatMessage) => String(m.ts), handleLoadEarlier: fn(), isPinned: () => false,
    handleJumpToQuote: fn(), handleTogglePinForMessage: fn(),
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

beforeEach(() => { vi.mocked(api.resolveApproval).mockClear() })

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

  it('routes a group approval to the newest unresolved permission, else to the page approve', async () => {
    const { result, p } = render()
    const withId: TurnItem = {
      kind: 'group', startIdx: 4,
      msgs: [msg('tool', '🔧 a', 't5'), msg('permission', 'p', 't6', { approval_id: 'ap-1' })],
    }
    const group = el(el(result.current.renderTurnItem(withId, 0)).props.children)
    expect(group.props.pendingPermCount).toBe(1)
    expect(group.props.count).toBe(1)
    await (group.props.onApprove as (a: string) => Promise<void>)('approve')
    expect(api.resolveApproval).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.resolveApproval).mock.calls[0][0]).toBe('ap-1')
    expect(p.dismissApproval).toHaveBeenCalledWith('ap-1')

    const noId: TurnItem = { kind: 'group', startIdx: 6, msgs: [msg('tool', '🔧 b', 't7')] }
    expect(el(el(result.current.renderTurnItem(noId, 0)).props.children).props.onApprove).toBe(p.approve)
  })
})

describe('renderFarmItem', () => {
  const items: DisplayItem[] = [
    { kind: 'turn', complete: true, items: [{ kind: 'single', msg: msg('user', 'q', 'f1'), idx: 0 }] },
    { kind: 'group', startIdx: 1, msgs: [msg('permission', 'p', 'f2')] },
    { kind: 'group', startIdx: 2, msgs: [msg('tool', '🔧 c', 'f3')] },
    { kind: 'single', msg: msg('assistant', 'a', 'f4'), idx: 3 },
  ]

  it('mirrors the transcript row wrappers, with default disclosure, and lowers the farm flag after each build', () => {
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
    expect(group.props.onApprove).toBe(p.approve)
    expect(group.props.activityOpen).toBe(false)

    const single = el(result.current.renderFarmItem(3))
    expect(single.props.className).toBe('px-4 mx-auto w-full py-1 empty:hidden')
    expect(farmPassRef.current).toBe(false)
  })
})
