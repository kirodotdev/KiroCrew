import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useComposerSend, type SentTurnRestore } from './composerSend'
import { useComposerDraftStores, type ComposerDraftStores } from './composerDrafts'
import { DRAFTS_KEY, __resetForTests as resetChatDrafts, isDraftApplied, setDraft } from '../../../utils/chatDrafts'
import { PASTE_DRAFTS_KEY } from '../../../utils/chatPasteDrafts'
import { __resetDeliveryWarningsForTests } from '../../../utils/deliveryWarning'
import { formatToken, restoreMissingBlocks, type PasteBlock } from '../../../utils/pasteTokens'
import type { RecoveredSends } from '../../../components/useDeliveryWarningStrip'

const SLOT = 'page-a'
const block: PasteBlock = { id: 'blk1', seq: 1, lines: 3, content: 'one\ntwo\nthree' }
const sentTurn = (inlined: string, pastes: PasteBlock[] = []): SentTurnRestore =>
  ({ inlined, pastes, sessionRefs: [], quote: null })

const ref = <T,>(current: T) => ({ current })

/** The chat page's real draft stores and its real `restoreSentTurn`, for a slot the composer is not showing. */
const mountPage = () => {
  let stores: ComposerDraftStores = null!
  let restore: ReturnType<typeof useComposerSend>['restoreSentTurn'] = null!
  const hook = renderHook(() => {
    stores = useComposerDraftStores()
    restore = useComposerSend({
      activeSlot: null, slots: [], dispatch: vi.fn(), slotRunning: false, autoFollowAllowed: () => false, scrollBottom: vi.fn(),
      drafts: stores.drafts, fileDrafts: stores.fileDrafts, pasteDrafts: stores.pasteDrafts, sessionRefDrafts: stores.sessionRefDrafts,
      saveDrafts: stores.saveDrafts, draftOwner: stores.draftOwner,
      composerSlotRef: ref<string | null>(null), composerMountedRef: ref(true), activeSlotRef: ref<string | null>(null),
      inputRef: ref(''), pendingSessionsRef: ref([]), messageQuote: { recoverInto: (text: string) => text },
      setInput: vi.fn(), installPasteBlocks: vi.fn(), setPendingSessions: vi.fn(), readPasteBlocks: () => [],
      mergeSlotTokens: vi.fn(), knowledgeFetchRef: ref({ clearResults: vi.fn(), inject: vi.fn(), pendingKnowledge: null }),
    } as never).restoreSentTurn
  })
  return { stores, restore: (sent: SentTurnRestore, recovered?: RecoveredSends) => restore(SLOT, sent, recovered), unmount: hook.unmount }
}

const refuseWritesTo = (refused: string) => {
  const write = Storage.prototype.setItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (key === refused) throw new DOMException('quota', 'QuotaExceededError')
    write.call(this, key, value)
  })
}

/** What a fresh page load starts with: storage survives, in-memory state does not. */
const reload = () => { resetChatDrafts() }

const storedText = (): unknown => (JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}') as Record<string, { text?: string } | string>)[SLOT]
const storedPlainText = (): string | undefined => {
  const v = storedText()
  return typeof v === 'string' ? v : (v as { text?: string } | undefined)?.text
}

describe('a recovered send whose text reached the page draft while another draft store refused its write', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetChatDrafts()
    __resetDeliveryWarningsForTests()
  })
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
    expect(isDraftApplied(SLOT, 'q1'), 'the ids come back with the text').toBe(true)
    expect(second.stores.pasteDrafts.current[SLOT]).toBeUndefined()
    expect(second.restore(sent, { ids: ['q1'], applied: true })).toBe(true)
    expect(second.stores.drafts.current[SLOT]).toBe(textOnDisk)
    expect(second.stores.drafts.current[SLOT].split('recovered')).toHaveLength(2)
    expect(second.stores.pasteDrafts.current[SLOT]).toEqual([block])
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
    mountPage()
    expect(isDraftApplied(SLOT, 'q-refused')).toBe(false)
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
    expect(isDraftApplied(SLOT, 'q1')).toBe(false)
    second.restore(sentTurn('second recovered'), { ids: ['q2'], applied: false })
    expect(second.stores.drafts.current[SLOT]).toBe('second recovered')
    setDraft(second.stores.drafts.current, SLOT, 'typed since')
    second.restore(sentTurn('third recovered'), { ids: ['q3'], applied: false })
    expect(second.stores.drafts.current[SLOT]).toBe('typed since\n\nthird recovered')
  })
})
