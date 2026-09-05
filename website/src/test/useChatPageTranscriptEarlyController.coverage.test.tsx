import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

// This suite invokes only the early export.  Stub the later controller's visual
// dependencies so importing the shared module does not initialise Markdown or
// widget renderers that are irrelevant to scroll/pin geometry.
vi.mock('../api/client', () => ({ api: {} }))
vi.mock('../components/FileCard', () => ({ FileCard: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/MessageErrorBoundary', () => ({ default: () => null }))
vi.mock('../hooks/useChatNavigation', () => ({ useChatNavigation: () => ({}) }))
vi.mock('../hooks/useChatPins', () => ({ useChatPins: () => ({}) }))
vi.mock('../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))
vi.mock('../hooks/SearchHighlightContext', () => ({ MessageSearchScope: ({ children }: { children?: unknown }) => children }))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({ useVirtualChat: () => ({}) }))
vi.mock('../store', () => ({ store: {}, useAppSelector: vi.fn() }))
vi.mock('../store/chatSlice', () => ({
  isSupersededPagingRejection: () => false,
  loadOlderMessages: vi.fn(),
  openActivityPanel: vi.fn(),
}))
vi.mock('../pages/chat', () => ({
  AssistantMessage: () => null,
  UserMessage: () => null,
}))
vi.mock('../pages/chat/ChatFooter', () => ({ useStreamIdle: () => true }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ErrorCard', () => ({ ErrorCard: () => null }))
vi.mock('../pages/chat/NoticeCard', () => ({ default: () => null }))
vi.mock('../pages/chat/RecoveryCard', () => ({ default: () => null, resolveInjectCard: () => null }))
vi.mock('../pages/chat/StopEventCard', () => ({ default: () => null }))
vi.mock('../pages/chat/SubagentCompletionCard', () => ({ default: () => null, headline: () => '' }))
vi.mock('../pages/chat/SubagentRunCard', () => ({ default: () => null, extractSpawnRunLaunch: () => null }))
vi.mock('../pages/chat/ThinkingBlock', () => ({ default: () => null }))
vi.mock('../pages/chat/ToolCallLine', () => ({ default: () => null }))
vi.mock('../pages/chat/WorkflowCompletionCard', () => ({ default: () => null, isWorkflowCompletionMessage: () => false }))
vi.mock('../pages/chat/WorkflowRunCard', () => ({ default: () => null, extractWorkflowRunId: () => null }))
vi.mock('../pages/chat/ChatPageMessageContent', () => ({
  messageRowKey: () => '',
  msgIdentityKey: () => '',
  renderUserContent: () => null,
  turnLeadKey: () => '',
  virtualKeyFor: () => '',
}))

import {
  useChatPageTranscriptEarlyController,
} from '../pages/chat/useChatPageTranscriptController'

/**
 * The early transcript controller owns DOM-driven pin geometry and the two
 * pinned-prompt jump modes.  Keep this test at that seam: a full ChatPage mount
 * hides these callbacks behind the virtualizer, while the real hook below keeps
 * the rAF/geometry behaviour under test.
 */

interface QueuedFrame {
  id: number
  cb: FrameRequestCallback
}

let frames: QueuedFrame[] = []
let nextFrameId = 1
let clock = 0
let originalRaf: typeof requestAnimationFrame
let originalCancelRaf: typeof cancelAnimationFrame
let nowSpy: ReturnType<typeof vi.spyOn>
const detachedNodes: HTMLElement[] = []

const rect = (top: number, height: number): DOMRect => ({
  top,
  bottom: top + height,
  left: 0,
  right: 0,
  width: 0,
  height,
  x: 0,
  y: top,
  toJSON: () => ({}),
}) as DOMRect

function setRect(el: HTMLElement, top: number, height: number) {
  Object.defineProperty(el, 'getBoundingClientRect', {
    configurable: true,
    value: () => rect(top, height),
  })
}

function flushFrame(at: number) {
  const frame = frames.shift()
  expect(frame, 'expected a queued animation frame').toBeDefined()
  clock = at
  act(() => { frame!.cb(at) })
}

const message = (role: string, content: string, ts: string): ChatMessage => ({
  role,
  content,
  cls: '',
  ts,
})

const single = (idx: number, role: string, content: string): DisplayItem => ({
  kind: 'single',
  idx,
  msg: message(role, content, `2026-08-30T00:00:0${idx}.000Z`),
})

function mountGeometry(rowCount: number) {
  const scroller = document.createElement('div')
  const fold = document.createElement('div')
  const card = document.createElement('div')
  scroller.append(fold, card)
  const rows = Array.from({ length: rowCount }, (_, index) => {
    const row = document.createElement('div')
    row.dataset.displayIndex = String(index)
    scroller.append(row)
    return row
  })
  document.body.append(scroller)
  detachedNodes.push(scroller)

  setRect(scroller, 0, 400)
  setRect(fold, 100, 0)
  setRect(card, 104, 70)
  rows.forEach((row, index) => {
    // index 3 is the first row below the handoff line; index 4 is the next
    // prompt, mounted far enough down that it does not push the pinned card.
    const top = [0, 50, 100, 160, 300][index] ?? (300 + index * 100)
    setRect(row, top, 40)
  })

  let scrollTop = 200
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => 200 },
    scrollHeight: { configurable: true, get: () => 2000 },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (next: number) => { scrollTop = next },
    },
  })

  return {
    scroller,
    fold,
    card,
    rows,
    get scrollTop() { return scrollTop },
  }
}

function renderEarly(
  mountIndex: (index: number) => boolean = () => false,
  pinPromptMinimized = false,
) {
  const vGetFollowRef = { current: () => false }
  const mountIndexRef = { current: vi.fn(mountIndex) }
  const scrollerRef = { current: null as HTMLDivElement | null }
  const vScrollToBottomRef = { current: vi.fn() }
  const scrollToDisplayIndex = vi.fn()
  const hook = renderHook(() => useChatPageTranscriptEarlyController({
    activeTip: null,
    mountIndexRef,
    pinPromptMinimized,
    scrollerRef: scrollerRef as never,
    scrollToDisplayIndex: scrollToDisplayIndex as never,
    vGetFollowRef,
    vScrollToBottomRef,
  }))
  return { ...hook, vGetFollowRef, mountIndexRef, scrollerRef, vScrollToBottomRef, scrollToDisplayIndex }
}

beforeEach(() => {
  frames = []
  nextFrameId = 1
  clock = 0
  originalRaf = globalThis.requestAnimationFrame
  originalCancelRaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    const id = nextFrameId++
    frames.push({ id, cb })
    return id
  }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = ((id: number) => {
    frames = frames.filter(frame => frame.id !== id)
  }) as typeof cancelAnimationFrame
  nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => clock)
})

