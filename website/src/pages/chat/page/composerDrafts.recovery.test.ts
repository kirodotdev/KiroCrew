import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useComposerDraftLifecycle, useComposerDraftStores, type ComposerDraftStores } from './composerDrafts'
import { DRAFTS_KEY, setDraft } from '../../../utils/chatDrafts'
import { DRAFT_MAX_STORE_BYTES } from '../../../utils/draftConstants'
import { PASTE_DRAFTS_KEY, setPasteDraft } from '../../../utils/chatPasteDrafts'
import { SESSION_REF_DRAFTS_KEY, setSessionRefDraft } from '../../../utils/chatSessionRefDrafts'
import { __resetDeliveryWarningsForTests, awaitDraftSave, type DraftOwner } from '../../../utils/deliveryWarning'
import { loadSendRecoveries, parkSendRecovery } from '../../../utils/sendRecoveryStore'

const awaiting = (slot: string, sendId: string, owner: DraftOwner) => {
  parkSendRecovery(slot, { sendId, surface: 'page', sent: { inlined: 'handed back' } })
  awaitDraftSave(slot, 'page', [sendId], owner)
}

const ref = <T,>(current: T) => ({ current })

/** A chat page's draft stores with the lifecycle that flushes them on unmount. */
const mountPage = (activeSlot: string, typed = '') => {
  let stores: ComposerDraftStores = null!
  const hook = renderHook(() => {
    stores = useComposerDraftStores()
    useComposerDraftLifecycle({
      activeSlot, stores,
      staging: {
        stagedNowRef: ref({ files: [], pastes: [], sessions: [] }),
        pendingFilesRef: ref([]), pasteBlocksRef: ref([]), pendingSessionsRef: ref([]),
        setPendingFiles: vi.fn(), setPasteBlocks: vi.fn(), setPendingSessions: vi.fn(),
        setUploadError: vi.fn(), setUploadHint: vi.fn(), pickedFileTokens: ref({}),
      },
      prevSlot: ref<string | null>(null), composerSlotRef: ref(activeSlot),
      inputRef: ref(typed), setInput: vi.fn(), consumedPrefillRef: ref(null), foregroundCreateId: null,
      foregroundCreateIdRef: ref(null), lastCreatedActivationRef: ref(null),
      knowledgeFetchRef: ref({ clearResults: vi.fn() }), raisePrefillHint: vi.fn(), setPrefillHint: vi.fn(),
      setActionError: vi.fn(), updateHistoryQuery: vi.fn(),
    } as never)
  })
  return { stores, unmount: hook.unmount }
}

const refuseWritesTo = (refused: string) => {
  const write = Storage.prototype.setItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (key === refused) throw new DOMException('quota', 'QuotaExceededError')
    write.call(this, key, value)
  })
}

const stagedTurn = () => {
  const { result } = renderHook(() => useComposerDraftStores())
  const stores = result.current
  setDraft(stores.drafts.current, 'page-a', 'handed back [ Paste #1 · 2 lines ]')
  setPasteDraft(stores.pasteDrafts.current, 'page-a', [{ id: 'p1', seq: 1, lines: 2, content: 'one\ntwo' }])
  setSessionRefDraft(stores.sessionRefDrafts.current, 'page-a', [{ key: 'chat-1-1', title: 'Other tab' }])
  return stores
}

