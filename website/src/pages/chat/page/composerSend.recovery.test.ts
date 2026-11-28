import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useComposerSend, type SentTurnRestore } from './composerSend'
import { useComposerDraftStores, type ComposerDraftStores } from './composerDrafts'
import { DRAFTS_KEY, __resetForTests as resetChatDrafts, isDraftApplied, loadDrafts, setDraft, takeDraftApplied } from '../../../utils/chatDrafts'
import { PASTE_DRAFTS_KEY } from '../../../utils/chatPasteDrafts'
import { SESSION_REF_DRAFTS_KEY } from '../../../utils/chatSessionRefDrafts'
import { __resetDeliveryWarningsForTests } from '../../../utils/deliveryWarning'
import { formatToken, restoreMissingBlocks, type PasteBlock } from '../../../utils/pasteTokens'
import { MAX_SESSION_REFS_RECOVERY, formatSessionRefLink } from '../../../utils/sessionRefs'
import type { RecoveredSends } from '../../../components/useDeliveryWarningStrip'

const SLOT = 'page-a'
const block: PasteBlock = { id: 'blk1', seq: 1, lines: 3, content: 'one\ntwo\nthree' }
const sentTurn = (inlined: string, pastes: PasteBlock[] = []): SentTurnRestore => ({ inlined, pastes, sessionRefs: [], quote: null })
const ref = <T,>(current: T) => ({ current })

/** The chat page's real draft stores and its real `restoreSentTurn`, for a slot the composer is not showing. */
const mountPage = () => {
  let stores: ComposerDraftStores = null!
  let page: ReturnType<typeof useComposerSend> = null!
  const hook = renderHook(() => {
    stores = useComposerDraftStores()
    page = useComposerSend({
      activeSlot: null, slots: [], dispatch: vi.fn(), slotRunning: false, autoFollowAllowed: () => false, scrollBottom: vi.fn(),
      drafts: stores.drafts, fileDrafts: stores.fileDrafts, pasteDrafts: stores.pasteDrafts, sessionRefDrafts: stores.sessionRefDrafts,
      saveDrafts: stores.saveDrafts,
      composerSlotRef: ref<string | null>(null), composerMountedRef: ref(true), activeSlotRef: ref<string | null>(null),
      inputRef: ref(''), pendingSessionsRef: ref([]), messageQuote: { recoverInto: (text: string) => text },
      setInput: vi.fn(), installPasteBlocks: vi.fn(), setPendingSessions: vi.fn(), readPasteBlocks: () => [],
      mergeSlotTokens: vi.fn(), knowledgeFetchRef: ref({ clearResults: vi.fn(), inject: vi.fn(), pendingKnowledge: null }),
    } as never)
  })
  // `applied` asks it the way ChatPage does: about the draft text THIS instance holds for the slot.
  return { stores, restore: (sent: SentTurnRestore, recovered?: RecoveredSends) => page.restoreSentTurn(SLOT, sent, recovered), applied: (sendId: string) => isDraftApplied(SLOT, sendId, stores.drafts.current[SLOT] ?? ''), unmount: hook.unmount }
}

const refuseWritesTo = (refused: string): void => { const write = Storage.prototype.setItem; vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) { if (key === refused) throw new DOMException('quota', 'QuotaExceededError'); write.call(this, key, value) }) }

