/** ChatEmbed relays a read of the rows it renders, watermarked at the newest gateway-minted row, only while it is
 *  seen: a hidden page, an unfocused window, an inactive host tab, `display: none` or no area in view reads nothing. */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { ChatMessage } from '../types'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import { bindSlotReadSender, noteUnsavedRowTs, _resetSlotReadRelayForTest } from '../lib/slotReadRelay'

const mockGet = vi.fn()
const mockPost = vi.fn()
let mockHostActive: boolean | undefined
let mockTranscriptMounted = true

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: (p: string, ...a: unknown[]) => (p.endsWith('/question') ? Promise.resolve({ pending: [] }) : mockGet(p, ...a)), post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] }, active: mockHostActive }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList({ messages, transcript }: {
    messages: ChatMessage[]
    transcript?: { scrollerRef?: React.MutableRefObject<HTMLDivElement | null> }
  }, ref: React.Ref<unknown>) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return mockTranscriptMounted
      ? <div ref={transcript?.scrollerRef} data-testid="chat-message-list" data-rows={messages.map(m => m.content).join('|')} />
      : null
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

const SAVED_TS = '2026-10-16T11:59:30Z'
const NEWER_SAVED_TS = '2026-10-16T11:59:50Z'
const row = (content: string, ts: string, mid?: string): ChatMessage =>
  ({ role: 'assistant', content, cls: '', ts, ...(mid ? { meta: { mid } } : {}) })
// The last row has no gateway id, so its newer time must never become the watermark.
const PAGE = {
  messages: [row('first', '2026-10-16T11:59:00Z', 'm1'), row('reply', SAVED_TS, 'm2'), row('local copy', '2026-10-16T11:59:59Z')],
  running: false, title: 'Demo', has_more: false,
}

// The viewport, the transcript scroller's box, and what a hit test inside it lands on.
const VIEWPORT = { width: 1024, height: 768 }
type Box = { left: number; top: number; width: number; height: number }
const IN_VIEW: Box = { left: 0, top: 0, width: 400, height: 300 }
let scrollerBox: Box = IN_VIEW
let cover: Element | null = null

let queryClient: QueryClient
let sent: [string, string | undefined][]
let focused: boolean
let pageHidden: boolean

async function mount(wrapperStyle?: React.CSSProperties) {
  const ctx: AppSdkContextValue = {
    api: { get: mockGet, post: mockPost } as unknown as AppSdkContextValue['api'],
    info: { name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
  }
  const tree = (style?: React.CSSProperties) => (
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <div data-testid="wrapper" style={style}><ChatEmbed slotKey="slot-1" /></div>
      </AppSdkContext.Provider>
    </QueryClientProvider>
  )
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(tree(wrapperStyle)) })
  await advance(100)
  return {
    root: () => view.getByTestId('wrapper'),
    restyle: (style?: React.CSSProperties) => act(async () => { view.rerender(tree(style)) }),
    rerender: () => act(async () => { view.rerender(tree(wrapperStyle)) }),
  }
}

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
const fire = (target: EventTarget, type: string) => act(async () => { target.dispatchEvent(new Event(type)) })
const landNewerRow = () => mockGet.mockResolvedValue({ ...PAGE, messages: [...PAGE.messages, row('next', NEWER_SAVED_TS, 'm3')] })
const OUT_OF_VIEW: Record<string, () => void> = {
  'collapsed to no area': () => { scrollerBox = { ...IN_VIEW, height: 0 } },
  'scrolled below the viewport': () => { scrollerBox = { ...IN_VIEW, top: VIEWPORT.height + 50 } },
  'covered by another element at every probe point': () => { cover = document.body },
}

beforeEach(() => {
  vi.clearAllMocks()
  _resetSlotReadRelayForTest()
  resetEmbedRecoveryForTests()
  sent = []
  bindSlotReadSender((slot, ts) => { sent.push([slot, ts]) })
  mockHostActive = undefined
  mockTranscriptMounted = true
  focused = true
  pageHidden = false
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => pageHidden)
  scrollerBox = IN_VIEW
  cover = null
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    const box = this.matches('[data-testid="chat-message-list"]') ? scrollerBox : IN_VIEW
    const { left, top, width, height } = box
    return { left, top, width, height, x: left, y: top, right: left + width, bottom: top + height, toJSON: () => ({}) } as DOMRect
  })
  const win = document.defaultView!
  vi.spyOn(win, 'innerWidth', 'get').mockReturnValue(VIEWPORT.width)
  vi.spyOn(win, 'innerHeight', 'get').mockReturnValue(VIEWPORT.height)
  vi.spyOn(document, 'elementFromPoint').mockImplementation(() => cover ?? document.querySelector('[data-testid="chat-message-list"]'))
  mockGet.mockResolvedValue(PAGE)
  mockPost.mockResolvedValue({})
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-16T12:00:00Z'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(() => {
  queryClient.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
  _resetSlotReadRelayForTest()
})

