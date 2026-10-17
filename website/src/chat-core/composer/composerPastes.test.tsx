import { describe, it, expect, vi, afterAll, afterEach, beforeEach } from 'vitest'
import { useMemo, useState, type ReactNode } from 'react'
import { act, cleanup, fireEvent, render, renderHook, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../../store/chatSlice'
import dashboardReducer from '../../store/dashboardSlice'
import notificationsReducer from '../../store/notificationsSlice'
import type { RootState } from '../../store'

/* chat-core P3-c: the Paste atom. The hook table pins the block store's
 * contract (carry, install, set, identity); the mounted cases drive a real
 * `ChatInput` under a real `Composer` root in the two shapes hosts use -- the
 * main chat's (`draft` store, slice over the page's state) and the pane's
 * (`value`, the atom itself passed as `pastes`) -- through paste, prune,
 * expand-on-send and carry-back. */

// Independent of whatever ran before in the same worker: start from a fresh
// module registry, so the mocks below bind even when another file has already
// loaded these modules (and `matchMedia` is restored for the next one).
vi.hoisted(() => { vi.resetModules() })
vi.mock('../../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../api/client', () => ({
  api: {
    sttConfig: vi.fn().mockResolvedValue({ enabled: false, available: false }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    agents: vi.fn().mockResolvedValue([]),
    models: vi.fn().mockResolvedValue([]),
    dashboardConfig: vi.fn().mockResolvedValue({}),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {},
}))
vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

const MATCH_MEDIA = Object.getOwnPropertyDescriptor(window, 'matchMedia')
Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})
// Explicit, not only Testing Library's auto-cleanup: that hook is registered
// once per worker when the library first loads, so a file that is not first
// in a shared worker would otherwise keep every earlier test's DOM.
afterEach(() => { cleanup() })
afterAll(() => {
  if (MATCH_MEDIA) Object.defineProperty(window, 'matchMedia', MATCH_MEDIA)
  else Reflect.deleteProperty(window, 'matchMedia')
})

import ChatInput from '../../components/ChatInput'
import { Composer } from './Composer'
import { createComposerDraftStore, type ComposerDraftStore } from './draftStore'
import { storeSentPastes, useComposerPastes, type ComposerPastes } from './composerPastes'
import { buildOutgoingTurn, type OutgoingTurn } from './outgoingTurn'
import { formatToken, mergeCarriedDraft, readStoredPaste, type PasteBlock } from '../../utils/pasteTokens'
import { SlotProvider } from '../../providers/SlotContext'

const PASTED = 'line1\nline2\nline3\nline4\nline5'
const SECOND = 'aa\nbb\ncc\ndd'
const T1 = '[ Paste #1 · 5 lines ]'
const A: PasteBlock = { id: 'a', seq: 1, lines: 3, content: 'a1\na2\na3' }
const B: PasteBlock = { id: 'b', seq: 1, lines: 3, content: 'b1\nb2\nb3' }
const TA = '[ Paste #1 · 3 lines ]'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: [], unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function Providers({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <Provider store={makeStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SlotProvider slotId="chat-1-x">{children}</SlotProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
}

/** The container of this test's own render. Queries are scoped to it, never
 *  to the whole document, so DOM another file left in a shared worker cannot
 *  answer them. */
let host: HTMLElement = document.body
async function mount(ui: ReactNode) {
  await act(async () => { host = render(<Providers>{ui}</Providers>).container })
}
const box = () => within(host).getByRole('textbox', { name: 'Message input' }) as HTMLTextAreaElement

/** Paste through ChatInput's real handler (it reads `getData('text')`). */
async function pasteInto(el: HTMLTextAreaElement, text: string) {
  await act(async () => {
    fireEvent.paste(el, { clipboardData: { items: [], getData: (t: string) => (t === 'text' ? text : '') } })
  })
}

beforeEach(() => { localStorage.clear() })

describe('useComposerPastes', () => {
  it('a carry with no blocks writes nothing and hands the text back as is', () => {
    const { result } = renderHook(() => useComposerPastes())
    let carried!: ReturnType<ComposerPastes['carry']>
    act(() => {
      // A plain set still pending in the batch (an editor edit) keeps its value.
      result.current.set([A])
      carried = result.current.carry('plain', [])
    })
    expect(carried).toEqual({ text: 'plain', full: 'plain', pastes: [] })
    expect(result.current.blocks).toEqual([A])
  })

  it('a carry renumbers a block whose number is taken and rewrites its token', () => {
    const { result } = renderHook(() => useComposerPastes())
    act(() => { result.current.install([A]) })
    let carried!: ReturnType<ComposerPastes['carry']>
    act(() => { carried = result.current.carry(`retry ${formatToken(B)}`, [B]) })
    expect(carried.text).toBe(`retry ${formatToken({ ...B, seq: 2 })}`)
    expect(result.current.blocks).toEqual([A, { ...B, seq: 2 }])
  })

  it('a carry drops the token of a block the composer already holds, without adding it twice', () => {
    const { result } = renderHook(() => useComposerPastes())
    act(() => { result.current.install([A]) })
    let carried!: ReturnType<ComposerPastes['carry']>
    act(() => { carried = result.current.carry(`again ${TA}`, [A]) })
    expect(carried.text).toBe('again ')
    expect(result.current.blocks).toEqual([A])
  })

  it('two carries in one batch compose: the second carries on top of the first', () => {
    const { result } = renderHook(() => useComposerPastes())
    act(() => {
      result.current.carry(`one ${TA}`, [A])
      // Still inside the batch, before any render: the first carry is visible.
      expect(result.current.read()).toEqual([A])
      result.current.carry(`two ${TA}`, [B])
    })
    expect(result.current.blocks).toEqual([A, { ...B, seq: 2 }])
  })

  it('set is the plain setter: read() catches up on the next render, install at once', () => {
    const { result } = renderHook(() => useComposerPastes())
    act(() => {
      result.current.set([A])
      expect(result.current.read()).toEqual([])
    })
    expect(result.current.read()).toEqual([A])
    act(() => {
      result.current.install([B])
      expect(result.current.read()).toEqual([B])
    })
  })

  it('keeps its members stable and changes identity only with the blocks', () => {
    const { result, rerender } = renderHook(() => useComposerPastes())
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
    act(() => { result.current.set([A]) })
    expect(result.current).not.toBe(first)
    for (const k of ['set', 'read', 'install', 'carry'] as const) expect(result.current[k]).toBe(first[k])
  })

  it('records a send in the paste side table, and nothing for a turn with no paste', () => {
    const turn = buildOutgoingTurn({ text: `see ${TA}`, pastes: [A] }, 'send')
    storeSentPastes(turn.storedPaste)
    expect(readStoredPaste(turn.wire)).toEqual(expect.objectContaining({ displayTxt: `see ${TA}`, pastes: [A] }))
    const plain = buildOutgoingTurn({ text: 'hi' }, 'send')
    storeSentPastes(plain.storedPaste)
    expect(readStoredPaste('hi')).toBeNull()
  })
})

/* ── The main chat's shape: the text in a `draft` store, the slice built over
 *    the page's own paste state, no paste props on ChatInput. ── */
interface PageHandle { draft: ComposerDraftStore; blocks: PasteBlock[]; sent: OutgoingTurn[] }
function PagePreset({ handle, showFullPastes = false, strayProps }: { handle: PageHandle; showFullPastes?: boolean; strayProps?: { pasteBlocks: PasteBlock[]; onPasteBlocksChange: (b: PasteBlock[]) => void } }) {
  const [draft] = useState(() => createComposerDraftStore(''))
  const { blocks, set } = useComposerPastes()
  const slice = useMemo(() => ({ blocks, set }), [blocks, set])
  handle.draft = draft
  handle.blocks = blocks
  const onSend = () => { handle.sent.push(buildOutgoingTurn({ text: draft.get(), pastes: blocks }, 'send')) }
  return (
    <Composer slotKey="chat-1-x" draft={draft} onChange={draft.set} pastes={slice}>
      <ChatInput onChange={draft.set} onSend={onSend} showFullPastes={showFullPastes} {...strayProps} />
    </Composer>
  )
}

describe('Paste atom under the main chat preset (draft store)', () => {
  it('a large paste becomes a chip backed by a block, and a send expands it on the wire only', async () => {
    const handle = { sent: [] } as unknown as PageHandle
    await mount(<PagePreset handle={handle} />)
    await act(async () => { fireEvent.change(box(), { target: { value: 'please read ' } }) })
    await pasteInto(box(), PASTED)
    expect(box().value).toMatch(/\[ Paste #1(?:\u2063[\u200b\u200c]+\u2063)? · 5 lines \]/)
    expect(box().value).not.toContain('line3')
    expect(handle.blocks).toEqual([expect.objectContaining({ seq: 1, lines: 5, content: PASTED })])
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' }) })
    expect(handle.sent).toHaveLength(1)
    expect(handle.sent[0].wire).toBe(`please read \n${PASTED}`)
    expect(handle.sent[0].bubble).toBe(`please read \n${formatToken(handle.blocks[0])}`)
    expect(handle.sent[0].meta.pastes).toEqual(handle.blocks)
  })

  it('deleting the token as text prunes its block', async () => {
    const handle = { sent: [] } as unknown as PageHandle
    await mount(<PagePreset handle={handle} />)
    await pasteInto(box(), PASTED)
    expect(handle.blocks).toHaveLength(1)
    await act(async () => { fireEvent.change(box(), { target: { value: 'nothing pasted now' } }) })
    expect(handle.blocks).toEqual([])
  })

  it('with full pastes on, a large paste stays editable text and stages no block', async () => {
    const handle = { sent: [] } as unknown as PageHandle
    await mount(<PagePreset handle={handle} showFullPastes />)
    await pasteInto(box(), PASTED)
    expect(box().value).not.toContain(T1)
    expect(handle.blocks).toEqual([])
  })

  it('the root slice wins over paste props handed to ChatInput', async () => {
    const handle = { sent: [] } as unknown as PageHandle
    const stray = { pasteBlocks: [{ ...A, id: 'stray', seq: 7 }], onPasteBlocksChange: vi.fn() }
    await mount(<PagePreset handle={handle} strayProps={stray} />)
    await pasteInto(box(), PASTED)
    expect(handle.blocks).toEqual([expect.objectContaining({ seq: 1, content: PASTED })])
    expect(stray.onPasteBlocksChange).not.toHaveBeenCalled()
  })
})

/* ── The pane's shape: controlled `value`, the atom itself as `pastes`, the
 *    host's refused-send recovery through `carry`. ── */
interface PaneHandle { pastes: ComposerPastes; setInput: (v: string | ((p: string) => string)) => void; sent: OutgoingTurn[] }
function PanePreset({ handle }: { handle: PaneHandle }) {
  const [input, setInput] = useState('')
  const pastes = useComposerPastes()
  handle.pastes = pastes
  handle.setInput = setInput
  const onSend = () => { handle.sent.push(buildOutgoingTurn({ text: input, pastes: pastes.blocks }, 'send')) }
  return (
    <Composer slotKey="pane-1" value={input} onChange={setInput} pastes={pastes}>
      <ChatInput value={input} onChange={setInput} onSend={onSend} />
    </Composer>
  )
}

describe('Paste atom under the pane preset (controlled value)', () => {
  /** What the pane does with a refused send: clear, then carry the payload back. */
  function refuse(handle: PaneHandle, turnText: string, blocks: PasteBlock[]) {
    const carried = handle.pastes.carry(turnText, blocks)
    handle.setInput(cur => mergeCarriedDraft(cur, carried))
  }

  it('a refused send comes back as the same chip, and the retry sends the content', async () => {
    const handle = { sent: [] } as unknown as PaneHandle
    await mount(<PanePreset handle={handle} />)
    await pasteInto(box(), PASTED)
    const genToken = box().value
    expect(genToken).toMatch(/^\[ Paste #1(?:\u2063[\u200b\u200c]+\u2063)? · 5 lines \]$/)
    const sentBlocks = handle.pastes.blocks
    await act(async () => { handle.setInput(''); handle.pastes.set([]) })
    expect(box().value).toBe('')
    await act(async () => { refuse(handle, genToken, sentBlocks) })
    expect(box().value).toBe(genToken)
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' }) })
    expect(handle.sent.at(-1)?.wire).toBe(PASTED)
  })

  it('two refusals settling in one batch both come back, numbered apart', async () => {
    const handle = { sent: [] } as unknown as PaneHandle
    await mount(<PanePreset handle={handle} />)
    const one: PasteBlock = { id: 'p1', seq: 1, lines: 5, content: PASTED }
    const two: PasteBlock = { id: 'p2', seq: 1, lines: 4, content: SECOND }
    await act(async () => {
      refuse(handle, formatToken(one), [one])
      refuse(handle, formatToken(two), [two])
    })
    expect(box().value).toMatch(/\[ Paste #1(?:\u2063[\u200b\u200c]+\u2063)? · 5 lines \][\s\S]*\[ Paste #2(?:\u2063[\u200b\u200c]+\u2063)? · 4 lines \]/)
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' }) })
    const wire = handle.sent.at(-1)?.wire ?? ''
    expect(wire).toContain(PASTED)
    expect(wire).toContain(SECOND)
    expect(wire).not.toMatch(/\[ Paste #\d/)
  })

  it('install swaps the blocks at once, so a rebind restores a backed chip', async () => {
    const handle = { sent: [] } as unknown as PaneHandle
    await mount(<PanePreset handle={handle} />)
    await act(async () => {
      handle.pastes.install([A])
      expect(handle.pastes.read()).toEqual([A])
      handle.setInput(`back ${TA}`)
    })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter', code: 'Enter' }) })
    expect(handle.sent.at(-1)?.wire).toBe(`back ${A.content}`)
  })
})

describe('ChatInput with no root', () => {
  it('still collapses a large paste through its paste props', async () => {
    const onPasteBlocksChange = vi.fn()
    await mount(<ChatInput value="" onChange={() => {}} onSend={() => {}} pasteBlocks={[]} onPasteBlocksChange={onPasteBlocksChange} />)
    await pasteInto(box(), PASTED)
    expect(onPasteBlocksChange).toHaveBeenCalledWith([expect.objectContaining({ seq: 1, content: PASTED })])
  })
})