afterEach(() => {
  nowSpy.mockRestore()
  globalThis.requestAnimationFrame = originalRaf
  globalThis.cancelAnimationFrame = originalCancelRaf
  detachedNodes.splice(0).forEach(node => node.remove())
})

describe('useChatPageTranscriptEarlyController pinned prompt coverage', () => {
  it('derives a machine prompt from live geometry and throttles scroll recomputes', () => {
    const { result } = renderEarly()
    const geometry = mountGeometry(5)
    const items: DisplayItem[] = [
      single(0, 'user', 'first prompt'),
      single(1, 'assistant', 'first reply'),
      single(2, 'nudge', '[auto-nudge cycle 2]\ninspect the latest logs'),
      single(3, 'assistant', 'second reply'),
      single(4, 'user', 'next prompt'),
    ]

    act(() => {
      result.current.scrollerRef.current = geometry.scroller
      result.current.pinFoldRef.current = geometry.fold
      result.current.pinCardRef.current = geometry.card
      result.current.displayItemsRef.current = items
      result.current.onPinCollapsedHeight(60)
      result.current.updatePinnedPrompt()
    })

    expect(result.current.pinned).toMatchObject({
      idx: 2,
      raw: '[auto-nudge cycle 2]\ninspect the latest logs',
      full: 'inspect the latest logs',
      push: 0,
      bannerH: 70,
    })

    act(() => {
      result.current.onScrollPin()
      result.current.onScrollPin()
    })
    expect(frames).toHaveLength(1)
    flushFrame(16)
    expect(result.current.pinned?.idx).toBe(2)
  })

  it('keeps a minimized pin out of the card push and drop geometry', () => {
    const { result } = renderEarly(() => false, true)
    const geometry = mountGeometry(5)
    const items: DisplayItem[] = [
      single(0, 'user', 'first prompt'),
      single(1, 'assistant', 'first reply'),
      single(2, 'nudge', '[auto-nudge cycle 2]\ninspect the latest logs'),
      single(3, 'assistant', 'second reply'),
      single(4, 'user', 'incoming prompt'),
    ]
    // This puts the next prompt far enough up to fully push a card out. The
    // minimized chip must retain the current pin instead of applying that card
    // geometry; the later transcript controller normally synchronizes this ref.
    setRect(geometry.rows[4], 95, 40)

    act(() => {
      result.current.scrollerRef.current = geometry.scroller
      result.current.pinFoldRef.current = geometry.fold
      result.current.pinCardRef.current = geometry.card
      result.current.displayItemsRef.current = items
      result.current.onPinCollapsedHeight(60)
      result.current.pinMinimizedRef.current = true
      result.current.updatePinnedPrompt()
    })

    expect(result.current.pinned).toMatchObject({
      idx: 2,
      push: 0,
      // The 70px live card was deliberately ignored; this is the fallback
      // value the minimized chip uses only to preserve the pin state shape.
      bannerH: 60,
    })
  })

  it('uses live fold and card geometry for pinned jump chrome', () => {
    const { result } = renderEarly()
    const geometry = mountGeometry(3)

    act(() => {
      result.current.scrollerRef.current = geometry.scroller
      result.current.pinFoldRef.current = geometry.fold
      result.current.pinCardRef.current = geometry.card
      result.current.onPinCollapsedHeight(60)
    })

    // fold (100px) + card travel (70px + 4px) + convergence slack (24px)
    expect(result.current.pinnedJumpChrome()).toBe(198)
  })

  it('glides a near pinned jump and sends a far one through the mounted-row poll', () => {
    const near = renderEarly(() => false)
    const nearGeometry = mountGeometry(3)
    const items: DisplayItem[] = [
      single(0, 'user', 'first prompt'),
      single(1, 'assistant', 'reply'),
      single(2, 'user', 'target prompt'),
    ]
    act(() => {
      near.result.current.scrollerRef.current = nearGeometry.scroller
      near.result.current.pinFoldRef.current = nearGeometry.fold
      near.result.current.pinCardRef.current = nearGeometry.card
      setRect(nearGeometry.rows[2], 300, 40)
      near.result.current.displayItemsRef.current = items
      near.result.current.onPinCollapsedHeight(60)
      near.result.current.scrollToPinnedPrompt(2)
    })

    // Three stable observations enter the self-driven glide; the final frame
    // advances beyond GLIDE_MS so the test asserts the settled destination.
    flushFrame(0)
    flushFrame(16)
    flushFrame(32)
    flushFrame(532)
    expect(near.mountIndexRef.current).toHaveBeenCalledWith(2)
    expect(nearGeometry.scrollTop).toBeGreaterThan(200)

    const far = renderEarly(() => true)
    const farGeometry = mountGeometry(3)
    act(() => {
      far.result.current.scrollerRef.current = farGeometry.scroller
      far.result.current.pinFoldRef.current = farGeometry.fold
      far.result.current.pinCardRef.current = farGeometry.card
      far.result.current.displayItemsRef.current = items
      far.result.current.scrollToPinnedPrompt(2)
    })

    // A far jump delegates to navToDisplayIndex, which waits for the mounted
    // row instead of gliding across virtualizer spacer.  Its first poll step
    // may immediately scroll because this fixture already contains the row.
    flushFrame(548)
    expect(far.mountIndexRef.current).toHaveBeenCalledWith(2)
    expect(far.scrollToDisplayIndex).toHaveBeenCalledWith(2, {
      behavior: 'auto',
      align: 'start',
      offset: -198,
    })
  })
})
