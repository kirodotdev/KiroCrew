import { act, renderHook } from '@testing-library/react'
import { useSelectionQuoteAsk } from '../chat-core/composer/selectionActions'
import { quoteIntoDraft } from '../chat-core/composer/quoteDraft'
import {
  clearSideChatDrafts,
  consumeSideChatSeed,
  readSideChatDraft,
  seedSideChatDraft,
  useSideChatDraft,
  writeSideChatDraft,
} from '../chat-core/composer/sideChatDrafts'

/* The shared Quote / Ask seam behind every host of AssistantMessage's selection
 * toolbar (ChatPage, split-view ChatPane, the Members thread), and the per-slot
 * Side Chat draft store it seeds. Pins the pure pieces — draft shaping, the
 * store's write/seed/subscribe contract — and the host-facing hook contract:
 * Ask exists only when the host can bring a Side Chat on screen AND there is a
 * slot to seed, and a seed is a store write under that slot, so it is there
 * whether or not the panel is mounted yet. */

describe('quoteIntoDraft', () => {
  it('turns an empty draft into a blockquote followed by a blank line for the question', () => {
    expect(quoteIntoDraft('', 'first line\nsecond line')).toBe('> first line\n> second line\n\n')
    expect(quoteIntoDraft('   ', 'x')).toBe('> x\n\n')
  })

  it('stacks a second quote below existing content instead of replacing it', () => {
    const once = quoteIntoDraft('typed so far', 'alpha')
    expect(once).toBe('typed so far\n\n> alpha\n\n')
    expect(quoteIntoDraft(once, 'beta')).toBe('typed so far\n\n> alpha\n\n> beta\n\n')
  })
})

describe('sideChatDrafts store', () => {
  beforeEach(() => { clearSideChatDrafts() })

  it('keeps one draft per slot', () => {
    writeSideChatDraft('A', 'for a')
    writeSideChatDraft('B', 'for b')
    expect(readSideChatDraft('A')).toBe('for a')
    expect(readSideChatDraft('B')).toBe('for b')
    expect(readSideChatDraft('C')).toBe('')
  })

  it('a seed appends a blockquote to whatever the slot already holds and bumps the seed tick', () => {
    writeSideChatDraft('A', 'half typed')
    seedSideChatDraft('A', 'sel')
    expect(readSideChatDraft('A')).toBe('half typed\n\n> sel\n\n')
    const { result } = renderHook(() => useSideChatDraft('A'))
    expect(result.current.seedTick).toBe(1)
    act(() => { seedSideChatDraft('A', 'again') })
    expect(result.current.seedTick).toBe(2)
    expect(result.current.text).toBe('half typed\n\n> sel\n\n> again\n\n')
  })

  it('consuming the seed clears the mark but keeps the text — a remount does not re-nudge', () => {
    seedSideChatDraft('A', 'sel')
    const { result } = renderHook(() => useSideChatDraft('A'))
    expect(result.current.seedTick).toBe(1)
    act(() => { consumeSideChatSeed('A') })
    expect(result.current.seedTick).toBe(0)
    expect(result.current.text).toBe('> sel\n\n')
    // Idempotent: nothing pending, nothing to do.
    act(() => { consumeSideChatSeed('A') })
    expect(result.current.seedTick).toBe(0)
  })

  it('an empty selection seeds nothing', () => {
    seedSideChatDraft('A', '  \n')
    expect(readSideChatDraft('A')).toBe('')
  })

  it('a mounted reader re-renders from the store on every write', () => {
    writeSideChatDraft('A', 'x')
    seedSideChatDraft('A', 'y')
    const { result } = renderHook(() => useSideChatDraft('A'))
    expect(result.current.text).toBe('x\n\n> y\n\n')
    act(() => { writeSideChatDraft('A', 'live') })
    expect(result.current.text).toBe('live')
    act(() => { seedSideChatDraft('A', 'z') })
    expect(result.current.text).toBe('live\n\n> z\n\n')
  })

  it('a seed written BEFORE the reader mounts is there when it mounts (the late-panel case)', () => {
    seedSideChatDraft('late', 'the selection')
    const { result } = renderHook(() => useSideChatDraft('late'))
    expect(result.current.text).toBe('> the selection\n\n')
    expect(result.current.seedTick).toBe(1)
  })
})