describe('a recovered send handed back into the chat page composer', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __resetDeliveryWarningsForTests()
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('keeps an awaiting record whose draft the byte budget evicted, through later saves that still lack it, and retires it once a save keeps that draft again', () => {
    const { result } = renderHook(() => useComposerDraftStores())
    const stores = result.current
    awaiting('page-a', 'q-evicted', stores.draftOwner)
    setDraft(stores.drafts.current, 'page-a', 'handed back')
    setDraft(stores.drafts.current, 'page-big', 'x'.repeat(DRAFT_MAX_STORE_BYTES))
    expect(stores.saveDrafts()).toBe(true)
    expect(stores.drafts.current['page-a']).toBeUndefined()
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-evicted'])

    expect(stores.saveDrafts()).toBe(true)
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-evicted'])

    setDraft(stores.drafts.current, 'page-big', '')
    setDraft(stores.drafts.current, 'page-a', 'handed back')
    expect(stores.saveDrafts()).toBe(true)
    expect(loadSendRecoveries('page-a')).toEqual([])
  })

  it('keeps an awaiting record when the byte budget evicts its paste blocks though its text draft was kept, since the text alone would restore a dead paste token', () => {
    const stores = stagedTurn()
    awaiting('page-a', 'q-paste-evicted', stores.draftOwner)
    setPasteDraft(stores.pasteDrafts.current, 'page-big', [{ id: 'big', seq: 1, lines: 1, content: 'x'.repeat(DRAFT_MAX_STORE_BYTES) }])
    expect(stores.saveDrafts()).toBe(true)
    expect(stores.drafts.current['page-a']).toBeDefined()
    expect(stores.pasteDrafts.current['page-a']).toBeUndefined()
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-paste-evicted'])
  })

  it('keeps the records when storage refuses the drafts, so a crash still has a copy', () => {
    const { result } = renderHook(() => useComposerDraftStores())
    awaiting('page-a', 'q-refused', result.current.draftOwner)
    setDraft(result.current.drafts.current, 'page-a', 'handed back')
    const write = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === DRAFTS_KEY) throw new DOMException('quota', 'QuotaExceededError')
      write.call(this, key, value)
    })
    expect(result.current.saveDrafts()).toBe(false)
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-refused'])
  })

  it('keeps the records when the paste blocks behind a stored text draft are refused, since the text alone would restore a dead paste token', () => {
    const stores = stagedTurn()
    awaiting('page-a', 'q-pastes', stores.draftOwner)
    refuseWritesTo(PASTE_DRAFTS_KEY)
    stores.saveDrafts()
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-pastes'])
  })

  it('keeps the records when the staged session references are refused, so the recovered turn does not lose its references', () => {
    const stores = stagedTurn()
    awaiting('page-a', 'q-refs', stores.draftOwner)
    refuseWritesTo(SESSION_REF_DRAFTS_KEY)
    stores.saveDrafts()
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-refs'])
  })

  it('retires the records once text, files, paste blocks and session references all reach storage', () => {
    const stores = stagedTurn()
    awaiting('page-a', 'q-all', stores.draftOwner)
    expect(stores.saveDrafts()).toBe(true)
    expect(loadSendRecoveries('page-a')).toEqual([])
  })

  it('keeps the record when a fresh page instance saves after the instance holding the text unmounted on a refused flush', () => {
    const first = mountPage('page-a', 'handed back')
    awaiting('page-a', 'q-gone', first.stores.draftOwner)
    refuseWritesTo(DRAFTS_KEY)
    first.unmount()
    vi.restoreAllMocks()
    const second = mountPage('page-a')
    expect(second.stores.saveDrafts()).toBe(true)
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-gone'])
  })

  it('keeps the record when another live page instance saves, since its stores do not hold the handed-back text', () => {
    const holder = renderHook(() => useComposerDraftStores())
    awaiting('page-a', 'q-held', holder.result.current.draftOwner)
    const other = renderHook(() => useComposerDraftStores())
    expect(other.result.current.saveDrafts()).toBe(true)
    expect(loadSendRecoveries('page-a').map(r => r.sendId)).toEqual(['q-held'])
  })

  it('retires the record when the instance holding the text flushes it on unmount', () => {
    const page = mountPage('page-a', 'handed back')
    awaiting('page-a', 'q-flushed', page.stores.draftOwner)
    page.unmount()
    expect(loadSendRecoveries('page-a')).toEqual([])
  })
})