describe('ChatEmbed read relay', () => {
  it('reads the rows it shows, up to the newest gateway-minted row', async () => {
    await mount()
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads nothing when only the header is on screen, then reads when the transcript is on screen', async () => {
    scrollerBox = { ...IN_VIEW, top: VIEWPORT.height + 50 }
    await mount()
    expect(sent).toEqual([])
    scrollerBox = IN_VIEW
    await advance(6000)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads nothing without a transcript scroller, then reads after it mounts', async () => {
    mockTranscriptMounted = false
    const view = await mount()
    expect(sent).toEqual([])
    mockTranscriptMounted = true
    await view.rerender()
    await advance(6000)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('does not widen the displayed watermark to an unsaved row it did not fetch', async () => {
    noteUnsavedRowTs('slot-1', '2026-10-16T11:59:55Z')
    await mount()
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('moves the read to a newer saved row once it renders', async () => {
    await mount()
    landNewerRow()
    await advance(6000)
    await advance(1100)
    expect(sent.at(-1)).toEqual(['slot-1', NEWER_SAVED_TS])
  })

  it('reads nothing while mounted under display: none, and reads once shown', async () => {
    const view = await mount({ display: 'none' })
    await advance(6000)
    expect(sent).toEqual([])
    await view.restyle(undefined)
    await fire(window, 'focus')
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('rechecks an unchanged landed read after a hidden cover lifts without resending it', async () => {
    const view = await mount({ visibility: 'hidden' })
    expect(sent).toEqual([])
    view.root().style.visibility = ''
    await advance(6000)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
    await advance(6000)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it.each(Object.keys(OUT_OF_VIEW))('reads nothing while %s, and reads at the next landed poll once in view', async (name) => {
    OUT_OF_VIEW[name]()
    await mount()
    await advance(6000)
    expect(sent).toEqual([])
    scrollerBox = IN_VIEW
    cover = null
    await advance(6000)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it.each(Object.keys(OUT_OF_VIEW))('reads nothing for a row that lands after it was %s, with no event in between', async (name) => {
    await mount()
    OUT_OF_VIEW[name]()
    landNewerRow()
    await advance(6000)
    await advance(1100)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads a row that lands after a visibility: hidden cover lifted, with no event in between', async () => {
    const view = await mount()
    await view.restyle({ visibility: 'hidden' })
    await fire(window, 'focus')
    await view.restyle(undefined)
    landNewerRow()
    await advance(6000)
    await advance(1100)
    expect(sent.at(-1)).toEqual(['slot-1', NEWER_SAVED_TS])
  })

  it('without checkVisibility, reads only while it has a box and no hidden or transparent cover', async () => {
    Object.defineProperty(HTMLElement.prototype, 'checkVisibility', { configurable: true, value: undefined })
    onTestFinished(() => { delete (HTMLElement.prototype as { checkVisibility?: unknown }).checkVisibility })
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
    const view = await mount({ opacity: 0 })
    await advance(6000)
    expect(sent).toEqual([])
    await view.restyle({ visibility: 'hidden' })
    await fire(window, 'focus')
    expect(sent).toEqual([])
    await view.restyle(undefined)
    await fire(window, 'focus')
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads nothing while an opacity: 0 ancestor hides it', async () => {
    const view = await mount()
    await view.restyle({ opacity: 0 })
    landNewerRow()
    await advance(6000)
    await advance(1100)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('sends its pending trailing read at once when it stops being seen', async () => {
    await mount()
    landNewerRow()
    await act(async () => { await queryClient.refetchQueries() })
    await advance(100)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
    focused = false
    await fire(window, 'blur')
    expect(sent).toEqual([['slot-1', SAVED_TS], ['slot-1', NEWER_SAVED_TS]])
  })

  it('flushes the trailing read on blur after it came into view with no event', async () => {
    OUT_OF_VIEW['collapsed to no area']()
    await mount()
    scrollerBox = IN_VIEW
    await advance(6000)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
    landNewerRow()
    await act(async () => { await queryClient.refetchQueries() })
    await advance(100)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
    focused = false
    await fire(window, 'blur')
    expect(sent).toEqual([['slot-1', SAVED_TS], ['slot-1', NEWER_SAVED_TS]])
  })

  it('reads nothing while a visibility: hidden cover hides it', async () => {
    const view = await mount()
    await view.restyle({ visibility: 'hidden' })
    landNewerRow()
    await advance(6000)
    await advance(1100)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads nothing in an unfocused window, and reads on focus', async () => {
    focused = false
    await mount()
    expect(sent).toEqual([])
    focused = true
    await fire(window, 'focus')
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads nothing on a hidden page, and reads on reveal', async () => {
    pageHidden = true
    await mount()
    expect(sent).toEqual([])
    pageHidden = false
    await fire(document, 'visibilitychange')
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('reads nothing in an inactive host tab, and reads once the tab is active', async () => {
    mockHostActive = false
    const view = await mount()
    expect(sent).toEqual([])
    mockHostActive = true
    await view.rerender()
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })

  it('does not read a row that lands after the embed was hidden', async () => {
    const view = await mount()
    await view.restyle({ display: 'none' })
    landNewerRow()
    await advance(6000)
    await advance(1100)
    expect(sent).toEqual([['slot-1', SAVED_TS]])
  })
})