describe('useSelectionQuoteAsk', () => {
  beforeEach(() => { clearSideChatDrafts() })

  it('offers Quote always and Ask only with BOTH a Side Chat opener and a slot to seed', () => {
    const setInput = vi.fn()
    const withoutSide = renderHook(() => useSelectionQuoteAsk({ slot: 'S', setInput }))
    expect(typeof withoutSide.result.current.onQuote).toBe('function')
    expect(withoutSide.result.current.onAsk).toBeUndefined()

    // No slot → no transcript to select from → no Ask, even with an opener.
    const withoutSlot = renderHook(() => useSelectionQuoteAsk({ slot: null, setInput, openSideChat: vi.fn() }))
    expect(typeof withoutSlot.result.current.onQuote).toBe('function')
    expect(withoutSlot.result.current.onAsk).toBeUndefined()

    const withSide = renderHook(() => useSelectionQuoteAsk({ slot: 'S', setInput, openSideChat: vi.fn() }))
    expect(typeof withSide.result.current.onAsk).toBe('function')
  })

  it('Quote appends the blockquote to the draft, starts one transit flight and reveals the composer', () => {
    let draft = 'typed'
    const setInput = vi.fn((upd: string | ((p: string) => string)) => { draft = typeof upd === 'function' ? upd(draft) : upd })
    const revealComposer = vi.fn()
    const { result } = renderHook(() => useSelectionQuoteAsk({ slot: 'S', setInput, revealComposer }))
    const rect = { top: 1, left: 2, width: 3, height: 4 } as DOMRect
    act(() => { result.current.onQuote('sel', rect) })
    expect(draft).toBe('typed\n\n> sel\n\n')
    expect(result.current.quoteFlight).toEqual({ text: 'sel', from: rect })
    expect(revealComposer).toHaveBeenCalledTimes(1)
    act(() => { result.current.endQuoteFlight() })
    expect(result.current.quoteFlight).toBeNull()
  })

  it('Ask opens the host Side Chat for THIS slot and seeds THIS slot\'s store, leaving the draft alone', () => {
    const setInput = vi.fn()
    const openSideChat = vi.fn()
    const { result } = renderHook(() => useSelectionQuoteAsk({ slot: 'pane-slot', setInput, openSideChat }))
    act(() => { result.current.onAsk!('why?') })
    expect(openSideChat).toHaveBeenCalledWith('pane-slot')
    expect(readSideChatDraft('pane-slot')).toBe('> why?\n\n')
    expect(readSideChatDraft('other')).toBe('')
    expect(setInput).not.toHaveBeenCalled()
  })

  it('a refused opener (returns false) seeds nothing — no hidden quote waits in the slot', () => {
    // The offline re-bind guard in ChatPage returns false; the Side Chat never
    // opened, so the selection must not be parked in that slot's draft store
    // to surface at some later, unrelated open.
    const openSideChat = vi.fn(() => false)
    const { result } = renderHook(() => useSelectionQuoteAsk({ slot: 'pane-slot', setInput: vi.fn(), openSideChat }))
    act(() => { result.current.onAsk!('why?') })
    expect(openSideChat).toHaveBeenCalledWith('pane-slot')
    expect(readSideChatDraft('pane-slot')).toBe('')
  })

  it('an opener that must re-bind first returns a Promise; the seed waits for its verdict', async () => {
    // Resolving true → seeded after the switch settled, not before.
    let settle: (ok: boolean) => void = () => {}
    const openSideChat = vi.fn(() => new Promise<boolean>(res => { settle = res }))
    const { result } = renderHook(() => useSelectionQuoteAsk({ slot: 'pane-slot', setInput: vi.fn(), openSideChat }))
    act(() => { result.current.onAsk!('why?') })
    expect(readSideChatDraft('pane-slot')).toBe('')
    await act(async () => { settle(true); await Promise.resolve() })
    expect(readSideChatDraft('pane-slot')).toBe('> why?\n\n')
  })

  it('a re-bind the server rejects (Promise of false) seeds nothing', async () => {
    const openSideChat = vi.fn(() => Promise.resolve(false))
    const { result } = renderHook(() => useSelectionQuoteAsk({ slot: 'pane-slot', setInput: vi.fn(), openSideChat }))
    await act(async () => { result.current.onAsk!('why?'); await Promise.resolve() })
    expect(readSideChatDraft('pane-slot')).toBe('')
  })
})