/** What a fresh page load starts with: storage survives, in-memory state does not. */
const reload = () => { resetChatDrafts() }
const storedPlainText = (): string | undefined => { const v = (JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}') as Record<string, { text?: string } | string>)[SLOT]; return typeof v === 'string' ? v : v?.text }

describe('a recovered send whose text reached the page draft while another draft store refused its write', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); resetChatDrafts(); __resetDeliveryWarningsForTests() })
  afterEach(() => { vi.restoreAllMocks() })
  it('does not append the text again on reload, restores the paste block its token needs, and lets the record retire once that lands', () => {
    const first = mountPage()
    setDraft(first.stores.drafts.current, SLOT, 'existing draft')
    refuseWritesTo(PASTE_DRAFTS_KEY)
    const sent = sentTurn(`recovered ${formatToken(block)}`, [block])
    expect(first.restore(sent, { ids: ['q1'], applied: false }), 'the paste write was refused, so the record stays').not.toBe(true)
    first.unmount()
    vi.restoreAllMocks()
    const textOnDisk = storedPlainText()
    expect(textOnDisk).toContain('existing draft')
    expect(textOnDisk).toContain('recovered')
    reload()
    const second = mountPage()
    expect(second.applied('q1'), 'the ids come back with the text').toBe(true)
    expect(second.stores.pasteDrafts.current[SLOT]).toBeUndefined()
    expect(second.restore(sent, { ids: ['q1'], applied: true })).toBe(true)
    expect(second.stores.drafts.current[SLOT]).toBe(textOnDisk)
    expect(second.stores.drafts.current[SLOT].split('recovered')).toHaveLength(2)
    expect(second.stores.pasteDrafts.current[SLOT]).toEqual([block])
  })
  it('reads the ids against the text THIS instance holds, so a second tab that never adopted the stored text still gets the recovered words', () => {
    // A tab is a module, so the other tab recovers first and `reload` gives this one the module it mounts with.
    mountPage().restore(sentTurn('recovered elsewhere'), { ids: ['q-dup'], applied: false })
    const written = Object.keys(localStorage).map(k => [k, localStorage.getItem(k) ?? ''])
    reload()
    localStorage.clear()
    const dup = mountPage()
    setDraft(dup.stores.drafts.current, SLOT, 'what this tab was typing')
    for (const [k, v] of written) localStorage.setItem(k, v)
    loadDrafts()
    expect(dup.applied('q-dup'), 'the stored ids describe the stored text, not this composer\'s').toBe(false)
    expect(dup.restore(sentTurn('recovered elsewhere'), { ids: ['q-dup'], applied: false })).toBe(true)
    expect(dup.stores.drafts.current[SLOT]).toBe('what this tab was typing\n\nrecovered elsewhere')
  })
  it('keeps the ids through an ordinary edit, so a draft typed into while the history is read still gets the block its token needs', () => {
    const page = mountPage()
    refuseWritesTo(PASTE_DRAFTS_KEY)
    const sent = sentTurn(`recovered ${formatToken(block)}`, [block])
    page.restore(sent, { ids: ['q-edit'], applied: false })
    vi.restoreAllMocks()
    setDraft(page.stores.drafts.current, SLOT, page.stores.drafts.current[SLOT].replace('recovered', 'Recovered, edited'))
    expect(page.applied('q-edit'), 'an edit of the recovered draft is neither a send nor a clear').toBe(true)
    expect(page.restore(sent, { ids: ['q-edit'], applied: true })).toBe(true)
    expect(page.stores.pasteDrafts.current[SLOT]).toEqual([block])
  })
  it('reports the hand-back stored only when its text, paste blocks and session references all reached storage', () => {
    const sent = { ...sentTurn(`recovered ${formatToken(block)}`, [block]), sessionRefs: [{ key: 'chat-1-1', title: 'Other tab' }] }
    const stored = (refused?: string) => { localStorage.clear(); reload(); if (refused) refuseWritesTo(refused); const page = mountPage(); const held = page.restore(sent, { ids: ['q-all'], applied: false }); vi.restoreAllMocks(); page.unmount(); return held }
    expect([stored(), stored(DRAFTS_KEY), stored(PASTE_DRAFTS_KEY), stored(SESSION_REF_DRAFTS_KEY)]).toEqual([true, false, false, false])
  })
  it('reports a refs-only hand-back stored, and keeps its ids on the empty draft for a resend when the refs write is refused', () => {
    const sent = { ...sentTurn(''), sessionRefs: [{ key: 'chat-1-1', title: 'Other tab' }] }
    expect(mountPage().restore(sent, { ids: ['q-refs'], applied: false })).toBe(true)
    localStorage.clear(); reload()
    refuseWritesTo(SESSION_REF_DRAFTS_KEY)
    const page = mountPage()
    expect(page.restore(sent, { ids: ['q-refs'], applied: false })).toBe(false)
    setDraft(page.stores.drafts.current, SLOT, '')
    expect(takeDraftApplied(SLOT, page.stores.drafts.current[SLOT] ?? '')).toEqual(['q-refs'])
  })
  it('puts a reference the recovery ceiling cuts back as its link in the text, once, before it reports the hand-back stored', () => {
    const sent = { ...sentTurn('recovered'), sessionRefs: Array.from({ length: MAX_SESSION_REFS_RECOVERY }, (_, i) => ({ key: `chat-${i}-1`, title: `Ref ${i}` })) }
    const page = mountPage()
    page.stores.sessionRefDrafts.current[SLOT] = [{ key: 'chat-new-1', title: 'Staged since' }]
    expect(page.restore(sent, { ids: ['q-cut'], applied: false })).toBe(true)
    const cut = sent.sessionRefs[MAX_SESSION_REFS_RECOVERY - 1]
    expect(page.stores.sessionRefDrafts.current[SLOT].some(r => r.key === cut.key)).toBe(false)
    expect(page.stores.drafts.current[SLOT].split(formatSessionRefLink(cut))).toHaveLength(2)
    expect(page.restore(sent, { ids: ['q-cut'], applied: true })).toBe(true)
    expect(page.stores.drafts.current[SLOT].split(formatSessionRefLink(cut))).toHaveLength(2)
  })

  it('restores a legacy id-less token by its one matching seq, an id-bearing token only by id', () => {
    const [legacy, other, gone] = ['x [ Paste #1 · 3 lines ]', { ...block, id: 'blk2' }, formatToken({ ...block, id: 'gone' })]
    expect([[legacy, [block]], [legacy, [block, other]], [gone, [other]]].map(([t, sent]) => restoreMissingBlocks(t as string, [], sent as PasteBlock[]))).toEqual([[block], [], []])
  })
  it('writes the applied ids in the same stored entry as the text, so a refused text write leaves neither', () => {
    const page = mountPage()
    refuseWritesTo(DRAFTS_KEY)
    page.restore(sentTurn('recovered'), { ids: ['q-refused'], applied: false })
    page.unmount()
    vi.restoreAllMocks()
    reload()
    expect(mountPage().applied('q-refused')).toBe(false)
  })
  it('drops the applied ids with a draft that was cleared or sent, so a later recovery appends normally', () => {
    const first = mountPage()
    first.restore(sentTurn('first recovered'), { ids: ['q1'], applied: false })
    setDraft(first.stores.drafts.current, SLOT, '')
    first.stores.saveDrafts()
    first.unmount()
    expect(JSON.stringify(JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}'))).not.toContain('q1')
    reload()
    const second = mountPage()
    expect(second.applied('q1')).toBe(false)
    second.restore(sentTurn('second recovered'), { ids: ['q2'], applied: false })
    expect(second.stores.drafts.current[SLOT]).toBe('second recovered')
    expect(takeDraftApplied(SLOT, second.stores.drafts.current[SLOT])).toEqual(['q2'])
    expect(second.applied('q2')).toBe(false)
    setDraft(second.stores.drafts.current, SLOT, 'typed since')
    second.restore(sentTurn('third recovered'), { ids: ['q3'], applied: false })
    expect(second.stores.drafts.current[SLOT]).toBe('typed since\n\nthird recovered')
  })
})
